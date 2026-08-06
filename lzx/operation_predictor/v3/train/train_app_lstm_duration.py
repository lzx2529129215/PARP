#!/usr/bin/env python3
"""Train and evaluate the duration-aware App LSTM v3."""

from __future__ import annotations

import argparse
import csv
import json
import random
import sys
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    import torch
    from torch import nn
    from torch.utils.data import DataLoader, Dataset
except ModuleNotFoundError as exc:  # pragma: no cover
    raise SystemExit("PyTorch is required for duration-aware LSTM training.") from exc

from v3.infer.infer_app_lstm_duration import parse_time, time_feature
from v3.models.app_lstm_duration import AppLSTMDurationV3


HORIZONS = [3, 5, 10]


def load_json(path: str | Path) -> dict[str, int]:
    return {key: int(value) for key, value in json.loads(Path(path).read_text(encoding="utf-8")).items()}


def split_pipe(value: str | None) -> list[str]:
    return [item for item in (value or "").split("|") if item]


def load_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        raise FileNotFoundError(f"duration dataset split not found: {path}")
    with path.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


class DurationDataset(Dataset):
    def __init__(self, rows: list[dict[str, str]], app_vocab: dict[str, int], group_vocab: dict[str, int], label_mode: str) -> None:
        self.rows = rows
        self.app_vocab = app_vocab
        self.group_vocab = group_vocab
        self.label_mode = label_mode
        self.unknown_id = app_vocab["<UNKNOWN>"]

    def __len__(self) -> int:
        return len(self.rows)

    def ids_for_apps(self, apps: list[str]) -> list[int]:
        return [self.app_vocab.get(app, self.unknown_id) for app in apps]

    def vec_for_apps(self, apps: list[str], include_unknown: bool = False) -> list[float]:
        vec = [0.0] * len(self.app_vocab)
        for app in apps:
            if app == "<PAD>" or (app == "<UNKNOWN>" and not include_unknown):
                continue
            app_id = self.app_vocab.get(app)
            if app_id is not None:
                vec[app_id] = 1.0
        return vec

    def current_app(self, row: dict[str, str]) -> str:
        if row.get("current_app"):
            return row["current_app"]
        apps = split_pipe(row.get("history_apps"))
        masks = split_pipe(row.get("history_mask"))
        for app, mask in reversed(list(zip(apps, masks))):
            if mask == "1":
                return app
        return ""

    def label_apps(self, row: dict[str, str], horizon: int) -> list[str]:
        if self.label_mode == "persistence":
            return split_pipe(row.get(f"labels_{horizon}"))
        field = f"labels_next_{horizon}"
        if field in row:
            return split_pipe(row.get(field))
        cur = self.current_app(row)
        return [app for app in split_pipe(row.get(f"labels_{horizon}")) if app != cur]

    def has_next(self, row: dict[str, str], horizon: int, apps: list[str]) -> float:
        field = f"has_next_{horizon}"
        if field in row:
            return 1.0 if row.get(field) == "1" else 0.0
        return 1.0 if apps else 0.0

    def __getitem__(self, idx: int) -> dict[str, Any]:
        row = self.rows[idx]
        group_name = row.get("user_group_name", "通用用户")
        user_group = int(row["user_group"]) if row.get("user_group", "").isdigit() else self.group_vocab.get(group_name, 0)
        labels: dict[int, list[float]] = {}
        has_next: dict[int, float] = {}
        for horizon in HORIZONS:
            apps = self.label_apps(row, horizon)
            labels[horizon] = self.vec_for_apps(apps)
            has_next[horizon] = self.has_next(row, horizon, apps)
        return {
            "history_apps": torch.tensor(self.ids_for_apps(split_pipe(row["history_apps"])), dtype=torch.long),
            "history_durations": torch.tensor([float(item) for item in split_pipe(row["history_durations_s"])], dtype=torch.float32),
            "history_mask": torch.tensor([float(item) for item in split_pipe(row["history_mask"])], dtype=torch.float32),
            "opened_apps": torch.tensor(self.vec_for_apps(split_pipe(row.get("opened_apps"))), dtype=torch.float32),
            "time_feature": torch.tensor(time_feature(parse_time(row["timestamp"])), dtype=torch.float32),
            "user_group": torch.tensor(user_group, dtype=torch.long),
            "labels": {horizon: torch.tensor(labels[horizon], dtype=torch.float32) for horizon in HORIZONS},
            "has_next": {horizon: torch.tensor(has_next[horizon], dtype=torch.float32) for horizon in HORIZONS},
        }


