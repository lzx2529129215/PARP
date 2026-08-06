#!/usr/bin/env python3
"""Merge global feature labels into per-app feature rows."""

from __future__ import annotations

import argparse
import csv
from pathlib import Path
from typing import Any


MERGE_FIELDS = [
    "foreground_app",
    "open_apps",
    "observed_apps",
    "closed_apps",
    "manual_label",
    "state_label",
    "scenario_id",
    "step_id",
    "action",
]
DERIVED_FIELDS = ["label_app", "is_label_target_app"]


def read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        return list(reader.fieldnames or []), list(reader)


def write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fieldnames})


def key_for(row: dict[str, str]) -> tuple[str, str]:
    return (row.get("session_id", ""), row.get("feature_window_id", ""))


def build_feature_index(features: list[dict[str, str]]) -> tuple[dict[tuple[str, str], dict[str, str]], dict[str, dict[str, str]]]:
    by_session_and_window: dict[tuple[str, str], dict[str, str]] = {}
    by_window: dict[str, dict[str, str]] = {}
    for row in features:
        session_id, window_id = key_for(row)
        if not window_id:
            continue
        by_window.setdefault(window_id, row)
        if session_id:
            by_session_and_window[(session_id, window_id)] = row
    return by_session_and_window, by_window


def label_app_features(
    feature_rows: list[dict[str, str]],
    app_rows: list[dict[str, str]],
) -> list[dict[str, str]]:
    by_session_and_window, by_window = build_feature_index(feature_rows)
    out: list[dict[str, str]] = []
    for row in app_rows:
        item = dict(row)
        session_id, window_id = key_for(row)
        feature = by_session_and_window.get((session_id, window_id)) if session_id else None
        if feature is None:
            feature = by_window.get(window_id)
        for field in MERGE_FIELDS:
            item[field] = feature.get(field, "") if feature else ""
        label_app = parse_label_app(item.get("state_label", "") or item.get("manual_label", ""))
        item["label_app"] = label_app
        item["is_label_target_app"] = "1" if label_app and item.get("app_id") == label_app else "0"
        out.append(item)
    return out


def parse_label_app(label: str) -> str:
    text = str(label or "").upper()
    for app_id in ("WPS", "QQ", "FILES", "FIREFOX"):
        if app_id in text:
            return app_id
    return ""


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Label app_features_1s.csv from features_1s.labeled.csv.")
    parser.add_argument("--features-labeled", required=True, type=Path)
    parser.add_argument("--app-features", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    _, feature_rows = read_csv(args.features_labeled)
    app_fields, app_rows = read_csv(args.app_features)
    fieldnames = list(app_fields)
    for field in MERGE_FIELDS:
        if field not in fieldnames:
            fieldnames.append(field)
    for field in DERIVED_FIELDS:
        if field not in fieldnames:
            fieldnames.append(field)
    labeled_rows = label_app_features(feature_rows, app_rows)
    write_csv(args.output, fieldnames, labeled_rows)
    print(f"rows={len(labeled_rows)} output={args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
