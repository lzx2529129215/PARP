#!/usr/bin/env python3
"""Train the v3 single-step next-foreground application LSTM."""

from __future__ import annotations

import argparse
import csv
import json
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
except ModuleNotFoundError as exc:  # pragma: no cover
    raise SystemExit("PyTorch is required for v3 switch LSTM training") from exc

from v3.models.app_lstm_duration import AppLSTMNextV3


def split_pipe(value: str | None) -> list[str]:
    return [item for item in (value or "").split("|") if item]


def load_json(path: str | Path) -> dict[str, int]:
    return {key: int(value) for key, value in json.loads(Path(path).read_text(encoding="utf-8")).items()}


def load_csv(path: Path, max_samples: int = 0) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as stream:
        rows = [row for row in csv.DictReader(stream) if row.get("has_next_switch") == "1"]
    if max_samples:
        rows = rows[:max_samples]
    if not rows:
        raise ValueError(f"no next-switch rows in {path}")
    return rows


class SwitchDataset(Dataset):
    def __init__(self, rows: list[dict[str, str]], app_vocab: dict[str, int], group_vocab: dict[str, int]) -> None:
        self.rows = rows
        self.app_vocab = app_vocab
        self.group_vocab = group_vocab
        self.unknown_id = app_vocab["<UNKNOWN>"]

    def __len__(self) -> int:
        return len(self.rows)

    def __getitem__(self, index: int) -> dict[str, Any]:
        row = self.rows[index]
        history_apps = [self.app_vocab.get(app, self.unknown_id) for app in split_pipe(row["history_apps"])]
        durations = [float(value) for value in split_pipe(row["history_durations_s"])]
        masks = [float(value) for value in split_pipe(row["history_mask"])]
        opened = [self.app_vocab[app] for app in split_pipe(row.get("opened_apps")) if app in self.app_vocab]
        opened_vec = [0.0] * len(self.app_vocab)
        for app_id in opened:
            opened_vec[app_id] = 1.0
        group_name = row.get("user_group_name", "通用用户")
        group_id = int(row["user_group"]) if row.get("user_group", "").isdigit() else self.group_vocab.get(group_name, 0)
        target_id = int(row["next_app_id"])
        current_id = int(row.get("current_app_id") or self.unknown_id)
        timestamp = row["timestamp"]
        date, clock = timestamp.split(" ", 1)
        year, month, day = [int(item) for item in date.split("-")]
        hour, _minute, _second = [int(item) for item in clock.split(":")]
        # datetime-free weekday calculation keeps this dataset adapter simple;
        # the exact wall-clock feature is not used as a label.
        import datetime as dt
        weekday = dt.date(year, month, day).weekday()
        return {
            "history_apps": torch.tensor(history_apps, dtype=torch.long),
            "history_durations": torch.tensor(durations, dtype=torch.float32),
            "history_mask": torch.tensor(masks, dtype=torch.float32),
            "opened_apps": torch.tensor(opened_vec, dtype=torch.float32),
            "time_feature": torch.tensor([hour / 23.0, weekday / 6.0, float(weekday >= 5)], dtype=torch.float32),
            "user_group": torch.tensor(group_id, dtype=torch.long),
            "current_app": torch.tensor(current_id, dtype=torch.long),
            "target": torch.tensor(target_id, dtype=torch.long),
        }


def collate(batch: list[dict[str, Any]]) -> dict[str, torch.Tensor]:
    return {key: torch.stack([item[key] for item in batch]) for key in batch[0]}


def move_batch(batch: dict[str, torch.Tensor], device: torch.device) -> dict[str, torch.Tensor]:
    return {key: value.to(device) for key, value in batch.items()}


def train_epoch(model: AppLSTMNextV3, loader: DataLoader, optimizer: torch.optim.Optimizer, device: torch.device) -> float:
    model.train()
    criterion = nn.CrossEntropyLoss()
    total = 0.0
    count = 0
    for batch in loader:
        batch = move_batch(batch, device)
        logits = model(
            batch["history_apps"], batch["history_durations"], batch["history_mask"],
            batch["opened_apps"], batch["time_feature"], batch["user_group"], batch["current_app"],
        )
        loss = criterion(logits, batch["target"])
        optimizer.zero_grad()
        loss.backward()
        optimizer.step()
        total += float(loss.item()) * len(batch["target"])
        count += len(batch["target"])
    return total / max(1, count)


