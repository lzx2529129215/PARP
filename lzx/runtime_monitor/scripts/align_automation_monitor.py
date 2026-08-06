#!/usr/bin/env python3
"""Align automation_trace.csv with runtime_monitor features_1s.csv."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import sys
from pathlib import Path
from typing import Any


LABEL_FIELDS = [
    "session_id",
    "scenario_id",
    "step_id",
    "action",
    "app",
    "label",
    "start_ns",
    "end_ns",
    "duration_ms",
    "source",
]


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fieldnames})


def to_int(value: str) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def build_labels(trace_rows: list[dict[str, str]]) -> list[dict[str, Any]]:
    starts: dict[tuple[str, str, str], dict[str, str]] = {}
    labels: list[dict[str, Any]] = []
    for row in trace_rows:
        key = (row.get("session_id", ""), row.get("scenario_id", ""), row.get("step_id", ""))
        if row.get("phase") == "start":
            starts[key] = row
            continue
        if row.get("phase") != "end":
            continue
        start = starts.get(key)
        if not start:
            print(f"warning: trace end without start: {key}", file=sys.stderr)
            continue
        start_ns = to_int(start.get("ts_ns", ""))
        end_ns = to_int(row.get("ts_ns", ""))
        if not start_ns or not end_ns or end_ns < start_ns:
            print(f"warning: invalid trace interval: {key}", file=sys.stderr)
            continue
        labels.append(
            {
                "session_id": row.get("session_id") or start.get("session_id", ""),
                "scenario_id": row.get("scenario_id") or start.get("scenario_id", ""),
                "step_id": row.get("step_id") or start.get("step_id", ""),
                "action": row.get("action") or start.get("action", ""),
                "app": row.get("app") or start.get("app", ""),
                "label": row.get("label") or start.get("label", ""),
                "start_ns": start_ns,
                "end_ns": end_ns,
                "duration_ms": f"{(end_ns - start_ns) / 1_000_000:.3f}",
                "source": "automation_trace",
            }
        )
    return labels


def parse_timestamp_ns(value: str) -> int:
    if not value:
        return 0
    try:
        numeric = float(value)
        if numeric > 1e12:
            return int(numeric)
        return int(numeric * 1_000_000_000)
    except ValueError:
        pass
    try:
        parsed = dt.datetime.fromisoformat(value)
        return int(parsed.timestamp() * 1_000_000_000)
    except ValueError:
        return 0


def feature_window(row: dict[str, str]) -> tuple[int, int]:
    start = to_int(row.get("window_start_ns", ""))
    end = to_int(row.get("window_end_ns", ""))
    if start and end:
        return start, end
    center = parse_timestamp_ns(row.get("timestamp", ""))
    if not center:
        return 0, 0
    return center, center + 1_000_000_000


def overlap(start_a: int, end_a: int, start_b: int, end_b: int) -> int:
    if start_a < end_b and end_a > start_b:
        return max(0, min(end_a, end_b) - max(start_a, start_b))
    return 0


def choose_label(feature_start: int, feature_end: int, labels: list[dict[str, Any]]) -> dict[str, Any] | None:
    matches: list[tuple[int, int, dict[str, Any]]] = []
    for label in labels:
        ov = overlap(feature_start, feature_end, int(label["start_ns"]), int(label["end_ns"]))
        if ov <= 0:
            continue
        non_wait = 1 if label.get("label") != "WAIT" else 0
        matches.append((non_wait, ov, label))
    if not matches:
        return None
    matches.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return matches[0][2]


def matching_labels(feature_start: int, feature_end: int, labels: list[dict[str, Any]]) -> list[tuple[int, dict[str, Any]]]:
    matches: list[tuple[int, dict[str, Any]]] = []
    for label in labels:
        ov = overlap(feature_start, feature_end, int(label["start_ns"]), int(label["end_ns"]))
        if ov > 0:
            matches.append((ov, label))
    return matches


def choose_state_label(matches: list[tuple[int, dict[str, Any]]], previous: str) -> str:
    non_wait = [(ov, label) for ov, label in matches if label.get("label") != "WAIT"]
    if non_wait:
        non_wait.sort(key=lambda item: item[0], reverse=True)
        return str(non_wait[0][1].get("label", ""))
    if matches:
        return previous
    return previous


def label_features(
    features: list[dict[str, str]],
    labels: list[dict[str, Any]],
    state_label_mode: str = "",
) -> tuple[list[str], list[dict[str, str]]]:
    fieldnames = list(features[0].keys()) if features else []
    for field in ("manual_label", "scenario_id", "step_id", "action"):
        if field not in fieldnames:
            fieldnames.append(field)
    if state_label_mode == "carry-forward" and "state_label" not in fieldnames:
        fieldnames.append("state_label")
    out: list[dict[str, str]] = []
    previous_state_label = ""
    for row in features:
        item = dict(row)
        start, end = feature_window(row)
        matches = matching_labels(start, end, labels) if start and end else []
        chosen = choose_label(start, end, labels) if start and end else None
        if chosen:
            item["manual_label"] = str(chosen.get("label", ""))
            item["scenario_id"] = str(chosen.get("scenario_id", ""))
            item["step_id"] = str(chosen.get("step_id", ""))
            item["action"] = str(chosen.get("action", ""))
        else:
            item.setdefault("manual_label", "")
            item.setdefault("scenario_id", "")
            item.setdefault("step_id", "")
            item.setdefault("action", "")
        if state_label_mode == "carry-forward":
            state_label = choose_state_label(matches, previous_state_label)
            if state_label:
                previous_state_label = state_label
            item["state_label"] = state_label
        out.append(item)
    return fieldnames, out


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Align automation trace labels to runtime monitor features.")
    parser.add_argument("--features", required=True, type=Path)
    parser.add_argument("--trace", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--labels-output", required=True, type=Path)
    parser.add_argument("--state-label-mode", choices=["", "carry-forward"], default="")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    trace_rows = read_csv(args.trace)
    labels = build_labels(trace_rows)
    write_csv(args.labels_output, LABEL_FIELDS, labels)

    features = read_csv(args.features)
    feature_fields, labeled = label_features(features, labels, args.state_label_mode)
    write_csv(args.output, feature_fields, labeled)
    print(f"labels={len(labels)} output={args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
