#!/usr/bin/env python3
"""Run duration-aware LSTM app prediction on Runtime Monitor output.

This adapter only performs offline CSV conversion and prediction. It does not
train models and does not perform prefetch, eviction, swap, MGLRU, or memory
scheduling actions.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any


RUNTIME_MONITOR_ROOT = Path(__file__).resolve().parents[1]
if str(RUNTIME_MONITOR_ROOT) not in sys.path:
    sys.path.insert(0, str(RUNTIME_MONITOR_ROOT))

from predictor_duration import OnlineLSTMDurationPredictor  # noqa: E402


MODEL_SEGMENT_FIELDS = [
    "segment_id",
    "session_id",
    "start_feature_window_id",
    "end_feature_window_id",
    "start_time",
    "end_time",
    "raw_app",
    "mapped_app",
    "dwell_s",
    "raw_open_apps_start",
    "raw_open_apps_end",
    "mapped_open_apps_start",
    "mapped_open_apps_end",
    "status",
    "note",
]

REVIEW_SEGMENT_FIELDS = [
    "segment_id",
    "start_time",
    "end_time",
    "raw_app",
    "mapped_app",
    "dwell_s",
    "mapped_open_apps_start",
    "mapped_open_apps_end",
    "note",
]

PREDICTION_FIELDS = [
    "session_id",
    "feature_window_id",
    "timestamp",
    "trigger_type",
    "raw_foreground_app",
    "mapped_foreground_app",
    "raw_open_apps",
    "mapped_opened_apps",
    "history_apps",
    "history_durations_s",
    "history_mask",
    "horizon",
    "rank",
    "app_id",
    "app",
    "probability",
    "score_mode",
    "status",
    "skip_reason",
]

TIMELINE_FIELDS = [
    "time",
    "trigger_type",
    "foreground_app",
    "mapped_foreground_app",
    "opened_apps",
    "mapped_opened_apps",
    "history_apps",
    "history_durations_s",
    "horizon",
    "top1",
    "top3",
    "status",
    "note",
]

CALL_TRACE_FIELDS = [
    "call_id",
    "session_id",
    "feature_window_id",
    "timestamp",
    "trigger_type",
    "transition_from_raw",
    "transition_to_raw",
    "transition_from_mapped",
    "transition_to_mapped",
    "raw_foreground_app",
    "mapped_foreground_app",
    "raw_open_apps",
    "mapped_opened_apps",
    "raw_history_apps",
    "mapped_history_apps",
    "input_history_apps",
    "input_history_durations_s",
    "input_history_mask",
    "current_app_dwell_s",
    "dwell_bucket",
    "user_group",
    "top_k",
    "score_mode",
    "device",
    "model_type",
    "checkpoint",
    "output_horizon_3",
    "output_horizon_5",
    "output_horizon_10",
    "status",
    "error",
]

REVIEW_CALL_TRACE_FIELDS = [
    "call_id",
    "time",
    "trigger_type",
    "foreground_app",
    "mapped_foreground_app",
    "opened_apps",
    "mapped_opened_apps",
    "history",
    "history_durations_s",
    "history_mask",
    "current_app_dwell_s",
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
UNKNOWN_VALUES = {"", "UNKNOWN", "None", "none", "null", "NULL"}


def parse_time(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")


def format_time(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S")


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


def map_app(raw_app: str | None) -> str:
    if raw_app is None:
        return "<UNKNOWN>"
    raw = str(raw_app).strip()
    if raw in UNKNOWN_VALUES:
        return "<UNKNOWN>"
    return APP_NAME_MAP.get(raw, "<UNKNOWN>")


def map_open_apps(raw_open_apps: str) -> list[str]:
    return dedupe_keep_order([map_app(app) for app in split_apps(raw_open_apps)])


def mapping_note(raw_values: list[str], include_model_note: bool = False) -> str:
    notes: list[str] = []
    if "FILES" in raw_values:
        notes.append("mapped FILES to 图库")
    if "QQ" in raw_values:
        notes.append("mapped QQ to 腾讯QQ")
    if include_model_note:
        notes.append("used duration-aware model")
    return "; ".join(notes)


def write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fieldnames})


def dwell_seconds(start: datetime, end: datetime) -> float:
    return max(1.0, (end - start).total_seconds())


def fmt_duration(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:.3f}".rstrip("0").rstrip(".")


def build_segments(global_rows: list[dict[str, str]]) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    segments: list[dict[str, Any]] = []
    row_to_segment: dict[str, dict[str, Any]] = {}
    current: dict[str, Any] | None = None

    sorted_rows = sorted(global_rows, key=lambda row: row.get("timestamp", ""))
    for row in sorted_rows:
        raw_app = row.get("foreground_app", "")
        mapped_app = map_app(raw_app)
        timestamp = parse_time(row["timestamp"])
        mapped_open_apps = "|".join(map_open_apps(row.get("open_apps", "")))
        if current is None or current["mapped_app"] != mapped_app:
            if current is not None:
                segments.append(current)
            current = {
                "segment_id": len(segments),
                "session_id": row.get("session_id", ""),
                "start_feature_window_id": row.get("feature_window_id", ""),
                "end_feature_window_id": row.get("feature_window_id", ""),
                "start_time": row["timestamp"],
                "end_time": row["timestamp"],
                "raw_app": raw_app,
                "mapped_app": mapped_app,
                "raw_open_apps_start": row.get("open_apps", ""),
                "raw_open_apps_end": row.get("open_apps", ""),
                "mapped_open_apps_start": mapped_open_apps,
                "mapped_open_apps_end": mapped_open_apps,
                "status": "ok",
                "note": mapping_note([raw_app] + split_apps(row.get("open_apps", ""))),
            }
        else:
            current["end_feature_window_id"] = row.get("feature_window_id", "")
            current["end_time"] = row["timestamp"]
            current["raw_open_apps_end"] = row.get("open_apps", "")
            current["mapped_open_apps_end"] = mapped_open_apps
        if current is not None:
            row_to_segment[row.get("feature_window_id", "")] = current

    if current is not None:
        segments.append(current)

    for segment_id, segment in enumerate(segments):
        segment["segment_id"] = segment_id
        dwell = dwell_seconds(parse_time(segment["start_time"]), parse_time(segment["end_time"]))
        segment["dwell_s"] = fmt_duration(dwell)
    return segments, row_to_segment


def padded_history(
    apps: list[str],
    durations: list[float],
    history_len: int,
) -> tuple[list[str], list[str], list[str]]:
    apps = apps[-history_len:]
    durations = durations[-history_len:]
    pad_count = max(0, history_len - len(apps))
    return (
        ["<PAD>"] * pad_count + apps,
        ["0"] * pad_count + [fmt_duration(duration) for duration in durations],
        ["0"] * pad_count + ["1"] * len(apps),
    )


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
    rows = [row for row in outputs if int(row.get("horizon", -1)) == horizon]
    rows.sort(key=lambda row: int(row.get("rank", 0)))
    return "|".join(str(row.get("app", "")) for row in rows[:limit] if row.get("app"))


def trigger_for_row(
    row: dict[str, str],
    previous_row: dict[str, str] | None,
    current_time: datetime,
    last_prediction_time: datetime | None,
    current_dwell_s: float,
    previous_dwell_s: float,
    dwell_buckets: list[float],
    args: argparse.Namespace,
) -> tuple[str, str]:
    raw_fg = row.get("foreground_app", "")
    mapped_fg = map_app(raw_fg)
    mapped_opened = "|".join(map_open_apps(row.get("open_apps", "")))
    event_triggers: list[str] = []
    bucket = ""
    if last_prediction_time is None:
        return "initial_prediction", bucket

    elapsed_since_prediction = (current_time - last_prediction_time).total_seconds()
    if previous_row is not None and map_app(previous_row.get("foreground_app", "")) != mapped_fg:
        event_triggers.append("foreground_transition")
    if previous_row is not None and "|".join(map_open_apps(previous_row.get("open_apps", ""))) != mapped_opened:
        event_triggers.append("opened_apps_change")

    if not args.disable_dwell_bucket_trigger:
        crossed = [item for item in dwell_buckets if previous_dwell_s < item <= current_dwell_s]
        if crossed:
            event_triggers.append("dwell_bucket_cross")
            bucket = fmt_duration(max(crossed))

    if event_triggers:
        if elapsed_since_prediction < args.min_event_cooldown_s:
            return "event_cooldown", bucket
        return "+".join(dedupe_keep_order(event_triggers)), bucket

    if args.trigger_mode == "event_plus_ttl" and elapsed_since_prediction >= args.prediction_ttl_s:
        return f"periodic_ttl_refresh_{fmt_duration(args.periodic_refresh_s)}s", bucket
    return "", bucket


def load_global_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def run(args: argparse.Namespace) -> int:
    session_dir = Path(args.session_dir)
    model_dir = session_dir / "model"
    review_dir = session_dir / "review"
    input_path = model_dir / "global_state_1s.csv"
    if not input_path.exists():
        raise FileNotFoundError(input_path)

    global_rows = load_global_rows(input_path)
    segments, row_to_segment = build_segments(global_rows)
    write_csv(model_dir / "app_state_segments.csv", MODEL_SEGMENT_FIELDS, segments)
    write_csv(review_dir / "app_state_segments.csv", REVIEW_SEGMENT_FIELDS, segments)

    prediction_rows: list[dict[str, Any]] = []
    timeline_rows: list[dict[str, Any]] = []
    call_trace_rows: list[dict[str, Any]] = []
    review_call_trace_rows: list[dict[str, Any]] = []
    skipped = Counter()
    dwell_buckets = [float(item) for item in args.dwell_buckets.split(",") if item.strip()]

    predictor: OnlineLSTMDurationPredictor | None = None
    predictor_error = ""
    try:
        predictor = OnlineLSTMDurationPredictor(
            checkpoint=args.checkpoint,
            app_vocab=args.app_vocab,
            group_vocab=args.group_vocab,
            user_group=args.user_group,
            history_len=args.history_len,
            duration_cap_s=args.duration_cap_s,
            top_k=args.top_k,
            score_mode=args.score_mode,
            device_name=args.device,
        )
    except Exception as exc:
        predictor_error = f"predictor_error:{exc}"
        print(f"ERROR: duration-aware predictor is unavailable: {exc}", file=sys.stderr)

    completed_segments: list[dict[str, Any]] = []
    previous_row: dict[str, str] | None = None
    previous_segment_id: int | None = None
    previous_dwell_s = 0.0
    last_prediction_time: datetime | None = None
    call_id = 0

    for row in sorted(global_rows, key=lambda item: item.get("timestamp", "")):
        feature_window_id = row.get("feature_window_id", "")
        segment = row_to_segment[feature_window_id]
        segment_id = int(segment["segment_id"])
        if previous_segment_id is not None and previous_segment_id != segment_id:
            completed_segments.append(segments[previous_segment_id])
            previous_dwell_s = 0.0

        timestamp = row.get("timestamp", "")
        current_time = parse_time(timestamp)
        segment_start = parse_time(segment["start_time"])
        current_dwell_s = max(1.0, (current_time - segment_start).total_seconds())
        trigger_type, dwell_bucket = trigger_for_row(
            row,
            previous_row,
            current_time,
            last_prediction_time,
            current_dwell_s,
            previous_dwell_s,
            dwell_buckets,
            args,
        )

        history_segments = completed_segments + [
            {
                "raw_app": segment["raw_app"],
                "mapped_app": segment["mapped_app"],
                "dwell_s": current_dwell_s,
            }
        ]
        raw_history = [str(item["raw_app"]) for item in history_segments]
        mapped_history = [str(item["mapped_app"]) for item in history_segments]
        durations = [float(item["dwell_s"]) for item in history_segments]
        input_history_apps, input_durations, input_mask = padded_history(mapped_history, durations, args.history_len)

        raw_open_apps = row.get("open_apps", "")
        mapped_opened = map_open_apps(raw_open_apps)
        raw_fg = row.get("foreground_app", "")
        mapped_fg = map_app(raw_fg)
        base_prediction = {
            "session_id": row.get("session_id", ""),
            "feature_window_id": feature_window_id,
            "timestamp": timestamp,
            "trigger_type": trigger_type,
            "raw_foreground_app": raw_fg,
            "mapped_foreground_app": mapped_fg,
            "raw_open_apps": raw_open_apps,
            "mapped_opened_apps": "|".join(mapped_opened),
            "history_apps": "|".join(input_history_apps),
            "history_durations_s": "|".join(input_durations),
            "history_mask": "|".join(input_mask),
            "score_mode": args.score_mode,
        }

        skip_reason = ""
        if trigger_type == "event_cooldown":
            skip_reason = "event_cooldown"
        elif not trigger_type:
            skip_reason = "no_prediction_trigger"
        elif not any(item == "1" for item in input_mask):
            skip_reason = "no_valid_history"
        elif predictor is None:
            skip_reason = predictor_error

        if skip_reason:
            skipped[skip_reason] += 1
            prediction_rows.append({**base_prediction, "status": "skipped", "skip_reason": skip_reason})
        else:
            assert predictor is not None
            status = "success"
            error = ""
            outputs: list[dict[str, Any]] = []
            try:
                outputs = predictor.predict(mapped_history, durations, mapped_opened, timestamp)
                if not outputs:
                    status = "skipped"
                    error = "predictor_returned_no_rows"
                    skipped[error] += 1
            except Exception as exc:
                status = "error"
                error = f"predictor_error:{exc}"
                skipped[error] += 1

            if status == "success":
                last_prediction_time = current_time
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
                for horizon in [3, 5, 10]:
                    timeline_rows.append({
                        "time": timestamp,
                        "trigger_type": trigger_type,
                        "foreground_app": raw_fg,
                        "mapped_foreground_app": mapped_fg,
                        "opened_apps": raw_open_apps,
                        "mapped_opened_apps": "|".join(mapped_opened),
                        "history_apps": "|".join(input_history_apps),
                        "history_durations_s": "|".join(input_durations),
                        "horizon": horizon,
                        "top1": top_names(outputs, horizon, 1),
                        "top3": top_names(outputs, horizon, 3),
                        "status": "success",
                        "note": mapping_note([raw_fg] + split_apps(raw_open_apps) + raw_history, True),
                    })
            else:
                prediction_rows.append({**base_prediction, "status": status, "skip_reason": error})

            previous_raw = previous_row.get("foreground_app", "") if previous_row else ""
            previous_mapped = map_app(previous_raw) if previous_row else ""
            raw_note_values = [raw_fg] + split_apps(raw_open_apps) + raw_history
            call_trace_rows.append({
                "call_id": call_id,
                "session_id": row.get("session_id", ""),
                "feature_window_id": feature_window_id,
                "timestamp": timestamp,
                "trigger_type": trigger_type,
                "transition_from_raw": previous_raw,
                "transition_to_raw": raw_fg,
                "transition_from_mapped": previous_mapped,
                "transition_to_mapped": mapped_fg,
                "raw_foreground_app": raw_fg,
                "mapped_foreground_app": mapped_fg,
                "raw_open_apps": raw_open_apps,
                "mapped_opened_apps": "|".join(mapped_opened),
                "raw_history_apps": "|".join(raw_history[-args.history_len:]),
                "mapped_history_apps": "|".join(mapped_history[-args.history_len:]),
                "input_history_apps": "|".join(input_history_apps),
                "input_history_durations_s": "|".join(input_durations),
                "input_history_mask": "|".join(input_mask),
                "current_app_dwell_s": fmt_duration(current_dwell_s),
                "dwell_bucket": dwell_bucket,
                "user_group": args.user_group,
                "top_k": args.top_k,
                "score_mode": args.score_mode,
                "device": str(getattr(predictor, "device", args.device)),
                "model_type": "AppLSTMDurationV3",
                "checkpoint": args.checkpoint,
                "output_horizon_3": json_rows_for_horizon(outputs, 3),
                "output_horizon_5": json_rows_for_horizon(outputs, 5),
                "output_horizon_10": json_rows_for_horizon(outputs, 10),
                "status": status,
                "error": error,
            })
            review_call_trace_rows.append({
                "call_id": call_id,
                "time": timestamp,
                "trigger_type": trigger_type,
                "foreground_app": raw_fg,
                "mapped_foreground_app": mapped_fg,
                "opened_apps": raw_open_apps,
                "mapped_opened_apps": "|".join(mapped_opened),
                "history": "|".join(input_history_apps),
                "history_durations_s": "|".join(input_durations),
                "history_mask": "|".join(input_mask),
                "current_app_dwell_s": fmt_duration(current_dwell_s),
                "top3_3min": top_names(outputs, 3),
                "top3_5min": top_names(outputs, 5),
                "top3_10min": top_names(outputs, 10),
                "status": status,
                "note": mapping_note(raw_note_values, True),
            })
            call_id += 1

        previous_row = row
        previous_segment_id = segment_id
        previous_dwell_s = current_dwell_s

    write_csv(model_dir / "app_predictions_duration_1s.csv", PREDICTION_FIELDS, prediction_rows)
    write_csv(review_dir / "app_predictions_duration_timeline.csv", TIMELINE_FIELDS, timeline_rows)
    write_csv(model_dir / "lstm_duration_call_trace.csv", CALL_TRACE_FIELDS, call_trace_rows)
    write_csv(review_dir / "lstm_duration_call_trace.csv", REVIEW_CALL_TRACE_FIELDS, review_call_trace_rows)

    print(f"saved: {model_dir / 'app_state_segments.csv'}")
    print(f"saved: {review_dir / 'app_state_segments.csv'}")
    print(f"saved: {model_dir / 'app_predictions_duration_1s.csv'}")
    print(f"saved: {review_dir / 'app_predictions_duration_timeline.csv'}")
    print(f"saved: {model_dir / 'lstm_duration_call_trace.csv'}")
    print(f"saved: {review_dir / 'lstm_duration_call_trace.csv'}")
    print(f"duration_lstm_calls: {len(call_trace_rows)}")
    print("skipped:")
    for reason, count in sorted(skipped.items()):
        print(f"  {reason}: {count}")
    print("No prefetch, eviction, swap, MGLRU, or memory scheduling action was performed.")
    return 2 if predictor is None else 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run duration-aware LSTM app prediction for a Runtime Monitor session.")
    parser.add_argument("--session-dir", required=True)
    parser.add_argument("--checkpoint", required=True)
    parser.add_argument("--app-vocab", required=True)
    parser.add_argument("--group-vocab", required=True)
    parser.add_argument("--user-group", default="通用用户")
    parser.add_argument("--history-len", type=int, default=5)
    parser.add_argument("--duration-cap-s", type=float, default=600.0)
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--score-mode", choices=["softmax", "sigmoid"], default="sigmoid")
    parser.add_argument("--device", choices=["auto", "cpu", "cuda"], default="auto")
    parser.add_argument("--dwell-buckets", default="5,15,30,60")
    parser.add_argument("--trigger-mode", choices=["event_plus_ttl", "event_only"], default="event_plus_ttl")
    parser.add_argument("--prediction-ttl-s", type=float, default=180.0)
    parser.add_argument("--periodic-refresh-s", type=float, default=180.0)
    parser.add_argument("--min-event-cooldown-s", type=float, default=5.0)
    if hasattr(argparse, "BooleanOptionalAction"):
        parser.add_argument(
            "--disable-dwell-bucket-trigger",
            action=argparse.BooleanOptionalAction,
            default=True,
        )
    else:
        dwell_group = parser.add_mutually_exclusive_group()
        dwell_group.add_argument(
            "--disable-dwell-bucket-trigger",
            dest="disable_dwell_bucket_trigger",
            action="store_true",
        )
        dwell_group.add_argument(
            "--no-disable-dwell-bucket-trigger",
            dest="disable_dwell_bucket_trigger",
            action="store_false",
        )
        parser.set_defaults(disable_dwell_bucket_trigger=True)
    return parser.parse_args()


def main() -> None:
    raise SystemExit(run(parse_args()))


if __name__ == "__main__":
    main()