@torch.no_grad()
def evaluate(model: AppLSTMNextV3, loader: DataLoader, device: torch.device, split: str) -> dict[str, Any]:
    model.eval()
    total = 0
    hits = {1: 0, 3: 0, 5: 0}
    reciprocal_rank = 0.0
    for batch in loader:
        batch = move_batch(batch, device)
        logits = model(
            batch["history_apps"], batch["history_durations"], batch["history_mask"],
            batch["opened_apps"], batch["time_feature"], batch["user_group"], batch["current_app"],
        )
        ranked = torch.argsort(logits, dim=1, descending=True)
        targets = batch["target"].tolist()
        for prediction, target in zip(ranked.tolist(), targets):
            total += 1
            for k in hits:
                hits[k] += int(target in prediction[:k])
            reciprocal_rank += 1.0 / (prediction.index(target) + 1)
    return {
        "version": "v3",
        "model": "app_lstm_switch",
        "split": split,
        "num_samples": total,
        "hit_at_1": hits[1] / max(1, total),
        "hit_at_3": hits[3] / max(1, total),
        "hit_at_5": hits[5] / max(1, total),
        "mrr": reciprocal_rank / max(1, total),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset-dir", default=str(ROOT / "data/test1/processed/app_lstm_duration_switch"))
    parser.add_argument("--app-vocab", default=str(ROOT / "data/vocab/test1/app_vocab_duration.json"))
    parser.add_argument("--group-vocab", default=str(ROOT / "data/vocab/test1/user_group_vocab.json"))
    parser.add_argument("--history-len", type=int, default=5)
    parser.add_argument("--duration-cap-s", type=float, default=600.0)
    parser.add_argument("--batch-size", type=int, default=2048)
    parser.add_argument("--epochs", type=int, default=10)
    parser.add_argument("--lr", type=float, default=1e-3)
    parser.add_argument("--app-embedding-dim", type=int, default=32)
    parser.add_argument("--duration-embedding-dim", type=int, default=8)
    parser.add_argument("--group-embedding-dim", type=int, default=8)
    parser.add_argument("--hidden-dim", type=int, default=64)
    parser.add_argument("--opened-dim", type=int, default=32)
    parser.add_argument("--dropout", type=float, default=0.2)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--device", choices=["auto", "cpu", "cuda"], default="auto")
    parser.add_argument("--max-samples-per-split", type=int, default=0)
    parser.add_argument("--output-dir", default=str(ROOT / "outputs/checkpoints/app_lstm_duration"))
    parser.add_argument("--checkpoint-name", default="lsapp_app_lstm_switch_v3.pt")
    parser.add_argument("--output", default=str(ROOT / "outputs/results/v3/lsapp_app_lstm_switch_results.json"))
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    random.seed(args.seed)
    torch.manual_seed(args.seed)
    app_vocab = load_json(args.app_vocab)
    group_vocab = load_json(args.group_vocab)
    dataset_dir = Path(args.dataset_dir)
    train_rows = load_csv(dataset_dir / "train.csv", args.max_samples_per_split)
    val_rows = load_csv(dataset_dir / "val.csv", args.max_samples_per_split)
    test_rows = load_csv(dataset_dir / "test.csv", args.max_samples_per_split)
    device = torch.device("cuda" if args.device == "auto" and torch.cuda.is_available() else ("cpu" if args.device == "auto" else args.device))
    train_loader = DataLoader(SwitchDataset(train_rows, app_vocab, group_vocab), batch_size=args.batch_size, shuffle=True, collate_fn=collate)
    val_loader = DataLoader(SwitchDataset(val_rows, app_vocab, group_vocab), batch_size=args.batch_size, shuffle=False, collate_fn=collate)
    test_loader = DataLoader(SwitchDataset(test_rows, app_vocab, group_vocab), batch_size=args.batch_size, shuffle=False, collate_fn=collate)
    model = AppLSTMNextV3(
        num_apps=len(app_vocab), num_user_groups=max(group_vocab.values()) + 1, pad_id=app_vocab["<PAD>"],
        app_embedding_dim=args.app_embedding_dim, duration_embedding_dim=args.duration_embedding_dim,
        group_embedding_dim=args.group_embedding_dim, hidden_dim=args.hidden_dim, opened_dim=args.opened_dim,
        duration_cap_s=args.duration_cap_s, dropout=args.dropout,
    ).to(device)
    optimizer = torch.optim.Adam(model.parameters(), lr=args.lr)
    for epoch in range(1, args.epochs + 1):
        loss = train_epoch(model, train_loader, optimizer, device)
        print(f"epoch {epoch}/{args.epochs} train_loss={loss:.6f}")
    results = [evaluate(model, val_loader, device, "val"), evaluate(model, test_loader, device, "test")]
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    checkpoint = {
        "model_type": "app_switch_v3",
        "model_state_dict": model.state_dict(),
        "args": vars(args),
        "num_apps": len(app_vocab),
        "num_user_groups": max(group_vocab.values()) + 1,
        "pad_id": app_vocab["<PAD>"],
        "unknown_id": app_vocab["<UNKNOWN>"],
        "output_format": "app_probability",
    }
    checkpoint_path = output_dir / args.checkpoint_name
    torch.save(checkpoint, checkpoint_path)
    result_path = Path(args.output)
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(results, ensure_ascii=False, indent=2))
    print(f"checkpoint saved: {checkpoint_path}")


if __name__ == "__main__":
    main()
