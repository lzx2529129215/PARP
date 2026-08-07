#!/usr/bin/env python3
"""Train and validate the v2 application-level LSTM model."""

from __future__ import annotations

import argparse
import random
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    import torch
    from torch import nn
    from torch.utils.data import DataLoader, Dataset
except ModuleNotFoundError as exc:  # pragma: no cover - depends on local environment
    raise SystemExit(
        "PyTorch is required for LSTM v2. Install dependencies first, for example: "
        "pip install -r requirements.txt"
    ) from exc

from src.eval.metrics import hit_at_k, mrr, precision_at_k, recall_at_k
from v2.models.app_lstm import AppLSTMV2
from src.utils.io_utils import ensure_dir, load_json, load_pickle, save_csv


class AppSampleDataset(Dataset):
    def __init__(self, samples: list[dict[str, Any]], horizons: list[int], num_apps: int) -> None:
        self.samples = samples
        self.horizons = horizons
        self.num_apps = num_apps

    def __len__(self) -> int:
        return len(self.samples)

    @staticmethod
    def _time_feature(values: list[int]) -> list[float]:
        hour, weekday, is_weekend = values
        return [float(hour) / 23.0, float(weekday) / 6.0, float(is_weekend)]

    def __getitem__(self, idx: int) -> dict[str, torch.Tensor]:
        sample = self.samples[idx]
        labels = {
            str(horizon): torch.tensor(sample[f"label_{horizon}min"], dtype=torch.float32)
            for horizon in self.horizons
        }
        return {
            "history_apps": torch.tensor(sample["history_apps"], dtype=torch.long),
            "opened_apps": torch.tensor(sample["opened_apps"], dtype=torch.float32),
            "time_feature": torch.tensor(self._time_feature(sample["time_feature"]), dtype=torch.float32),
            "user_group": torch.tensor(int(sample["user_group"]), dtype=torch.long),
            "labels": labels,
        }


def set_seed(seed: int) -> None:
    random.seed(seed)
    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(seed)


def collate_batch(batch: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "history_apps": torch.stack([item["history_apps"] for item in batch]),
        "opened_apps": torch.stack([item["opened_apps"] for item in batch]),
        "time_feature": torch.stack([item["time_feature"] for item in batch]),
        "user_group": torch.stack([item["user_group"] for item in batch]),
        "labels": {
            horizon: torch.stack([item["labels"][horizon] for item in batch])
            for horizon in batch[0]["labels"]
        },
    }


def move_batch(batch: dict[str, Any], device: torch.device) -> dict[str, Any]:
    return {
        "history_apps": batch["history_apps"].to(device),
        "opened_apps": batch["opened_apps"].to(device),
        "time_feature": batch["time_feature"].to(device),
        "user_group": batch["user_group"].to(device),
        "labels": {horizon: labels.to(device) for horizon, labels in batch["labels"].items()},
    }


def train_one_epoch(
    model: AppLSTMV2,
    loader: DataLoader,
    criterion: nn.Module,
    optimizer: torch.optim.Optimizer,
    horizons: list[int],
    device: torch.device,
) -> float:
    model.train()
    total_loss = 0.0
    total_batches = 0
    for batch in loader:
        batch = move_batch(batch, device)
        outputs = model(
            batch["history_apps"],
            batch["opened_apps"],
            batch["time_feature"],
            batch["user_group"],
        )
        loss = sum(criterion(outputs[horizon], batch["labels"][str(horizon)]) for horizon in horizons) / len(horizons)
        optimizer.zero_grad()
        loss.backward()
        optimizer.step()
        total_loss += float(loss.item())
        total_batches += 1
    return total_loss / max(total_batches, 1)


