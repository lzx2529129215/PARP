#!/usr/bin/env python3
"""Train and evaluate the v1 application-level Markov baseline."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.eval.metrics import hit_at_k, mrr, precision_at_k, recall_at_k
from v1.models.app_markov import AppMarkovV1
from src.utils.io_utils import load_pickle, save_csv


def evaluate(model: AppMarkovV1, test_samples: list[dict], top_k: list[int], horizons: list[int]) -> list[dict]:
    rows: list[dict] = []
    for horizon in horizons:
        label_key = f"label_{horizon}min"
        valid_samples = [sample for sample in test_samples if label_key in sample]
        for k in top_k:
            hit_sum = recall_sum = precision_sum = mrr_sum = 0.0
            used = 0
            for sample in valid_samples:
                labels = sample[label_key]
                preds = model.predict_topk(sample["history_apps"], horizon=horizon, k=k)
                if not preds:
                    continue
                hit_sum += hit_at_k(preds, labels)
                recall_sum += recall_at_k(preds, labels)
                precision_sum += precision_at_k(preds, labels)
                mrr_sum += mrr(preds, labels)
                used += 1
            if used:
                rows.append(
                    {
                        "version": "v1",
                        "model": "app_markov",
                        "order": model.order,
                        "horizon": horizon,
                        "k": k,
                        "hit_at_k": hit_sum / used,
                        "recall_at_k": recall_sum / used,
                        "precision_at_k": precision_sum / used,
                        "mrr": mrr_sum / used,
                        "num_samples": used,
                    }
                )
    return rows


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Train v1 application Markov baseline.")
    parser.add_argument("--train", default="data/processed/train_app.pkl")
    parser.add_argument("--test", default="data/processed/test_app.pkl")
    parser.add_argument("--order", type=int, choices=[1, 2, 3, 4], default=4)
    parser.add_argument("--top-k", nargs="+", type=int, default=[1, 3, 5])
    parser.add_argument("--horizons", nargs="+", type=int, default=[3, 5, 10])
    parser.add_argument("--output", default="outputs/results/v1/app_markov_results.csv")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    train_samples = load_pickle(args.train)
    test_samples = load_pickle(args.test)
    if not train_samples or not test_samples:
        raise ValueError("train/test samples must be non-empty")

    model = AppMarkovV1(order=args.order)
    model.fit(train_samples, horizons=args.horizons)
    rows = evaluate(model, test_samples, args.top_k, args.horizons)
    if not rows:
        raise ValueError("no evaluation rows generated")

    fieldnames = [
        "version",
        "model",
        "order",
        "horizon",
        "k",
        "hit_at_k",
        "recall_at_k",
        "precision_at_k",
        "mrr",
        "num_samples",
    ]
    save_csv(rows, args.output, fieldnames)
    print(f"train samples: {len(train_samples)}")
    print(f"test samples: {len(test_samples)}")
    print(f"contexts: {model.context_count()}")
    print(f"saved: {args.output}")


if __name__ == "__main__":
    main()
