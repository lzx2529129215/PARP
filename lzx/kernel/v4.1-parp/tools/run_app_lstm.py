#!/usr/bin/env python3
"""Run the existing operation_predictor v2 App-LSTM for v4.1 samples.

The input CSV must contain:

``sample_id,timestamp,history_apps,opened_apps,user_group,current_foreground_app``

Application lists may be separated by ``|`` or `,`.  The output is the
normalized prediction contract consumed by ``evaluate_app_lstm_effect.py`` and
``emit_parp_commands.py``.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import sys
from pathlib import Path
from typing import Any


HERE = Path(__file__).resolve()
LZX_ROOT = HERE.parents[3]
OP_ROOT = LZX_ROOT / "operation_predictor"
if str(OP_ROOT) not in sys.path:
    sys.path.insert(0, str(OP_ROOT))


def split_apps(value: str) -> list[str]:
    delimiter = "|" if "|" in value else ","
    return [item.strip() for item in value.split(delimiter) if item.strip()]


def time_feature(value: str) -> list[float]:
    timestamp = dt.datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    weekday = timestamp.weekday()
    return [float(timestamp.hour) / 23.0, float(weekday) / 6.0, float(weekday >= 5)]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--checkpoint", required=True)
    parser.add_argument("--app-vocab", default=str(OP_ROOT / "data/vocab/app_vocab.json"))
    parser.add_argument("--group-vocab", default=str(OP_ROOT / "data/vocab/user_group_vocab.json"))
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--score-mode", choices=["softmax", "sigmoid"], default="softmax")
    parser.add_argument("--model-version", type=int, default=401)
    parser.add_argument("--ttl-ms", type=int, default=30_000)
    parser.add_argument(
        "--keep-current",
        dest="exclude_current",
        action="store_false",
        help="keep the current foreground App in the candidate list",
    )
    parser.add_argument("--device", choices=["auto", "cpu", "cuda"], default="auto")
    return parser.parse_args()


def load_json(path: str | Path) -> dict[str, Any]:
    import json

    return json.loads(Path(path).read_text(encoding="utf-8"))


def main() -> None:
    args = parse_args()
    try:
        import torch
    except ModuleNotFoundError as exc:
        raise SystemExit(
            "PyTorch is required for real App-LSTM inference. "
            "Install operation_predictor/requirements.txt plus a compatible torch build."
        ) from exc

    from v2.infer.infer_app_lstm import build_model, load_checkpoint, score_logits  # noqa: PLC0415

    app_vocab = {name: int(value) for name, value in load_json(args.app_vocab).items()}
    group_vocab = {name: int(value) for name, value in load_json(args.group_vocab).items()}
    id_to_app = {value: name for name, value in app_vocab.items()}
    device = torch.device("cuda" if args.device == "auto" and torch.cuda.is_available() else "cpu")
    if args.device != "auto":
        device = torch.device(args.device)
    checkpoint = load_checkpoint(args.checkpoint, device)
    if len(app_vocab) != int(checkpoint["num_apps"]):
        raise SystemExit(
            f"app vocab size mismatch: vocab={len(app_vocab)} checkpoint={checkpoint['num_apps']}"
        )
    model = build_model(checkpoint, device)
    model.eval()

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "sample_id",
        "horizon_ms",
        "rank",
        "app_id",
        "app",
        "raw_score",
        "use_score",
        "score_mode",
        "model_version",
        "ttl_ms",
    ]
    with Path(args.input).open(encoding="utf-8", newline="") as source, output_path.open(
        "w", encoding="utf-8", newline=""
    ) as target:
        reader = csv.DictReader(source)
        writer = csv.DictWriter(target, fieldnames=fields)
        writer.writeheader()
        for row in reader:
            sample_id = str(row["sample_id"])
            history = [app for app in split_apps(str(row.get("history_apps", ""))) if app in app_vocab]
            opened = [app for app in split_apps(str(row.get("opened_apps", ""))) if app in app_vocab]
            if not history:
                continue
            user_group = str(row.get("user_group", "通用用户"))
            if user_group not in group_vocab:
                raise SystemExit(f"unknown user group {user_group!r} in sample {sample_id}")
            batch = {
                "history_apps": torch.tensor([[app_vocab[app] for app in history]], dtype=torch.long, device=device),
                "opened_apps": torch.tensor(
                    [[1.0 if index in {app_vocab[app] for app in opened} else 0.0 for index in range(len(app_vocab))]],
                    dtype=torch.float32,
                    device=device,
                ),
                "time_feature": torch.tensor([time_feature(str(row["timestamp"]))], dtype=torch.float32, device=device),
                "user_group": torch.tensor([group_vocab[user_group]], dtype=torch.long, device=device),
            }
            current_app = str(row.get("current_foreground_app", ""))
            with torch.no_grad():
                outputs = model(**batch)
            for horizon in sorted(outputs):
                scores = score_logits(outputs[horizon], args.score_mode)
                values, indices = torch.topk(scores, k=min(args.top_k, scores.shape[1]), dim=1)
                candidates = [
                    (int(app_id), float(score))
                    for app_id, score in zip(indices[0].tolist(), values[0].tolist())
                    if not args.exclude_current or id_to_app[int(app_id)] != current_app
                ]
                if args.score_mode == "sigmoid":
                    denominator = sum(score for _, score in candidates) or 1.0
                else:
                    denominator = 1.0
                for rank, (app_id, score) in enumerate(candidates, start=1):
                    writer.writerow(
                        {
                            "sample_id": sample_id,
                            "horizon_ms": int(horizon) * 60_000,
                            "rank": rank,
                            "app_id": app_id,
                            "app": id_to_app[app_id],
                            "raw_score": score,
                            "use_score": score / denominator,
                            "score_mode": args.score_mode,
                            "model_version": args.model_version,
                            "ttl_ms": args.ttl_ms,
                        }
                    )
    print(f"saved: {output_path}")


if __name__ == "__main__":
    main()