@torch.no_grad()
def evaluate(
    model: AppLSTMV2,
    loader: DataLoader,
    horizons: list[int],
    top_k: list[int],
    device: torch.device,
) -> list[dict[str, Any]]:
    model.eval()
    stats: dict[tuple[int, int], dict[str, float]] = {
        (horizon, k): {"hit": 0.0, "recall": 0.0, "precision": 0.0, "mrr": 0.0, "n": 0.0}
        for horizon in horizons
        for k in top_k
    }
    max_k = max(top_k)
    for batch in loader:
        batch = move_batch(batch, device)
        outputs = model(
            batch["history_apps"],
            batch["opened_apps"],
            batch["time_feature"],
            batch["user_group"],
        )
        for horizon in horizons:
            scores = torch.sigmoid(outputs[horizon])
            pred_ranked = torch.topk(scores, k=max_k, dim=1).indices.cpu().tolist()
            labels = batch["labels"][str(horizon)].cpu().int().tolist()
            for preds, label_vec in zip(pred_ranked, labels):
                for k in top_k:
                    pred_topk = preds[:k]
                    key = (horizon, k)
                    stats[key]["hit"] += hit_at_k(pred_topk, label_vec)
                    stats[key]["recall"] += recall_at_k(pred_topk, label_vec)
                    stats[key]["precision"] += precision_at_k(pred_topk, label_vec)
                    stats[key]["mrr"] += mrr(pred_topk, label_vec)
                    stats[key]["n"] += 1.0

    rows: list[dict[str, Any]] = []
    for horizon in horizons:
        for k in top_k:
            item = stats[(horizon, k)]
            n = int(item["n"])
            if not n:
                continue
            rows.append(
                {
                    "version": "v2",
                    "model": "app_lstm",
                    "horizon": horizon,
                    "k": k,
                    "hit_at_k": item["hit"] / n,
                    "recall_at_k": item["recall"] / n,
                    "precision_at_k": item["precision"] / n,
                    "mrr": item["mrr"] / n,
                    "num_samples": n,
                }
            )
    return rows


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Train v2 application LSTM model.")
    parser.add_argument("--train", default="data/processed/train_app.pkl")
    parser.add_argument("--val", default="data/processed/val_app.pkl")
    parser.add_argument(
        "--test",
        help="Deprecated. Do not use test data during training; use v2/eval/eval_app_lstm.py after training.",
    )
    parser.add_argument("--app-vocab", default="data/vocab/app_vocab.json")
    parser.add_argument("--group-vocab", default="data/vocab/user_group_vocab.json")
    parser.add_argument("--horizons", nargs="+", type=int, default=[3, 5, 10])
    parser.add_argument("--top-k", nargs="+", type=int, default=[1, 3, 5])
    parser.add_argument("--epochs", type=int, default=20)
    parser.add_argument("--batch-size", type=int, default=32)
    parser.add_argument("--lr", type=float, default=1e-3)
    parser.add_argument("--app-embedding-dim", type=int, default=32)
    parser.add_argument("--group-embedding-dim", type=int, default=8)
    parser.add_argument("--hidden-dim", type=int, default=64)
    parser.add_argument("--opened-dim", type=int, default=32)
    parser.add_argument("--dropout", type=float, default=0.2)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--device", default="auto", choices=["auto", "cpu", "cuda"])
    parser.add_argument("--output", default="outputs/results/v2/app_lstm_val_results.csv")
    parser.add_argument("--checkpoint", default="outputs/checkpoints/v2/app_lstm.pt")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    set_seed(args.seed)
    if args.test:
        raise ValueError(
            "--test is not allowed in training. Train with --val only, then evaluate the final checkpoint "
            "with v2/eval/eval_app_lstm.py."
        )

    app_vocab = load_json(args.app_vocab)
    group_vocab = load_json(args.group_vocab)
    train_samples = load_pickle(args.train)
    val_samples = load_pickle(args.val)
    if not train_samples or not val_samples:
        raise ValueError("train/val samples must be non-empty")

    num_apps = len(app_vocab)
    num_user_groups = max(int(value) for value in group_vocab.values()) + 1
    if args.device == "auto":
        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    else:
        device = torch.device(args.device)

    train_dataset = AppSampleDataset(train_samples, args.horizons, num_apps)
    val_dataset = AppSampleDataset(val_samples, args.horizons, num_apps)
    train_loader = DataLoader(
        train_dataset,
        batch_size=args.batch_size,
        shuffle=True,
        collate_fn=collate_batch,
    )
    val_loader = DataLoader(
        val_dataset,
        batch_size=args.batch_size,
        shuffle=False,
        collate_fn=collate_batch,
    )

    model = AppLSTMV2(
        num_apps=num_apps,
        num_user_groups=num_user_groups,
        horizons=args.horizons,
        app_embedding_dim=args.app_embedding_dim,
        group_embedding_dim=args.group_embedding_dim,
        hidden_dim=args.hidden_dim,
        opened_dim=args.opened_dim,
        dropout=args.dropout,
    ).to(device)
    criterion = nn.BCEWithLogitsLoss()
    optimizer = torch.optim.Adam(model.parameters(), lr=args.lr)

    last_loss = 0.0
    for epoch in range(1, args.epochs + 1):
        last_loss = train_one_epoch(model, train_loader, criterion, optimizer, args.horizons, device)
        print(f"epoch {epoch}/{args.epochs} train_loss={last_loss:.6f}")

    rows = evaluate(model, val_loader, args.horizons, args.top_k, device)
    if not rows:
        raise ValueError("no evaluation rows generated")
    for row in rows:
        row["train_loss"] = last_loss
        row["eval_split"] = args.val

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
        "train_loss",
        "eval_split",
    ]
    save_csv(rows, args.output, fieldnames)
    ensure_dir(Path(args.checkpoint).parent)
    torch.save(
        {
            "model_state_dict": model.state_dict(),
            "args": vars(args),
            "num_apps": num_apps,
            "num_user_groups": num_user_groups,
            "horizons": args.horizons,
        },
        args.checkpoint,
    )
    print(f"train samples: {len(train_samples)}")
    print(f"val samples: {len(val_samples)}")
    print(f"device: {device}")
    print(f"saved results: {args.output}")
    print(f"saved checkpoint: {args.checkpoint}")


if __name__ == "__main__":
    main()
