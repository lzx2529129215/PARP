#!/usr/bin/env python3
"""Train/evaluate the nested visit model; numpy and torch are sufficient."""
from __future__ import annotations

import argparse
import csv
import json
import random
import sys
import time
from pathlib import Path

import numpy as np
import torch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from v3.models.app_lstm_visit_window import (AppLSTMVisitWindow, MODEL_TYPE, FORMAT,
    HORIZONS, VISIT_DEFINITION, encode_features, masked_loss, log_probabilities, visit_probabilities)


def read_data(path, app_vocab, group_vocab, history_len=5):
    encoded, labels, masks, metadata = [], [], [], []
    with Path(path).open() as f:
        for row in csv.DictReader(f):
            encoded.append(encode_features(row["history_apps"].split("|"), row["history_durations_s"].split("|"),
                row["history_mask"].split("|"), row["opened_apps"].split("|"), row["current_app"],
                row["timestamp"], row["user_group"], app_vocab, group_vocab, history_len))
            target = np.zeros((len(app_vocab), 2), np.float32)
            valid = np.zeros_like(target)
            for i, h in enumerate(HORIZONS):
                for app in filter(None, row[f"labels_visit_{h}s"].split("|")):
                    target[app_vocab[app], i] = 1.
                valid[:, i] = np.array(row[f"valid_visit_{h}s"].split("|"), np.float32)
            if np.any((target != 0) & (target != 1)) or np.any((valid != 0) & (valid != 1)):
                raise ValueError("labels and masks must be binary")
            both = valid[:, 0] * valid[:, 1] > 0
            if np.any(target[both, 0] > target[both, 1]):
                raise ValueError("non-nested window labels")
            labels.append(target)
            masks.append(valid)
            metadata.append({key: row[key] for key in ("user_id", "session_id", "timestamp", "current_app", "opened_apps")})
    if not encoded:
        raise ValueError(f"empty dataset: {path}")
    integer = {"history_apps", "user_group", "current_app"}
    features = {key: torch.tensor(np.array([r[key] for r in encoded]),
                dtype=torch.long if key in integer else torch.float32) for key in encoded[0]}
    return features, torch.tensor(np.array(labels)), torch.tensor(np.array(masks)), metadata


def average_precision(labels, scores):
    """Step-integrated PR area (AP), grouping equal scores at one threshold."""
    labels = np.asarray(labels, np.int64)
    if not len(labels) or not labels.sum():
        return None
    order = np.argsort(-scores, kind="stable")
    y, p = labels[order], scores[order]
    ends = np.r_[np.flatnonzero(np.diff(p)), len(p) - 1]
    tp = np.cumsum(y)[ends]
    recall = tp / labels.sum()
    precision = tp / (ends + 1)
    return float(np.sum(np.diff(np.r_[0., recall]) * precision))


def ratio(num, den):
    return float(num / den) if den else None


def metrics(probabilities, labels, valid, eligible, app_vocab, hot=.90, cold=.20):
    p, y, v = np.asarray(probabilities), np.asarray(labels), np.asarray(valid).astype(bool)
    result = {"samples": len(p), "pr_auc_definition": "average_precision_step_integral_ties_grouped", "windows": {}}
    for i, h in enumerate(HORIZONS):
        mask = v[:, :, i]
        target, score = y[:, :, i][mask], p[:, :, i][mask]
        per_app = {}
        for app, aid in app_vocab.items():
            if app.startswith("<"):
                continue
            m = mask[:, aid]
            yy, pp = y[:, aid, i][m], p[:, aid, i][m]
            per_app[app] = {"valid_labels": int(m.sum()), "positives": int(yy.sum()),
                "pr_auc": average_precision(yy, pp), "brier": float(np.mean((pp - yy) ** 2)) if len(yy) else None}
        aps = [r["pr_auc"] for r in per_app.values() if r["pr_auc"] is not None]
        result["windows"][str(h)] = {"valid_labels": int(mask.sum()), "positives": int(target.sum()),
            "pr_auc": average_precision(target, score), "macro_pr_auc": float(np.mean(aps)) if aps else None,
            "brier": float(np.mean((score - target) ** 2)) if len(target) else None, "per_app": per_app}
    eligible = np.asarray(eligible, bool)
    hot_set = eligible & (p[:, :, 0] >= hot)
    cold_set = eligible & (p[:, :, 1] < cold)
    known_hot, known_cold = hot_set & v[:, :, 0], cold_set & v[:, :, 1]
    positive_hot = y[:, :, 0] == 1
    result["thermal"] = {
        "hot_threshold": hot, "cold_threshold": cold,
        "eligible_background_app_samples": int(eligible.sum()),
        "hot_selected": int(hot_set.sum()), "hot_valid_labels": int(known_hot.sum()),
        "hot_precision": ratio((known_hot & positive_hot).sum(), known_hot.sum()),
        "hot_recall": ratio((known_hot & positive_hot).sum(), (eligible & v[:, :, 0] & positive_hot).sum()),
        "hot_coverage": ratio(hot_set.sum(), eligible.sum()),
        "cold_selected": int(cold_set.sum()), "cold_valid_labels": int(known_cold.sum()),
        "cold_actual_visit_rate": ratio((known_cold & (y[:, :, 1] == 1)).sum(), known_cold.sum()),
        "cold_coverage": ratio(cold_set.sum(), eligible.sum()),
        "neutral_coverage": ratio((eligible & ~hot_set & ~cold_set).sum(), eligible.sum()),
    }
    result["monotonicity_violations"] = int((p[:, :, 0] > p[:, :, 1]).sum())
    return result