def collate(batch: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "history_apps": torch.stack([item["history_apps"] for item in batch]),
        "history_durations": torch.stack([item["history_durations"] for item in batch]),
        "history_mask": torch.stack([item["history_mask"] for item in batch]),
        "opened_apps": torch.stack([item["opened_apps"] for item in batch]),
        "time_feature": torch.stack([item["time_feature"] for item in batch]),
        "user_group": torch.stack([item["user_group"] for item in batch]),
        "labels": {horizon: torch.stack([item["labels"][horizon] for item in batch]) for horizon in HORIZONS},
        "has_next": {horizon: torch.stack([item["has_next"][horizon] for item in batch]) for horizon in HORIZONS},
    }


def move_batch(batch: dict[str, Any], device: torch.device) -> dict[str, Any]:
    return {
        "history_apps": batch["history_apps"].to(device),
        "history_durations": batch["history_durations"].to(device),
        "history_mask": batch["history_mask"].to(device),
        "opened_apps": batch["opened_apps"].to(device),
        "time_feature": batch["time_feature"].to(device),
        "user_group": batch["user_group"].to(device),
        "labels": {horizon: labels.to(device) for horizon, labels in batch["labels"].items()},
        "has_next": {horizon: flags.to(device) for horizon, flags in batch["has_next"].items()},
    }


def set_seed(seed: int) -> None:
    random.seed(seed)
    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(seed)


def metric_for_topk(preds: list[int], label_vec: list[int]) -> tuple[float, float, float, float]:
    true = {idx for idx, value in enumerate(label_vec) if value}
    if not true:
        return 0.0, 0.0, 0.0, 0.0
    pred_set = set(preds)
    overlap = len(pred_set & true)
    hit = float(overlap > 0)
    precision = overlap / len(preds) if preds else 0.0
    recall = overlap / len(true)
    rr = 0.0
    for rank, app_id in enumerate(preds, start=1):
        if app_id in true:
            rr = 1.0 / rank
            break
    return hit, recall, precision, rr


def add_metrics(bucket: dict[str, float], preds: list[int], label_vec: list[int]) -> None:
    hit, recall, precision, rr = metric_for_topk(preds, label_vec)
    bucket["hit"] += hit
    bucket["recall"] += recall
    bucket["precision"] += precision
    bucket["mrr"] += rr
    bucket["n"] += 1.0


@torch.no_grad()
def evaluate(
    model: AppLSTMDurationV3,
    loader: DataLoader,
    top_k: list[int],
    device: torch.device,
    split_name: str,
    label_mode: str,
) -> list[dict[str, Any]]:
    model.eval()
    stats: dict[tuple[int, int, str], dict[str, float]] = {
        (horizon, k, subset): {"hit": 0.0, "recall": 0.0, "precision": 0.0, "mrr": 0.0, "n": 0.0}
        for horizon in HORIZONS for k in top_k for subset in ["all", "has_next"]
    }
    total_by_horizon: Counter[int] = Counter()
    has_next_by_horizon: Counter[int] = Counter()
    max_k = max(top_k)
    for batch in loader:
        batch = move_batch(batch, device)
        outputs = model(
            batch["history_apps"],
            batch["history_durations"],
            batch["history_mask"],
            batch["opened_apps"],
            batch["time_feature"],
            batch["user_group"],
        )
        for horizon in HORIZONS:
            scores = torch.sigmoid(outputs[horizon])
            ranked = torch.topk(scores, k=min(max_k, scores.shape[1]), dim=1).indices.cpu().tolist()
            labels = batch["labels"][horizon].cpu().int().tolist()
            flags = batch["has_next"][horizon].cpu().int().tolist()
            for preds, label_vec, has_next in zip(ranked, labels, flags):
                total_by_horizon[horizon] += 1
                has_next_by_horizon[horizon] += int(has_next == 1)
                for k in top_k:
                    add_metrics(stats[(horizon, k, "all")], preds[:k], label_vec)
                    if has_next == 1:
                        add_metrics(stats[(horizon, k, "has_next")], preds[:k], label_vec)

    rows: list[dict[str, Any]] = []
    for horizon in HORIZONS:
        empty_ratio = 1.0 - (has_next_by_horizon[horizon] / total_by_horizon[horizon]) if total_by_horizon[horizon] else 0.0
        for subset in ["all", "has_next"]:
            for k in top_k:
                item = stats[(horizon, k, subset)]
                n = max(1.0, item["n"])
                rows.append({
                    "version": "v3",
                    "model": "app_lstm_duration",
                    "label_mode": label_mode,
                    "eval_subset": subset,
                    "split": split_name,
                    "horizon": horizon,
                    "k": k,
                    "hit_at_k": item["hit"] / n,
                    "recall_at_k": item["recall"] / n,
                    "precision_at_k": item["precision"] / n,
                    "mrr": item["mrr"] / n,
                    "num_samples": int(item["n"]),
                    "empty_next_ratio": empty_ratio if label_mode == "switch" else 0.0,
                })
    return rows


