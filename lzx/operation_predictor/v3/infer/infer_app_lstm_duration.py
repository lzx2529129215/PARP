#!/usr/bin/env python3
"""Run single-sample inference with the duration-aware app LSTM."""

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
except ModuleNotFoundError as exc:  # pragma: no cover
    raise SystemExit("PyTorch is required for duration-aware LSTM inference.") from exc

from src.utils.io_utils import ensure_dir, load_json
from v3.models.app_lstm_duration import AppLSTMDurationV3


def parse_time(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")


def time_feature(ts: datetime) -> list[float]:
    weekday = ts.weekday()
    return [float(ts.hour) / 23.0, float(weekday) / 6.0, float(weekday >= 5)]


def inverse_vocab(vocab: dict[str, int]) -> dict[int, str]:
    return {int(app_id): app for app, app_id in vocab.items()}


def split_csv_arg(value: str | list[str] | None) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            parts.extend(split_csv_arg(item))
        return parts
    return [item.strip() for item in str(value).split(",") if item.strip()]


def parse_float_list(value: str | list[str] | None) -> list[float]:
    return [float(item) for item in split_csv_arg(value)]


def multihot(ids: list[int], size: int) -> list[float]:
    vec = [0.0] * size
    for item_id in ids:
        if 0 <= item_id < size:
            vec[item_id] = 1.0
    return vec


def resolve_checkpoint_path(path: str | Path) -> Path:
    candidate = Path(path)
    if candidate.exists():
        return candidate
    fallback = ROOT / "outputs" / "checkpoints" / "app_lstm_duration" / candidate.name
    if fallback.exists():
        return fallback
    raise FileNotFoundError(f"duration-aware checkpoint not found: {candidate}")


def load_checkpoint(path: str | Path, device: torch.device) -> dict[str, Any]:
    path = resolve_checkpoint_path(path)
    try:
        return torch.load(path, map_location=device, weights_only=False)
    except TypeError:
        return torch.load(path, map_location=device)


def build_model(checkpoint: dict[str, Any], device: torch.device) -> AppLSTMDurationV3:
    ckpt_args = checkpoint.get("args", {})
    horizons = [int(horizon) for horizon in checkpoint.get("horizons", ckpt_args.get("horizons", [3, 5, 10]))]
    model = AppLSTMDurationV3(
        num_apps=int(checkpoint["num_apps"]),
        num_user_groups=int(checkpoint["num_user_groups"]),
        horizons=horizons,
        pad_id=int(checkpoint.get("pad_id", ckpt_args.get("pad_id"))),
        app_embedding_dim=int(ckpt_args.get("app_embedding_dim", 32)),
        duration_embedding_dim=int(ckpt_args.get("duration_embedding_dim", 8)),
        group_embedding_dim=int(ckpt_args.get("group_embedding_dim", 8)),
        hidden_dim=int(ckpt_args.get("hidden_dim", 64)),
        opened_dim=int(ckpt_args.get("opened_dim", 32)),
        duration_cap_s=float(ckpt_args.get("duration_cap_s", checkpoint.get("duration_cap_s", 600.0))),
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


def pad_history(
    apps: list[str],
    durations: list[float],
    app_vocab: dict[str, int],
    history_len: int,
) -> tuple[list[str], list[float], list[int]]:
    if len(apps) != len(durations):
        raise ValueError("history app count must match history duration count")
    if not apps:
        raise ValueError("--history-apps must contain at least one app")
    pad = "<PAD>"
    unknown = "<UNKNOWN>"
    if pad not in app_vocab or unknown not in app_vocab:
        raise ValueError("duration vocab must contain <PAD> and <UNKNOWN>")
    mapped_apps = [app if app in app_vocab else unknown for app in apps][-history_len:]
    mapped_durations = [max(0.0, float(item)) for item in durations][-history_len:]
    real_count = len(mapped_apps)
    pad_count = max(0, history_len - real_count)
    return (
        [pad] * pad_count + mapped_apps,
        [0.0] * pad_count + mapped_durations,
        [0] * pad_count + [1] * real_count,
    )


@torch.no_grad()
def infer(args: argparse.Namespace) -> list[dict[str, Any]]:
    app_vocab = {app: int(app_id) for app, app_id in load_json(args.app_vocab).items()}
    group_vocab = {group: int(group_id) for group, group_id in load_json(args.group_vocab).items()}
    id_to_app = inverse_vocab(app_vocab)
    if args.user_group not in group_vocab:
        raise ValueError(f"unknown user group: {args.user_group}")

    history_apps = split_csv_arg(args.history_apps)
    history_durations = parse_float_list(args.history_durations)
    opened_apps = [app if app in app_vocab else "<UNKNOWN>" for app in split_csv_arg(args.opened_apps)]
    padded_apps, padded_durations, history_mask = pad_history(
        history_apps, history_durations, app_vocab, args.history_len
    )
    history_ids = [app_vocab[app] for app in padded_apps]
    opened_ids = [app_vocab[app] for app in opened_apps if app in app_vocab]

    if args.device == "auto":
        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    else:
        device = torch.device(args.device)
    checkpoint = load_checkpoint(args.checkpoint, device)
    if len(app_vocab) != int(checkpoint["num_apps"]):
        raise ValueError(f"app vocab size mismatch: vocab={len(app_vocab)} checkpoint={checkpoint['num_apps']}")
    model = build_model(checkpoint, device)

    timestamp = parse_time(args.timestamp)
    batch = {
        "history_apps": torch.tensor([history_ids], dtype=torch.long, device=device),
        "history_durations": torch.tensor([padded_durations], dtype=torch.float32, device=device),
        "history_mask": torch.tensor([history_mask], dtype=torch.float32, device=device),
        "opened_apps": torch.tensor([multihot(opened_ids, len(app_vocab))], dtype=torch.float32, device=device),
        "time_feature": torch.tensor([time_feature(timestamp)], dtype=torch.float32, device=device),
        "user_group": torch.tensor([group_vocab[args.user_group]], dtype=torch.long, device=device),
    }
    outputs = model(**batch)

    rows: list[dict[str, Any]] = []
    for horizon in sorted(outputs):
        scores = score_logits(outputs[horizon], args.score_mode)
        values, indices = torch.topk(scores, k=min(args.top_k, scores.shape[1]), dim=1)
        for rank, (app_id, probability) in enumerate(zip(indices[0].tolist(), values[0].tolist()), start=1):
            rows.append({
                "horizon": int(horizon),
                "rank": rank,
                "app_id": int(app_id),
                "app": id_to_app[int(app_id)],
                "probability": float(probability),
                "score_mode": args.score_mode,
            })
    return rows


def write_rows(rows: list[dict[str, Any]], args: argparse.Namespace) -> None:
    fields = ["horizon", "rank", "app_id", "app", "probability", "score_mode"]
    if args.output:
        target = Path(args.output)
        ensure_dir(target.parent)
        with target.open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)
        print(f"saved: {target}")
        return
    if args.format == "json":
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return
    writer = csv.DictWriter(sys.stdout, fieldnames=fields)
    writer.writeheader()
    writer.writerows(rows)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Infer Top-K future apps with duration-aware LSTM v3.")
    parser.add_argument("--checkpoint", required=True)
    parser.add_argument("--app-vocab", default="data/vocab/app_vocab_duration.json")
    parser.add_argument("--group-vocab", default="data/vocab/user_group_vocab.json")
    parser.add_argument("--history-apps", required=True, help="Comma-separated app segment history.")
    parser.add_argument("--history-durations", required=True, help="Comma-separated dwell durations in seconds.")
    parser.add_argument("--opened-apps", default="", help="Comma-separated opened app names.")
    parser.add_argument("--timestamp", required=True)
    parser.add_argument("--user-group", default="通用用户")
    parser.add_argument("--history-len", type=int, default=5)
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--score-mode", choices=["softmax", "sigmoid"], default="sigmoid")
    parser.add_argument("--device", choices=["auto", "cpu", "cuda"], default="auto")
    parser.add_argument("--format", choices=["csv", "json"], default="csv")
    parser.add_argument("--output")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    rows = infer(args)
    write_rows(rows, args)


if __name__ == "__main__":
    main()
