#!/usr/bin/env python3
"""Run the existing LSTM app predictor on Runtime Monitor output.

This is an adapter only. It does not train models and does not perform any
prefetch, eviction, swap, MGLRU, or memory scheduling action.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any


RUNTIME_MONITOR_ROOT = Path(__file__).resolve().parents[1]
if str(RUNTIME_MONITOR_ROOT) not in sys.path:
    sys.path.insert(0, str(RUNTIME_MONITOR_ROOT))

from predictor import OnlineLSTMPredictor  # noqa: E402


APP_PREDICTION_FIELDS = [
    "session_id",
    "feature_window_id",
    "timestamp",
    "raw_foreground_app",
    "mapped_foreground_app",
    "raw_open_apps",
    "mapped_opened_apps",
    "history_apps",
    "history_len",
    "horizon",
    "rank",
    "app_id",
    "app",
    "probability",
    "score_mode",
    "status",
    "skip_reason",
]

LSTM_CALL_TRACE_FIELDS = [
    "call_id",
    "session_id",
    "feature_window_id",
    "timestamp",
    "call_reason",
    "raw_foreground_app",
    "mapped_foreground_app",
    "raw_open_apps",
    "mapped_opened_apps",
    "history_len",
    "raw_history_apps",
    "mapped_history_apps",
    "user_group",
    "top_k",
    "score_mode",
    "device",
    "input_history_apps",
    "input_opened_apps",
    "input_timestamp",
    "output_horizon_3",
    "output_horizon_5",
    "output_horizon_10",
    "status",
    "error",
]

REVIEW_CALL_TRACE_FIELDS = [
    "call_id",
    "time",
    "foreground_app",
    "mapped_foreground_app",
    "opened_apps",
    "mapped_opened_apps",
    "history",
    "mapped_history",
    "top3_3min",
    "top3_5min",
    "top3_10min",
    "status",
    "note",
]

APP_NAME_MAP = {
    "WPS": "WPS",
    "QQ": "腾讯QQ",
    "FILES": "图库",
}

SKIP_VALUES = {"", "UNKNOWN", "None", "none", "null", "NULL"}


def map_app(raw_app: str | None) -> str:
    if raw_app is None:
        return ""
    raw = str(raw_app).strip()
    if raw in SKIP_VALUES:
        return ""
    return APP_NAME_MAP.get(raw, "")


def split_apps(value: str) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split("|") if item.strip()]


def dedupe_keep_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


def map_open_apps(raw_open_apps: str) -> list[str]:
    mapped = [map_app(app) for app in split_apps(raw_open_apps)]
    return dedupe_keep_order([app for app in mapped if app])


def json_rows_for_horizon(outputs: list[dict[str, Any]], horizon: int) -> str:
    rows = [
        {
            "rank": int(row["rank"]),
            "app_id": int(row["app_id"]),
            "app": str(row["app"]),
            "probability": float(row["probability"]),
        }
        for row in outputs
        if int(row.get("horizon", -1)) == horizon
    ]
    return json.dumps(rows, ensure_ascii=False, separators=(",", ":"))


def top_names(outputs: list[dict[str, Any]], horizon: int, limit: int = 3) -> str:
    rows = [
        row for row in outputs
        if int(row.get("horizon", -1)) == horizon and int(row.get("rank", 0)) <= limit
    ]
    rows.sort(key=lambda row: int(row.get("rank", 0)))
    return "|".join(str(row.get("app", "")) for row in rows if row.get("app"))


def mapping_note(raw_values: list[str]) -> str:
    notes: list[str] = []
    if "FILES" in raw_values:
        notes.append("mapped FILES to 图库")
    if "QQ" in raw_values:
        notes.append("mapped QQ to 腾讯QQ")
    return "; ".join(notes)


def write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fieldnames})


def run(args: argparse.Namespace) -> int:
    session_dir = Path(args.session_dir)
    model_dir = session_dir / "model"
    review_dir = session_dir / "review"
    input_path = model_dir / "global_state_1s.csv"
    if not input_path.exists():
        raise FileNotFoundError(input_path)

    predictor = OnlineLSTMPredictor(
        checkpoint=args.checkpoint,
        app_vocab=args.app_vocab,
        group_vocab=args.group_vocab,
        user_group=args.user_group,
        top_k=args.top_k,
        score_mode=args.score_mode,
        device_name=args.device,
    )
    actual_device = str(getattr(predictor, "device", args.device))

    prediction_rows: list[dict[str, Any]] = []
    call_trace_rows: list[dict[str, Any]] = []
    review_trace_rows: list[dict[str, Any]] = []
    skipped = Counter()
    raw_history: list[str] = []
    mapped_history: list[str] = []
    call_id = 0

    with input_path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            session_id = row.get("session_id", "")
            feature_window_id = row.get("feature_window_id", "")
            timestamp = row.get("timestamp", "")
            raw_fg = row.get("foreground_app", "")
            mapped_fg = map_app(raw_fg)
            raw_open_apps = row.get("open_apps", "")
            mapped_opened = map_open_apps(raw_open_apps)
            raw_history_window = raw_history[-args.history_len:]
            mapped_history_window = mapped_history[-args.history_len:]

            base_prediction = {
                "session_id": session_id,
                "feature_window_id": feature_window_id,
                "timestamp": timestamp,
                "raw_foreground_app": raw_fg,
                "mapped_foreground_app": mapped_fg,
                "raw_open_apps": raw_open_apps,
                "mapped_opened_apps": "|".join(mapped_opened),
                "history_apps": "|".join(mapped_history_window),
                "history_len": len(mapped_history_window),
                "score_mode": args.score_mode,
            }

            skip_reason = ""
            if len(mapped_history_window) < args.history_len:
                skip_reason = "not_enough_valid_history"
            elif not mapped_history_window:
                skip_reason = "no_mappable_history"
            elif not timestamp:
                skip_reason = "missing_timestamp"

            if skip_reason:
                skipped[skip_reason] += 1
                prediction_rows.append({
                    **base_prediction,
                    "status": "skipped",
                    "skip_reason": skip_reason,
                })
            else:
                status = "success"
                error = ""
                outputs: list[dict[str, Any]] = []
                try:
                    outputs = predictor.predict(mapped_history_window, mapped_opened, timestamp)
                    if not outputs:
                        status = "skipped"
                        error = "predictor_returned_no_rows"
                        skipped[error] += 1
                except Exception as exc:  # keep batch conversion robust for inspection.
                    status = "error"
                    error = str(exc)

                if status == "success":
                    for output in outputs:
                        prediction_rows.append({
                            **base_prediction,
                            "horizon": output.get("horizon", ""),
                            "rank": output.get("rank", ""),
                            "app_id": output.get("app_id", ""),
                            "app": output.get("app", ""),
                            "probability": output.get("probability", ""),
                            "score_mode": output.get("score_mode", args.score_mode),
                            "status": "success",
                            "skip_reason": "",
                        })
                else:
                    prediction_rows.append({
                        **base_prediction,
                        "status": status,
                        "skip_reason": error,
                    })

                raw_note_values = [raw_fg] + split_apps(raw_open_apps) + raw_history_window
                call_trace_rows.append({
                    "call_id": call_id,
                    "session_id": session_id,
                    "feature_window_id": feature_window_id,
                    "timestamp": timestamp,
                    "call_reason": "enough_valid_history",
                    "raw_foreground_app": raw_fg,
                    "mapped_foreground_app": mapped_fg,
                    "raw_open_apps": raw_open_apps,
                    "mapped_opened_apps": "|".join(mapped_opened),
                    "history_len": len(mapped_history_window),
                    "raw_history_apps": "|".join(raw_history_window),
                    "mapped_history_apps": "|".join(mapped_history_window),
                    "user_group": args.user_group,
                    "top_k": args.top_k,
                    "score_mode": args.score_mode,
                    "device": actual_device,
                    "input_history_apps": "|".join(mapped_history_window),
                    "input_opened_apps": "|".join(mapped_opened),
                    "input_timestamp": timestamp,
                    "output_horizon_3": json_rows_for_horizon(outputs, 3),
                    "output_horizon_5": json_rows_for_horizon(outputs, 5),
                    "output_horizon_10": json_rows_for_horizon(outputs, 10),
                    "status": status,
                    "error": error,
                })
                review_trace_rows.append({
                    "call_id": call_id,
                    "time": timestamp,
                    "foreground_app": raw_fg,
                    "mapped_foreground_app": mapped_fg,
                    "opened_apps": raw_open_apps,
                    "mapped_opened_apps": "|".join(mapped_opened),
                    "history": "|".join(raw_history_window),
                    "mapped_history": "|".join(mapped_history_window),
                    "top3_3min": top_names(outputs, 3),
                    "top3_5min": top_names(outputs, 5),
                    "top3_10min": top_names(outputs, 10),
                    "status": status,
                    "note": mapping_note(raw_note_values),
                })
                call_id += 1

            if mapped_fg:
                raw_history.append(str(raw_fg))
                mapped_history.append(mapped_fg)

    write_csv(model_dir / "app_predictions_1s.csv", APP_PREDICTION_FIELDS, prediction_rows)
    write_csv(model_dir / "lstm_call_trace.csv", LSTM_CALL_TRACE_FIELDS, call_trace_rows)
    write_csv(review_dir / "lstm_call_trace.csv", REVIEW_CALL_TRACE_FIELDS, review_trace_rows)

    timeline_fields = [
        "time",
        "foreground_app",
        "mapped_foreground_app",
        "opened_apps",
        "mapped_opened_apps",
        "horizon",
        "topk_apps",
        "topk_probabilities",
        "status",
        "note",
    ]
    timeline_rows: list[dict[str, Any]] = []
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in prediction_rows:
        if row.get("status") != "success":
            continue
        grouped.setdefault((str(row.get("timestamp", "")), str(row.get("horizon", ""))), []).append(row)
    for (timestamp, horizon), rows in grouped.items():
        rows.sort(key=lambda item: int(item.get("rank", 0)))
        timeline_rows.append({
            "time": timestamp,
            "foreground_app": rows[0].get("raw_foreground_app", ""),
            "mapped_foreground_app": rows[0].get("mapped_foreground_app", ""),
            "opened_apps": rows[0].get("raw_open_apps", ""),
            "mapped_opened_apps": rows[0].get("mapped_opened_apps", ""),
            "horizon": horizon,
            "topk_apps": "|".join(str(item.get("app", "")) for item in rows),
            "topk_probabilities": "|".join(str(item.get("probability", "")) for item in rows),
            "status": "success",
            "note": mapping_note(
                [str(rows[0].get("raw_foreground_app", ""))]
                + split_apps(str(rows[0].get("raw_open_apps", "")))
            ),
        })
    timeline_rows.sort(key=lambda item: (item["time"], int(item["horizon"] or 0)))
    write_csv(review_dir / "app_predictions_timeline.csv", timeline_fields, timeline_rows)

    print(f"saved: {model_dir / 'app_predictions_1s.csv'}")
    print(f"saved: {model_dir / 'lstm_call_trace.csv'}")
    print(f"saved: {review_dir / 'lstm_call_trace.csv'}")
    print(f"saved: {review_dir / 'app_predictions_timeline.csv'}")
    print(f"lstm_calls: {len(call_trace_rows)}")
    print("skipped:")
    for reason, count in sorted(skipped.items()):
        print(f"  {reason}: {count}")
    print("No prefetch, eviction, swap, MGLRU, or memory scheduling action was performed.")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run LSTM app prediction for a Runtime Monitor session.")
    parser.add_argument("--session-dir", required=True)
    parser.add_argument("--checkpoint", required=True)
    parser.add_argument("--app-vocab", required=True)
    parser.add_argument("--group-vocab", required=True)
    parser.add_argument("--user-group", default="通用用户")
    parser.add_argument("--history-len", type=int, default=5)
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--score-mode", choices=["softmax", "sigmoid"], default="softmax")
    parser.add_argument("--device", choices=["auto", "cpu", "cuda"], default="auto")
    return parser.parse_args()


def main() -> None:
    raise SystemExit(run(parse_args()))


if __name__ == "__main__":
    main()
