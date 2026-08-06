#!/usr/bin/env python3
"""Run single-sample inference with the v2 application LSTM."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    import torch
except ModuleNotFoundError as exc:  # pragma: no cover - depends on local environment
    raise SystemExit(
        "PyTorch is required for LSTM v2 inference. Install dependencies first, for example: "
        "pip install -r requirements.txt"
    ) from exc

from src.utils.io_utils import ensure_dir, load_json
from v2.models.app_lstm import AppLSTMV2


def parse_time(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")


def time_feature(ts: datetime) -> list[float]:
    weekday = ts.weekday()
    return [float(ts.hour) / 23.0, float(weekday) / 6.0, float(weekday >= 5)]


def inverse_vocab(vocab: dict[str, int]) -> dict[int, str]:
    return {int(app_id): app for app, app_id in vocab.items()}


def app_ids(apps: list[str], app_vocab: dict[str, int], field_name: str) -> list[int]:
    ids: list[int] = []
    missing: list[str] = []
    for app in apps:
        if app not in app_vocab:
            missing.append(app)
            continue
        ids.append(int(app_vocab[app]))
    if missing:
        raise ValueError(f"unknown app names in {field_name}: {missing}")
    return ids


def multihot(ids: list[int], size: int) -> list[float]:
    vec = [0.0] * size
    for item_id in ids:
        if 0 <= item_id < size:
            vec[item_id] = 1.0
    return vec


def load_checkpoint(path: str | Path, device: torch.device) -> dict[str, Any]:
    path = resolve_checkpoint_path(path)
    try:
        return torch.load(path, map_location=device, weights_only=False)
    except TypeError:
        return torch.load(path, map_location=device)


def resolve_checkpoint_path(path: str | Path) -> Path:
    candidate = Path(path)
    if candidate.exists():
        return candidate

    fallback_paths = [
        Path("outputs/checkpoints/v2") / candidate.name,
        Path("outputs/checkpoints/app_lstm") / candidate.name,
    ]
    for fallback in fallback_paths:
        if fallback.exists():
            return fallback

    raise FileNotFoundError(candidate)


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


def score_logits(logits: torch.Tensor, mode: str) -> torch.Tensor:
    if mode == "softmax":
        return torch.softmax(logits, dim=1)
    if mode == "sigmoid":
        return torch.sigmoid(logits)
    raise ValueError(f"unsupported score mode: {mode}")


@torch.no_grad()
def infer(args: argparse.Namespace) -> list[dict[str, Any]]:
    app_vocab = {app: int(app_id) for app, app_id in load_json(args.app_vocab).items()}
    group_vocab = {group: int(group_id) for group, group_id in load_json(args.group_vocab).items()}
    id_to_app = inverse_vocab(app_vocab)

    if args.user_group not in group_vocab:
        raise ValueError(f"unknown user group: {args.user_group}")

    history_ids = app_ids(args.history_apps, app_vocab, "--history-apps")
    opened_ids = app_ids(args.opened_apps, app_vocab, "--opened-apps")
    if not history_ids:
        raise ValueError("--history-apps must contain at least one app")

    if args.device == "auto":
        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    else:
        device = torch.device(args.device)

    checkpoint = load_checkpoint(args.checkpoint, device)
    model = build_model(checkpoint, device)
    if len(app_vocab) != int(checkpoint["num_apps"]):
        raise ValueError(
            f"app vocab size mismatch: vocab={len(app_vocab)} checkpoint={checkpoint['num_apps']}"
        )

    timestamp = parse_time(args.timestamp)
    batch = {
        "history_apps": torch.tensor([history_ids], dtype=torch.long, device=device),
        "opened_apps": torch.tensor([multihot(opened_ids, len(app_vocab))], dtype=torch.float32, device=device),
        "time_feature": torch.tensor([time_feature(timestamp)], dtype=torch.float32, device=device),
        "user_group": torch.tensor([group_vocab[args.user_group]], dtype=torch.long, device=device),
    }

    outputs = model(
        batch["history_apps"],
        batch["opened_apps"],
        batch["time_feature"],
        batch["user_group"],
    )

    rows: list[dict[str, Any]] = []
    for horizon in sorted(outputs):
        scores = score_logits(outputs[horizon], args.score_mode)
        values, indices = torch.topk(scores, k=min(args.top_k, scores.shape[1]), dim=1)
        for rank, (app_id, probability) in enumerate(zip(indices[0].tolist(), values[0].tolist()), start=1):
            rows.append(
                {
                    "horizon": int(horizon),
                    "rank": rank,
                    "app_id": int(app_id),
                    "app": id_to_app[int(app_id)],
                    "probability": float(probability),
                    "score_mode": args.score_mode,
                }
            )
    return rows


def write_rows(rows: list[dict[str, Any]], args: argparse.Namespace) -> None:
    if args.output:
        target = Path(args.output)
        ensure_dir(target.parent)
        with target.open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(
                f,
                fieldnames=["horizon", "rank", "app_id", "app", "probability", "score_mode"],
            )
            writer.writeheader()
            writer.writerows(rows)
        print(f"saved: {target}")
        return

    if args.format == "json":
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return

    writer = csv.DictWriter(
        sys.stdout,
        fieldnames=["horizon", "rank", "app_id", "app", "probability", "score_mode"],
    )
    writer.writeheader()
    writer.writerows(rows)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Infer Top-K future apps with a trained v2 LSTM checkpoint.")
    parser.add_argument("--checkpoint", default="outputs/checkpoints/v2/lsapp_app_lstm.pt")
    parser.add_argument("--app-vocab", default="data/vocab/app_vocab.json")
    parser.add_argument("--group-vocab", default="data/vocab/user_group_vocab.json")
    parser.add_argument("--history-apps", nargs="+", required=True, help="Recent foreground app names in time order.")
    parser.add_argument("--opened-apps", nargs="*", default=[], help="Currently opened app names.")
    parser.add_argument("--timestamp", required=True, help='Current timestamp, e.g. "2018-01-16 06:26:26".')
    parser.add_argument("--user-group", default="通用用户")
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--score-mode", choices=["softmax", "sigmoid"], default="softmax")
    parser.add_argument("--device", choices=["auto", "cpu", "cuda"], default="auto")
    parser.add_argument("--format", choices=["csv", "json"], default="csv")
    parser.add_argument("--output", help="Optional CSV output path. If omitted, prints to stdout.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    rows = infer(args)
    write_rows(rows, args)


if __name__ == "__main__":
    main()