def train_one_epoch(model: AppLSTMDurationV3, loader: DataLoader, criterion: nn.Module, optimizer: torch.optim.Optimizer, device: torch.device) -> float:
    model.train()
    total = 0.0
    batches = 0
    for batch in loader:
        batch = move_batch(batch, device)
        outputs = model(
            batch["history_apps"],
            batch["history_durations"],
            batch["history_mask"],
            batch["opened_apps"],
            batch["time_feature"],
            batch["user_group"],
        )
        loss = sum(criterion(outputs[horizon], batch["labels"][horizon]) for horizon in HORIZONS) / len(HORIZONS)
        optimizer.zero_grad()
        loss.backward()
        optimizer.step()
        total += float(loss.item())
        batches += 1
    return total / max(1, batches)


def duration_values(rows: list[dict[str, str]]) -> list[float]:
    values: list[float] = []
    for row in rows:
        for value, mask in zip(split_pipe(row.get("history_durations_s")), split_pipe(row.get("history_mask"))):
            if mask == "1":
                values.append(float(value))
    return values


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    values = sorted(values)
    idx = min(len(values) - 1, max(0, int(round((len(values) - 1) * p))))
    return values[idx]


def unknown_history_ratio(rows: list[dict[str, str]]) -> float:
    total = 0
    unknown = 0
    for row in rows:
        for app, mask in zip(split_pipe(row.get("history_apps")), split_pipe(row.get("history_mask"))):
            if mask == "1":
                total += 1
                unknown += int(app == "<UNKNOWN>")
    return unknown / total if total else 0.0


def write_results(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "version", "model", "label_mode", "eval_subset", "split", "horizon", "k",
        "hit_at_k", "recall_at_k", "precision_at_k", "mrr", "num_samples", "empty_next_ratio",
    ]
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def default_checkpoint_name(label_mode: str) -> str:
    return f"lsapp_app_lstm_duration_{label_mode}.pt"


def default_results_path(label_mode: str) -> str:
    return f"huawei_mem/lzx/operation_predictor/outputs/results/v3/lsapp_app_lstm_duration_{label_mode}_results.csv"


