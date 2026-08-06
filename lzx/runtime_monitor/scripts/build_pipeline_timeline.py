#!/usr/bin/env python3
"""从指定 session 和 kernel 快照构建不重复的原始事件时间线。"""

from __future__ import annotations

import argparse
import csv
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from audit_common import PROJECT_ROOT, event_type, integer, json_text, parse_ns, read_csv, resolve_input_path, row_ns

FIELDS = [
    "event_time_ns", "event_time", "event_type", "event_origin", "session_id", "app_key",
    "app_name", "model_app_id", "runtime_app_id", "cgroup_id", "scope_name", "foreground_app",
    "horizon_minutes", "raw_logit", "probability", "probability_fixed", "requested_workload_id",
    "observed_workload_id", "prev_workload_id", "current_workload_id", "next_workload_id",
    "prediction_id", "confidence", "confidence_fixed", "resolution_status", "hit", "command",
    "status", "snapshot_name", "line", "source_file", "source_row", "details_json",
]


def iso_time(ns: int) -> str:
    return datetime.fromtimestamp(ns / 1_000_000_000).isoformat(timespec="milliseconds") if ns else ""


def add_event(events: list[dict[str, Any]], kind: str, row: dict[str, Any], source: Path, source_row: int,
              origin: str = "RAW_RUNTIME", ns: int | None = None, **extra: Any) -> None:
    ns = row_ns(row) if ns is None else ns
    mapped = {field: "" for field in FIELDS}
    mapped.update({
        "event_time_ns": ns,
        "event_time": iso_time(ns),
        "event_type": kind,
        "event_origin": origin,
        "session_id": row.get("session_id", ""),
        "app_key": row.get("app_key", ""),
        "app_name": row.get("app_name", "") or row.get("app", ""),
        "model_app_id": row.get("model_app_id", "") or row.get("app_id", "") if kind in {"LSTM_PREDICTION", "LSTM_CALL"} else row.get("model_app_id", ""),
        "runtime_app_id": row.get("runtime_app_id", "") if row.get("runtime_app_id") is not None else (row.get("app_id", "") if kind not in {"LSTM_PREDICTION", "LSTM_CALL"} else ""),
        "cgroup_id": row.get("cgroup_id", "") or row.get("foreground_cgroup_id", ""),
        "scope_name": row.get("scope_name", ""),
        "foreground_app": row.get("mapped_foreground_app", "") or row.get("raw_foreground_app", ""),
        "horizon_minutes": row.get("horizon", ""),
        "raw_logit": row.get("raw_logit", "") or row.get("logit", ""),
        "probability": row.get("probability", ""),
        "probability_fixed": row.get("probability_fixed", ""),
        "requested_workload_id": row.get("requested_workload_id", ""),
        "observed_workload_id": row.get("observed_workload_id", ""),
        "prev_workload_id": row.get("prev_workload_id", ""),
        "current_workload_id": row.get("current_workload_id", ""),
        "next_workload_id": row.get("next_workload_id", "") or row.get("observed_next_workload_id", "") or row.get("predicted_next_workload_id", ""),
        "prediction_id": row.get("prediction_id", ""),
        "confidence": row.get("confidence", ""),
        "confidence_fixed": row.get("confidence_fixed", ""),
        "resolution_status": row.get("resolution_status", ""),
        "hit": row.get("hit", ""),
        "command": row.get("command", ""),
        "status": row.get("status", ""),
        "source_file": str(source),
        "source_row": source_row,
    })
    mapped.update(extra)
    mapped["details_json"] = json_text(dict(row, **extra))
    events.append(mapped)