@torch.no_grad()
def evaluate(model, data, batch_size, device, with_predictions=False):
    features, labels, masks, _ = data
    model.eval()
    sums = torch.zeros(2, dtype=torch.float64)
    counts = masks.sum((0, 1)).double()
    predictions = []
    for start in range(0, len(labels), batch_size):
        sl = slice(start, start + batch_size)
        logits = model(**{k: v[sl].to(device) for k, v in features.items()})
        lp, ln = log_probabilities(logits)
        target, valid = labels[sl].to(device), masks[sl].to(device)
        sums += (-(target * lp + (1 - target) * ln) * valid).sum((0, 1)).cpu().double()
        if with_predictions:
            predictions.append(visit_probabilities(logits).cpu().numpy())
    active = counts > 0
    loss = float((sums[active] / counts[active]).mean()) if active.any() else None
    return loss, np.concatenate(predictions) if predictions else None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset-dir", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--epochs", type=int, default=20)
    parser.add_argument("--batch-size", type=int, default=2048)
    parser.add_argument("--lr", type=float, default=1e-3)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--threads", type=int, default=2)
    parser.add_argument("--device", choices=["auto", "cpu", "cuda"], default="auto")
    args = parser.parse_args()
    if min(args.epochs, args.batch_size, args.threads, args.lr) <= 0:
        parser.error("epochs, batch size, threads and learning rate must be positive")
    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=False)
    torch.set_num_threads(args.threads)
    random.seed(args.seed)
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    meta = json.loads((Path(args.dataset_dir) / "dataset_meta.json").read_text())
    if meta["horizons_s"] != list(HORIZONS) or meta["visit_definition"] != VISIT_DEFINITION:
        raise ValueError("wrong dataset contract")
    vocab, groups = meta["app_vocab"], meta["group_vocab"]
    model_args = {"num_apps": len(vocab), "num_user_groups": max(groups.values()) + 1,
                  "pad_id": vocab["<PAD>"], "duration_cap_s": meta["args"]["duration_cap_s"]}
    device = torch.device("cuda" if args.device == "auto" and torch.cuda.is_available() else
                          "cpu" if args.device == "auto" else args.device)
    data = {}
    for split in ("train", "val", "test"):
        data[split] = read_data(Path(args.dataset_dir) / f"{split}.csv", vocab, groups, meta["args"]["history_len"])
        print(f"loaded {split}: {len(data[split][1])}", flush=True)
    model = AppLSTMVisitWindow(**model_args).to(device)
    optimizer = torch.optim.Adam(model.parameters(), lr=args.lr)
    best, best_epoch, history = float("inf"), 0, []
    started = time.monotonic()
    features, labels, masks, _ = data["train"]
    for epoch in range(1, args.epochs + 1):
        model.train()
        order = torch.randperm(len(labels))
        total, batches = 0., 0
        for start in range(0, len(labels), args.batch_size):
            idx = order[start:start + args.batch_size]
            valid = masks[idx].to(device)
            if not valid.any():
                continue
            logits = model(**{k: v[idx].to(device) for k, v in features.items()})
            loss = masked_loss(logits, labels[idx].to(device), valid)
            if not torch.isfinite(loss):
                raise ValueError("non-finite training loss")
            optimizer.zero_grad(set_to_none=True)
            loss.backward()
            optimizer.step()
            total += loss.item()
            batches += 1
        val_loss, _ = evaluate(model, data["val"], args.batch_size, device)
        row = {"epoch": epoch, "train_loss": total / max(1, batches), "val_loss": val_loss,
               "elapsed_s": time.monotonic() - started}
        history.append(row)
        print(json.dumps(row), flush=True)
        if val_loss is None:
            raise ValueError("validation has no observed labels")
        if val_loss < best:
            best, best_epoch = val_loss, epoch
            checkpoint = {"model_type": MODEL_TYPE, "schema_version": 1, "prediction_format": FORMAT,
                "horizons_s": list(HORIZONS), "visit_definition": VISIT_DEFINITION,
                "probability_parameterization": "p30_plus_survival30_times_sigmoid_z2",
                "model_args": model_args, "app_vocab": vocab, "group_vocab": groups,
                "history_len": meta["args"]["history_len"], "training_args": vars(args),
                "best_epoch": epoch, "validation_loss": best, "dataset_meta": meta,
                "model_state_dict": {k: v.detach().cpu() for k, v in model.state_dict().items()}}
            torch.save(checkpoint, out / "app_lstm_visit_window.pt")
        (out / "training_history.json").write_text(json.dumps(history, indent=2) + "\n")
    ckpt = torch.load(out / "app_lstm_visit_window.pt", map_location=device, weights_only=False)
    model.load_state_dict(ckpt["model_state_dict"])
    results = {"best_epoch": best_epoch, "training_seconds": time.monotonic() - started,
               "dataset_meta": meta, "splits": {}}
    for split in ("val", "test"):
        loss, probabilities = evaluate(model, data[split], args.batch_size, device, True)
        features, labels, valid, metadata = data[split]
        eligible = features["opened_apps"].numpy().astype(bool)
        eligible[np.arange(len(eligible)), features["current_app"].numpy()] = False
        report = metrics(probabilities, labels.numpy(), valid.numpy(), eligible, vocab)
        report["loss"] = loss
        results["splits"][split] = report
        np.savez_compressed(out / f"{split}_predictions.npz", probabilities=probabilities,
                            labels=labels.numpy(), valid=valid.numpy(), eligible=eligible,
                            app_names=np.array([a for a, _ in sorted(vocab.items(), key=lambda kv: kv[1])]))
        with (out / f"{split}_rows.jsonl").open("w") as stream:
            for row in metadata:
                stream.write(json.dumps(row, ensure_ascii=False) + "\n")
    (out / "evaluation.json").write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({split: {"loss": r["loss"], "thermal": r["thermal"]}
                     for split, r in results["splits"].items()}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