def main() -> None:
    args = parse_args()
    if args.checkpoint_name is None:
        args.checkpoint_name = default_checkpoint_name(args.label_mode)
    if args.output is None:
        args.output = default_results_path(args.label_mode)

    set_seed(args.seed)
    app_vocab = load_json(args.app_vocab)
    group_vocab = load_json(args.group_vocab)
    pad_id = app_vocab["<PAD>"]
    unknown_id = app_vocab["<UNKNOWN>"]

    dataset_dir = Path(args.dataset_dir)
    train_rows = load_csv(dataset_dir / "train.csv")
    val_rows = load_csv(dataset_dir / "val.csv")
    test_rows = load_csv(dataset_dir / "test.csv")
    if not train_rows or not val_rows or not test_rows:
        raise ValueError("train/val/test duration datasets must be non-empty")
    meta_path = dataset_dir / "dataset_meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}

    all_rows = train_rows + val_rows + test_rows
    values = duration_values(all_rows)
    trigger_counts: Counter[str] = Counter()
    for row in all_rows:
        anchor = row.get("anchor_type") or row.get("trigger_type", "")
        if anchor.startswith("periodic_refresh"):
            trigger_counts["periodic_refresh"] += 1
        elif anchor.startswith("dwell_bucket_cross"):
            trigger_counts["dwell_bucket_cross"] += 1
        elif anchor.startswith("foreground_transition"):
            trigger_counts["foreground_transition"] += 1
        else:
            trigger_counts[anchor] += 1
    print(f"label_mode={args.label_mode}")
    print(f"num_apps={len(app_vocab)}")
    print(f"pad_id={pad_id}")
    print(f"unknown_id={unknown_id}")
    print(f"history_len={args.history_len}")
    print(f"duration_cap_s={args.duration_cap_s}")
    print(f"train/val/test samples={len(train_rows)}/{len(val_rows)}/{len(test_rows)}")
    print(f"foreground_transition anchors={trigger_counts['foreground_transition']}")
    print(f"periodic_refresh anchors={trigger_counts['periodic_refresh']}")
    print(f"dwell_bucket_cross anchors={trigger_counts['dwell_bucket_cross']}")
    print(f"<UNKNOWN> history ratio={unknown_history_ratio(all_rows):.6f}")
    print(
        "duration stats="
        f"min:{min(values) if values else 0} "
        f"p50:{percentile(values, 0.50)} "
        f"p90:{percentile(values, 0.90)} "
        f"p99:{percentile(values, 0.99)} "
        f"max:{max(values) if values else 0}"
    )

    device = torch.device("cuda" if args.device == "auto" and torch.cuda.is_available() else ("cpu" if args.device == "auto" else args.device))
    train_dataset = DurationDataset(train_rows, app_vocab, group_vocab, args.label_mode)
    val_dataset = DurationDataset(val_rows, app_vocab, group_vocab, args.label_mode)
    test_dataset = DurationDataset(test_rows, app_vocab, group_vocab, args.label_mode)
    train_loader = DataLoader(train_dataset, batch_size=args.batch_size, shuffle=True, collate_fn=collate)
    val_loader = DataLoader(val_dataset, batch_size=args.batch_size, shuffle=False, collate_fn=collate)
    test_loader = DataLoader(test_dataset, batch_size=args.batch_size, shuffle=False, collate_fn=collate)

    model = AppLSTMDurationV3(
        num_apps=len(app_vocab),
        num_user_groups=max(group_vocab.values()) + 1,
        horizons=HORIZONS,
        pad_id=pad_id,
        duration_cap_s=args.duration_cap_s,
        app_embedding_dim=args.app_embedding_dim,
        duration_embedding_dim=args.duration_embedding_dim,
        group_embedding_dim=args.group_embedding_dim,
        hidden_dim=args.hidden_dim,
        opened_dim=args.opened_dim,
        dropout=args.dropout,
    ).to(device)
    criterion = nn.BCEWithLogitsLoss()
    optimizer = torch.optim.Adam(model.parameters(), lr=args.lr)

    last_loss = 0.0
    for epoch in range(1, args.epochs + 1):
        last_loss = train_one_epoch(model, train_loader, criterion, optimizer, device)
        print(f"epoch {epoch}/{args.epochs} train_loss={last_loss:.6f}")

    result_rows: list[dict[str, Any]] = []
    result_rows.extend(evaluate(model, val_loader, args.top_k, device, "val", args.label_mode))
    result_rows.extend(evaluate(model, test_loader, args.top_k, device, "test", args.label_mode))
    for row in result_rows:
        print(
            f"{row['split']} {row['eval_subset']} horizon={row['horizon']} k={row['k']} "
            f"Hit@K={row['hit_at_k']:.6f} Recall@K={row['recall_at_k']:.6f} "
            f"Precision@K={row['precision_at_k']:.6f} MRR={row['mrr']:.6f} "
            f"empty_next_ratio={row['empty_next_ratio']:.6f}"
        )

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    checkpoint_path = output_dir / args.checkpoint_name
    payload = {
        "model_state_dict": model.state_dict(),
        "args": vars(args),
        "dataset_meta": meta,
        "num_apps": len(app_vocab),
        "num_user_groups": max(group_vocab.values()) + 1,
        "horizons": HORIZONS,
        "pad_id": pad_id,
        "unknown_id": unknown_id,
        "duration_cap_s": args.duration_cap_s,
        "label_mode": args.label_mode,
    }
    torch.save(payload, checkpoint_path)
    if args.label_mode == "persistence":
        torch.save(payload, output_dir / "lsapp_app_lstm_duration.pt")
    write_results(Path(args.output), result_rows)
    print(f"checkpoint saved: {checkpoint_path}")
    print(f"results saved: {args.output}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Train duration-aware App LSTM v3.")
    parser.add_argument("--dataset-dir", default="huawei_mem/lzx/operation_predictor/data/processed/app_lstm_duration_gap3600_periodic180")
    parser.add_argument("--app-vocab", default="huawei_mem/lzx/operation_predictor/data/vocab/app_vocab_duration.json")
    parser.add_argument("--group-vocab", default="huawei_mem/lzx/operation_predictor/data/vocab/user_group_vocab.json")
    parser.add_argument("--history-len", type=int, default=5)
    parser.add_argument("--duration-cap-s", type=float, default=600.0)
    parser.add_argument("--label-mode", choices=["persistence", "switch"], default="persistence")
    parser.add_argument("--batch-size", type=int, default=256)
    parser.add_argument("--epochs", type=int, default=20)
    parser.add_argument("--lr", type=float, default=1e-3)
    parser.add_argument("--app-embedding-dim", type=int, default=32)
    parser.add_argument("--duration-embedding-dim", type=int, default=8)
    parser.add_argument("--group-embedding-dim", type=int, default=8)
    parser.add_argument("--hidden-dim", type=int, default=64)
    parser.add_argument("--opened-dim", type=int, default=32)
    parser.add_argument("--dropout", type=float, default=0.2)
    parser.add_argument("--top-k", nargs="+", type=int, default=[1, 3, 5])
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--device", choices=["auto", "cpu", "cuda"], default="auto")
    parser.add_argument("--output-dir", default="huawei_mem/lzx/operation_predictor/outputs/checkpoints/app_lstm_duration")
    parser.add_argument("--checkpoint-name", default=None)
    parser.add_argument("--output", default=None)
    return parser.parse_args()


if __name__ == "__main__":
    main()
