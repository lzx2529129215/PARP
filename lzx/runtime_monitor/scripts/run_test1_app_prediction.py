#!/usr/bin/env python3
"""Run the test1 v2 app-switch LSTM over a real Runtime Monitor session."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
OP_ROOT = ROOT / "operation_predictor"
RUNTIME_ROOT = ROOT / "runtime_monitor"
for import_root in (ROOT, RUNTIME_ROOT, OP_ROOT):
    if str(import_root) not in sys.path:
        sys.path.insert(0, str(import_root))

from predictor import OnlineLSTMPredictor
from core.runtime_scope import load_runtime_app_scope


FIELDS = [
    "session_id", "feature_window_id", "timestamp", "foreground_app_key",
    "foreground_app", "history_apps", "opened_apps", "horizon", "rank",
    "app_id", "app", "probability", "score_mode", "status", "error",
]


def split_apps(value: str) -> list[str]:
    return [item.strip() for item in (value or "").replace(";", "|").split("|") if item.strip()]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session-dir", type=Path, required=True)
    parser.add_argument("--checkpoint", type=Path, required=True)
    parser.add_argument("--app-vocab", type=Path, required=True)
    parser.add_argument("--group-vocab", type=Path, required=True)
    parser.add_argument("--scope-config", type=Path, required=True)
    parser.add_argument("--user-group", default="通用用户")
    parser.add_argument("--history-len", type=int, default=5)
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--score-mode", choices=["softmax", "sigmoid"], default="sigmoid")
    parser.add_argument("--device", choices=["auto", "cpu", "cuda"], default="auto")
    args = parser.parse_args()

    model_input = args.session_dir / "model" / "global_state_1s.csv"
    output = args.session_dir / "model" / "test1_app_predictions.csv"
    if not model_input.exists():
        raise FileNotFoundError(model_input)

    scope = load_runtime_app_scope(args.scope_config, args.app_vocab)
    key_to_vocab = scope.app_key_to_vocab_name
    predictor = OnlineLSTMPredictor(
        checkpoint=args.checkpoint,
        app_vocab=args.app_vocab,
        group_vocab=args.group_vocab,
        user_group=args.user_group,
        top_k=args.top_k,
        score_mode=args.score_mode,
        device_name=args.device,
    )

    output.parent.mkdir(parents=True, exist_ok=True)
    prediction_count = 0
    switch_count = 0
    skipped = 0
    history_keys: list[str] = []
    last_key = ""
    with model_input.open("r", encoding="utf-8", newline="") as source, output.open("w", encoding="utf-8", newline="") as target:
        reader = csv.DictReader(source)
        writer = csv.DictWriter(target, fieldnames=FIELDS)
        writer.writeheader()
        for row in reader:
            key = str(row.get("foreground_app", "")).strip()
            vocab_name = key_to_vocab.get(key, "")
            if not vocab_name or key == last_key:
                continue
            switch_count += 1
            opened = [key_to_vocab[item] for item in split_apps(row.get("open_apps", "")) if item in key_to_vocab]
            history_keys.append(key)
            history = [key_to_vocab[item] for item in history_keys[-args.history_len:] if item in key_to_vocab]
            last_key = key
            if len(history) < args.history_len:
                skipped += 1
                writer.writerow({
                    "session_id": row.get("session_id", ""),
                    "feature_window_id": row.get("feature_window_id", ""),
                    "timestamp": row.get("timestamp", ""),
                    "foreground_app_key": key,
                    "foreground_app": vocab_name,
                    "history_apps": "|".join(history),
                    "opened_apps": "|".join(opened),
                    "status": "skipped",
                    "error": "not_enough_history",
                })
                continue
            try:
                outputs = predictor.predict(history, opened, row.get("timestamp", ""))
            except Exception as exc:
                skipped += 1
                writer.writerow({
                    "session_id": row.get("session_id", ""),
                    "feature_window_id": row.get("feature_window_id", ""),
                    "timestamp": row.get("timestamp", ""),
                    "foreground_app_key": key,
                    "foreground_app": vocab_name,
                    "history_apps": "|".join(history),
                    "opened_apps": "|".join(opened),
                    "status": "error",
                    "error": str(exc),
                })
                continue
            for item in outputs:
                prediction_count += 1
                writer.writerow({
                    "session_id": row.get("session_id", ""),
                    "feature_window_id": row.get("feature_window_id", ""),
                    "timestamp": row.get("timestamp", ""),
                    "foreground_app_key": key,
                    "foreground_app": vocab_name,
                    "history_apps": "|".join(history),
                    "opened_apps": "|".join(opened),
                    "horizon": item.get("horizon", ""),
                    "rank": item.get("rank", ""),
                    "app_id": item.get("app_id", ""),
                    "app": item.get("app", ""),
                    "probability": item.get("probability", ""),
                    "score_mode": item.get("score_mode", args.score_mode),
                    "status": "success",
                })

    summary = {
        "session_dir": str(args.session_dir),
        "input": str(model_input),
        "output": str(output),
        "switch_events": switch_count,
        "prediction_rows": prediction_count,
        "skipped_switches": skipped,
        "checkpoint": str(args.checkpoint),
        "app_vocab": str(args.app_vocab),
    }
    (args.session_dir / "review" / "test1_app_prediction_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
