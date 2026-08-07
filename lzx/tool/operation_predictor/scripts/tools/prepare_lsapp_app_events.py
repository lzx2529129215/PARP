#!/usr/bin/env python3
"""Prepare LSApp application events for the app-level prediction pipeline."""

from __future__ import annotations

import argparse
import csv
import gzip
import sys
from pathlib import Path
from typing import Iterable, TextIO

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.utils.io_utils import ensure_dir, load_json

DEFAULT_INPUT = "data/raw/datasets/LSApp/after_mapping/add_opened_apps/lsapp_mapped_with_opened.clean.tsv"
DEFAULT_OUTPUT = "data/raw/lsapp/app_events.csv"
DEFAULT_GROUP = "通用用户"
KEEP_EVENTS = {"Opened", "User Interaction"}
STATE_EVENTS = KEEP_EVENTS | {"Closed"}
NAME_FIXES = {"大宗点评": "大众点评"}


def normalize_name(name: str) -> str:
    value = name.strip()
    return NAME_FIXES.get(value, value)


def open_text(path: Path) -> TextIO:
    if path.suffix == ".gz":
        return gzip.open(path, "rt", encoding="utf-8", errors="replace", newline="")
    return path.open("r", encoding="utf-8", errors="replace", newline="")


def find_column(fieldnames: Iterable[str], target: str, fallback: int) -> str:
    fields = list(fieldnames)
    lowered = [field.strip().lower() for field in fields]
    if target in lowered:
        return fields[lowered.index(target)]
    for field, lowered_field in zip(fields, lowered):
        if target == "user_id" and lowered_field.endswith("user_id"):
            return field
    if fallback < len(fields):
        return fields[fallback]
    raise ValueError(f"cannot find column {target!r} in header: {fields}")


def dedupe_keep_order(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value and value not in seen:
            result.append(value)
            seen.add(value)
    return result


def add_opened(opened_apps: list[str], app: str) -> None:
    if app not in opened_apps:
        opened_apps.append(app)


def close_opened(opened_apps: list[str], app: str) -> None:
    opened_apps[:] = [opened for opened in opened_apps if opened != app]


def prepare(args: argparse.Namespace) -> dict[str, int]:
    input_path = Path(args.input)
    output_path = Path(args.output)
    if not input_path.exists():
        raise FileNotFoundError(input_path)

    app_vocab = load_json(args.app_vocab)
    ensure_dir(output_path.parent)

    stats = {
        "read_rows": 0,
        "written_rows": 0,
        "skipped_event_type": 0,
        "skipped_unknown_foreground": 0,
        "fixed_foreground_names": 0,
        "added_foreground_to_opened": 0,
        "bad_rows": 0,
    }

    with open_text(input_path) as fin, output_path.open("w", encoding="utf-8", newline="") as fout:
        reader = csv.DictReader(fin, delimiter="\t")
        if not reader.fieldnames:
            raise ValueError(f"empty input file: {input_path}")

        user_col = find_column(reader.fieldnames, "user_id", 0)
        session_col = find_column(reader.fieldnames, "session_id", 1)
        ts_col = find_column(reader.fieldnames, "timestamp", 2)
        app_col = find_column(reader.fieldnames, "app_name", 3)
        event_col = find_column(reader.fieldnames, "event_type", 4)

        writer = csv.DictWriter(
            fout,
            fieldnames=["user_id", "timestamp", "foreground_app", "opened_apps", "user_group"],
        )
        writer.writeheader()

        opened_by_session: dict[tuple[str, str], list[str]] = {}
        for row in reader:
            stats["read_rows"] += 1
            try:
                event_type = row.get(event_col, "").strip()
                if event_type not in STATE_EVENTS:
                    stats["skipped_event_type"] += 1
                    continue

                raw_app = row.get(app_col, "")
                app = normalize_name(raw_app)
                if app != raw_app.strip():
                    stats["fixed_foreground_names"] += 1
                app_in_vocab = app in app_vocab
                user_id = row.get(user_col, "").strip()
                session_id = row.get(session_col, "").strip()
                session_key = (user_id, session_id)
                opened_apps = opened_by_session.setdefault(session_key, [])

                if app_in_vocab:
                    if event_type == "Closed":
                        close_opened(opened_apps, app)
                    elif event_type == "Opened":
                        add_opened(opened_apps, app)
                    elif event_type == "User Interaction":
                        if app not in opened_apps:
                            stats["added_foreground_to_opened"] += 1
                            add_opened(opened_apps, app)

                if event_type not in KEEP_EVENTS:
                    stats["skipped_event_type"] += 1
                    continue

                if not app_in_vocab:
                    stats["skipped_unknown_foreground"] += 1
                    continue

                opened_apps = dedupe_keep_order(opened_apps)

                writer.writerow(
                    {
                        "user_id": user_id,
                        "timestamp": row.get(ts_col, "").strip(),
                        "foreground_app": app,
                        "opened_apps": ";".join(opened_apps),
                        "user_group": args.user_group,
                    }
                )
                stats["written_rows"] += 1
            except Exception:
                stats["bad_rows"] += 1
                if args.strict:
                    raise

            if args.limit and stats["read_rows"] >= args.limit:
                break

    return stats


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Prepare LSApp app events for v2 LSTM training.")
    parser.add_argument("--input", default=DEFAULT_INPUT)
    parser.add_argument("--output", default=DEFAULT_OUTPUT)
    parser.add_argument("--app-vocab", default="data/vocab/app_vocab.json")
    parser.add_argument("--user-group", default=DEFAULT_GROUP)
    parser.add_argument("--limit", type=int, default=0, help="Optional input row limit for smoke tests.")
    parser.add_argument("--strict", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    stats = prepare(args)
    for key, value in stats.items():
        print(f"{key}: {value}")
    print(f"saved: {args.output}")


if __name__ == "__main__":
    main()
