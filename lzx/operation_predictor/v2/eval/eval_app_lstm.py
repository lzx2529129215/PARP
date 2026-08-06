#!/usr/bin/env python3
"""Evaluate a trained v2 application LSTM checkpoint without training."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    import torch
    from torch.utils.data import DataLoader
except ModuleNotFoundError as exc:  # pragma: no cover - depends on local environment
    raise SystemExit(
        "PyTorch is required for LSTM v2 evaluation. Install dependencies first, for example: "
        "pip install -r requirements.txt"
    ) from exc

from src.utils.io_utils import ensure_dir, load_json, load_pickle, save_csv
from v2.models.app_lstm import AppLSTMV2
from v2.train.train_app_lstm import AppSampleDataset, collate_batch, evaluate


def load_checkpoint(path: str | Path, device: torch.device) -> dict[str, Any]:
    try:
        return torch.load(path, map_location=device, weights_only=False)
    except TypeError:
        return torch.load(path, map_location=device)


def build_model(checkpoint: dict[str, Any], device: torch.device) -> AppLSTMV2:
    ckpt_args = checkpoint.get("args", {})
    horizons = [int(horizon) for horizon in checkpoint.get("horizons", ckpt_args.get("horizons", [3, 5, 10]))]
    model = AppLSTMV2(
        num_apps=int(checkpoint["num_apps"]),
        num_user_groups=int(checkpoint["num_user_groups"]),
        horizons=horizons,
        app_embedding_dim=int(ckpt_args.get("app_embedding_dim", 32)),
        group_embedding_dim=int(ckpt_args.get("group_embedding_dim", 8)),
        hidden_dim=int(ckpt_args.get("hidden_dim", 64)),
        opened_dim=int(ckpt_args.get("opened_dim", 32)),
        dropout=float(ckpt_args.get("dropout", 0.2)),
    ).to(device)
    model.load_state_dict(checkpoint["model_state_dict"])
    model.eval()
    return model


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Evaluate a trained v2 application LSTM checkpoint.")
    parser.add_argument("--checkpoint", default="outputs/checkpoints/app_lstm/lsapp_app_lstm.pt")
    parser.add_argument("--test", default="data/processed/lsapp/test_app.pkl")
    parser.add_argument("--app-vocab", default="data/vocab/app_vocab.json")
    parser.add_argument("--top-k", nargs="+", type=int, default=[1, 3, 5])
    parser.add_argument("--batch-size", type=int, default=2048)
    parser.add_argument("--device", default="auto", choices=["auto", "cpu", "cuda"])
    parser.add_argument("--output", default="outputs/results/v2/lsapp_app_lstm_eval_results.csv")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.device == "auto":
        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    else:
        device = torch.device(args.device)

    checkpoint = load_checkpoint(args.checkpoint, device)
    horizons = [int(horizon) for horizon in checkpoint.get("horizons", checkpoint.get("args", {}).get("horizons", [3, 5, 10]))]
    app_vocab = load_json(args.app_vocab)
    test_samples = load_pickle(args.test)
    if not test_samples:
        raise ValueError(f"test samples are empty: {args.test}")

    if len(app_vocab) != int(checkpoint["num_apps"]):
        raise ValueError(
            f"app vocab size mismatch: vocab={len(app_vocab)} checkpoint={checkpoint['num_apps']}"
        )

    model = build_model(checkpoint, device)
    test_dataset = AppSampleDataset(test_samples, horizons, len(app_vocab))
    test_loader = DataLoader(
        test_dataset,
        batch_size=args.batch_size,
        shuffle=False,
        collate_fn=collate_batch,
    )

    rows = evaluate(model, test_loader, horizons, args.top_k, device)
    if not rows:
        raise ValueError("no evaluation rows generated")

    for row in rows:
        row["checkpoint"] = args.checkpoint
        row["split"] = args.test

    fieldnames = [
        "version",
        "model",
        "horizon",
        "k",
        "hit_at_k",
        "recall_at_k",
        "precision_at_k",
        "mrr",
        "num_samples",
        "checkpoint",
        "split",
    ]
    save_csv(rows, args.output, fieldnames)
    print(f"test samples: {len(test_samples)}")
    print(f"horizons: {horizons}")
    print(f"top_k: {args.top_k}")
    print(f"device: {device}")
    print(f"checkpoint: {args.checkpoint}")
    print(f"saved results: {args.output}")


if __name__ == "__main__":
    main()
