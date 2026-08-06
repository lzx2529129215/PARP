#!/usr/bin/env python3
"""Train and evaluate the in-app operation GAM experiment group."""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.eval.metrics import mrr, topk_accuracy
from src.models.op_gam import OpGamModel
from src.utils.io_utils import load_json, load_pickle, save_csv


def evaluate(model: OpGamModel, test_samples: list[dict], top_k: list[int]) -> list[dict]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for sample in test_samples:
        grouped[sample["app"]].append(sample)

    rows: list[dict] = []
    all_stats = {k: {"acc": 0.0, "mrr": 0.0, "n": 0} for k in top_k}
    for app, samples in sorted(grouped.items()):
        for k in top_k:
            acc_sum = mrr_sum = 0.0
            used = 0
            for sample in samples:
                label = int(sample["label_next_op"])
                special_ids = model._special_ids(app)
                if label in special_ids:
                    continue
                preds = model.predict_topk(app, sample["history_ops"], k=k)
                if not preds:
                    continue
                acc_sum += topk_accuracy(preds, label, ignore_labels=special_ids)
                mrr_sum += mrr(preds, {label}, ignore_labels=special_ids)
                used += 1
            if not used:
                continue
            rows.append(
                {
                    "app": app,
                    "model": "op_gam",
                    "order": "",
                    "k": k,
                    "topk_accuracy": acc_sum / used,
                    "mrr": mrr_sum / used,
                    "num_samples": used,
                }
            )
            all_stats[k]["acc"] += acc_sum
            all_stats[k]["mrr"] += mrr_sum
            all_stats[k]["n"] += used

    for k in top_k:
        n = all_stats[k]["n"]
        if n:
            rows.append(
                {
                    "app": "ALL",
                    "model": "op_gam",
                    "order": "",
                    "k": k,
                    "topk_accuracy": all_stats[k]["acc"] / n,
                    "mrr": all_stats[k]["mrr"] / n,
                    "num_samples": n,
                }
            )
    return rows


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Train in-app operation GAM experiment group.")
    parser.add_argument("--train", default="data/processed/train_op.pkl")
    parser.add_argument("--test", default="data/processed/test_op.pkl")
    parser.add_argument("--op-vocab", default="data/vocab/op_vocab.json")
    parser.add_argument("--smoothing", type=float, default=1.0)
    parser.add_argument("--top-k", nargs="+", type=int, default=[1, 3, 5])
    parser.add_argument("--output", default="outputs/results/op_gam_results.csv")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    train_samples = load_pickle(args.train)
    test_samples = load_pickle(args.test)
    if not train_samples or not test_samples:
        raise ValueError("train/test samples must be non-empty")

    op_vocab = load_json(args.op_vocab)
    model = OpGamModel(op_vocab=op_vocab, smoothing=args.smoothing)
    model.fit(train_samples)
    rows = evaluate(model, test_samples, args.top_k)
    if not rows:
        raise ValueError("no evaluation rows generated")

    fieldnames = ["app", "model", "order", "k", "topk_accuracy", "mrr", "num_samples"]
    save_csv(rows, args.output, fieldnames)
    print(f"train samples: {len(train_samples)}")
    print(f"test samples: {len(test_samples)}")
    print(f"apps in train: {len(model.label_counts)}")
    print(f"smoothing: {model.smoothing}")
    print(f"saved: {args.output}")


if __name__ == "__main__":
    main()