def read_model_events(model: Path, events: list[dict[str, Any]]) -> None:
    def each(name: str):
        path = model / name
        if not path.exists():
            return []
        return [(path, index, row) for index, row in enumerate(read_csv(path), 2)]

    for path, index, row in each("online_lstm_duration_call_trace.csv"):
        add_event(events, "LSTM_CALL", row, path, index)
    for path, index, row in each("online_app_predictions_duration_1s.csv"):
        add_event(events, "LSTM_PREDICTION", row, path, index)
    for path, index, row in each("cgroup_metrics_1s.csv"):
        add_event(events, "CGROUP_METRIC_SAMPLE", row, path, index)
    for path, index, row in each("workload_classifier_results_1s.csv"):
        add_event(events, "WORKLOAD_CLASSIFICATION", row, path, index)
        if str(row.get("state_changed", "")).lower() == "true":
            add_event(events, "WORKLOAD_STATE_CHANGE", row, path, index)
    for path, index, row in each("workload_markov_online_updates.csv"):
        add_event(events, "MARKOV_TRANSITION_UPDATE", row, path, index)
    for path, index, row in each("workload_markov_online_predictions.csv"):
        add_event(events, "MARKOV_PREDICTION_CREATED", row, path, index)
        if row.get("resolution_status") == "RESOLVED":
            resolved = dict(row)
            resolved["timestamp_ns"] = row.get("actual_next_time_ns", "")
            add_event(events, "MARKOV_PREDICTION_RESOLVED", resolved, path, index, ns=parse_ns(row.get("actual_next_time_ns")))

    # mglru_markov_debugfs_writes is the authoritative runtime write log.  The
    # online and lstm-specific CSVs are derived/duplicate views and are not
    # inserted again into the main timeline.
    writes = model / "mglru_markov_debugfs_writes.csv"
    for path, index, row in ([(writes, i, r) for i, r in enumerate(read_csv(writes), 2)] if writes.exists() else []):
        kind = event_type(row)
        if kind in {"current_app", "app_current"}:
            event = "APP_CURRENT_WRITE"
        elif kind in {"app_bind", "bind"}:
            event = "APP_BIND_WRITE"
        elif kind in {"app_probability", "probability"}:
            event = "APP_PROBABILITY_WRITE"
        elif kind == "workload_update":
            event = "WORKLOAD_UPDATE_WRITE"
        elif kind == "markov_set":
            event = "MARKOV_SET_WRITE"
        elif kind in {"predicted_apps", "app_predict"}:
            event = "APP_PREDICT_WRITE"
        else:
            continue
        add_event(events, event, row, path, index)


def read_kernel_events(root: Path, events: list[dict[str, Any]]) -> None:
    kernel = root / "kernel"
    for name in ("debugfs_baseline_after_clear.txt", "debugfs_after.txt"):
        path = kernel / name
        if not path.exists():
            continue
        ns = path.stat().st_mtime_ns
        add_event(events, "KERNEL_DEBUGFS_SNAPSHOT", {"session_id": "", "status": "snapshot"}, path, 0, "RAW_KERNEL_SNAPSHOT", ns, snapshot_name=name)
        for index, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            if line.startswith("hint "):
                add_event(events, "KERNEL_MARKOV_HINT", {"session_id": "", "status": "snapshot"}, path, index, "RAW_KERNEL_SNAPSHOT", ns, line=line)


def build_timeline(source_roots: list[Path], output: Path) -> int:
    session_roots = [path for path in source_roots if (path / "model").is_dir()]
    kernel_roots = [path for path in source_roots if (path / "kernel").is_dir()]
    if not session_roots:
        raise FileNotFoundError("没有找到包含 model/ 的指定 session 目录")
    events: list[dict[str, Any]] = []
    read_model_events(session_roots[0] / "model", events)
    for root in kernel_roots:
        read_kernel_events(root, events)
    events.sort(key=lambda row: (integer(row.get("event_time_ns")), str(row.get("event_type")), str(row.get("source_file")), integer(row.get("source_row"))))
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(events)
    return len(events)


def main() -> int:
    parser = argparse.ArgumentParser(description="构建单 session 原始事件时间线")
    parser.add_argument("--source-root", action="append", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    roots = [resolve_input_path(value) for value in args.source_root]
    print(build_timeline(roots, resolve_input_path(args.output)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
