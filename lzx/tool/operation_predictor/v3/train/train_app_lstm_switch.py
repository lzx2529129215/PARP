#!/usr/bin/env python3
"""Train the v3 single-step next-foreground application LSTM."""

from __future__ import annotations

import argparse
import csv
import json
import math
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


def mask_non_app_outputs(logits: torch.Tensor, invalid_ids: tuple[int, ...]) -> torch.Tensor:
    if not invalid_ids:
        return logits
    logits = logits.clone()
    logits[:, list(invalid_ids)] = torch.finfo(logits.dtype).min
    return logits


def class_weights(rows: list[dict[str, str]], app_vocab: dict[str, int], mode: str) -> tuple[torch.Tensor | None, dict[str, Any]]:
    counts = Counter(int(row["next_app_id"]) for row in rows)
    id_to_app = {app_id: app for app, app_id in app_vocab.items()}
    eligible = [app_id for app, app_id in app_vocab.items() if not app.startswith("<")]
    weights = torch.ones(len(app_vocab), dtype=torch.float32)
    if mode == "inverse-sqrt":
        total = sum(counts[app_id] for app_id in eligible)
        classes = max(1, sum(counts[app_id] > 0 for app_id in eligible))
        for app_id in eligible:
            count = counts[app_id]
            weights[app_id] = min(4.0, max(0.25, math.sqrt(total / max(1, classes * count)))) if count else 0.0
    for app, app_id in app_vocab.items():
        if app.startswith("<"):
            weights[app_id] = 0.0
    report = {
        "mode": mode,
        "target_counts": {id_to_app[app_id]: counts[app_id] for app_id in eligible},
        "weights": {id_to_app[app_id]: float(weights[app_id]) for app_id in eligible},
    }
    return (weights if mode != "none" else None), report


def train_epoch(model: AppLSTMNextV3, loader: DataLoader, optimizer: torch.optim.Optimizer, device: torch.device, weight: torch.Tensor | None, invalid_ids: tuple[int, ...]) -> float:
    model.train()
    criterion = nn.CrossEntropyLoss(weight=weight.to(device) if weight is not None else None)
    total = 0.0
    count = 0
    for batch in loader:
        batch = move_batch(batch, device)
        logits = model(
            batch["history_apps"], batch["history_durations"], batch["history_mask"],
            batch["opened_apps"], batch["time_feature"], batch["user_group"], batch["current_app"],
        )
        logits = mask_non_app_outputs(logits, invalid_ids)
        loss = criterion(logits, batch["target"])
        optimizer.zero_grad()
        loss.backward()
        optimizer.step()
        total += float(loss.item()) * len(batch["target"])
        count += len(batch["target"])
    return total / max(1, count)


@torch.no_grad()
def evaluate(model: AppLSTMNextV3, loader: DataLoader, device: torch.device, split: str, app_vocab: dict[str, int]) -> dict[str, Any]:
    model.eval()
    total = 0
    hits = {1: 0, 3: 0, 5: 0}
    per_app_total: Counter[int] = Counter()
    per_app_hits = {1: Counter(), 3: Counter(), 5: Counter()}
    reciprocal_rank = 0.0
    invalid_ids = tuple(app_id for app, app_id in app_vocab.items() if app.startswith("<"))
    for batch in loader:
        batch = move_batch(batch, device)
        logits = model(
            batch["history_apps"], batch["history_durations"], batch["history_mask"],
            batch["opened_apps"], batch["time_feature"], batch["user_group"], batch["current_app"],
        )
        logits = mask_non_app_outputs(logits, invalid_ids)
        ranked = torch.argsort(logits, dim=1, descending=True)
        targets = batch["target"].tolist()
        for prediction, target in zip(ranked.tolist(), targets):
            total += 1
            per_app_total[target] += 1
            for k in hits:
                matched = int(target in prediction[:k])
                hits[k] += matched
                per_app_hits[k][target] += matched
            reciprocal_rank += 1.0 / (prediction.index(target) + 1)
    id_to_app = {app_id: app for app, app_id in app_vocab.items()}
    per_app = {
        id_to_app[app_id]: {
            "num_samples": count,
            **{f"hit_at_{k}": per_app_hits[k][app_id] / count for k in hits},
        }
        for app_id, count in sorted(per_app_total.items())
        if count and not id_to_app[app_id].startswith("<")
    }
    return {
        "version": "v3",
        "model": "app_lstm_switch",
        "split": split,
        "num_samples": total,
        "hit_at_1": hits[1] / max(1, total),
        "hit_at_3": hits[3] / max(1, total),
        "hit_at_5": hits[5] / max(1, total),
        "macro_hit_at_1": sum(item["hit_at_1"] for item in per_app.values()) / max(1, len(per_app)),
        "macro_hit_at_3": sum(item["hit_at_3"] for item in per_app.values()) / max(1, len(per_app)),
        "macro_hit_at_5": sum(item["hit_at_5"] for item in per_app.values()) / max(1, len(per_app)),
        "mrr": reciprocal_rank / max(1, total),
        "per_app": per_app,
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
    parser.add_argument("--class-weight-mode", choices=["none", "inverse-sqrt"], default="none")
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
    weight, weight_report = class_weights(train_rows, app_vocab, args.class_weight_mode)
    invalid_ids = tuple(app_id for app, app_id in app_vocab.items() if app.startswith("<"))
    for epoch in range(1, args.epochs + 1):
        loss = train_epoch(model, train_loader, optimizer, device, weight, invalid_ids)
        print(f"epoch {epoch}/{args.epochs} train_loss={loss:.6f}")
    results = [evaluate(model, val_loader, device, "val", app_vocab), evaluate(model, test_loader, device, "test", app_vocab)]
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
        "class_weighting": weight_report,
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
