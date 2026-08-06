#!/usr/bin/env python3
"""Build application-level multi-label samples from app event logs."""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.utils.io_utils import load_json, read_csv, save_pickle

REQUIRED_FIELDS = {"user_id", "timestamp", "foreground_app", "opened_apps", "user_group"}
DEFAULT_GROUP = "通用用户"


def parse_time(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")


def time_feature(ts: datetime) -> list[int]:
    weekday = ts.weekday()
    return [ts.hour, weekday, int(weekday >= 5)]


def multihot(ids: set[int], size: int) -> list[int]:
    vec = [0] * size
    for item_id in ids:
        if 0 <= item_id < size:
            vec[item_id] = 1
    return vec


def build_samples(args: argparse.Namespace) -> list[dict]:
    rows = read_csv(args.input)
    if not rows:
        raise ValueError(f"input file is empty: {args.input}")
    missing = REQUIRED_FIELDS - set(rows[0].keys())
    if missing:
        raise ValueError(f"missing required fields in {args.input}: {sorted(missing)}")

    app_vocab = load_json(args.app_vocab)
    group_vocab = load_json(args.group_vocab)
    app_count = len(app_vocab)
    default_group_id = group_vocab.get(DEFAULT_GROUP, 0)

    grouped: dict[str, list[dict]] = defaultdict(list)
    skipped_unknown_app = 0
    for row in rows:
        app = row["foreground_app"]
        if app not in app_vocab:
            skipped_unknown_app += 1
            continue
        item = dict(row)
        item["_timestamp"] = parse_time(row["timestamp"])
        item["_app_id"] = int(app_vocab[app])
        grouped[row["user_id"]].append(item)

    samples: list[dict] = []
    horizons = sorted(set(args.horizons))
    max_horizon = max(horizons)
    for user_id, events in grouped.items():
        events.sort(key=lambda x: x["_timestamp"])
        for idx in range(args.history_len, len(events)):
            current = events[idx]
            now = current["_timestamp"]
            history = [event["_app_id"] for event in events[idx - args.history_len : idx]]

            opened_ids: set[int] = set()
            for app in current["opened_apps"].split(";"):
                app = app.strip()
                if app in app_vocab:
                    opened_ids.add(int(app_vocab[app]))

            labels_by_horizon: dict[int, set[int]] = {horizon: set() for horizon in horizons}
            until_by_horizon = {
                horizon: now + timedelta(minutes=horizon)
                for horizon in horizons
            }
            max_until = now + timedelta(minutes=max_horizon)
            for event in events[idx + 1 :]:
                event_time = event["_timestamp"]
                if event_time > max_until:
                    break
                if event_time <= now:
                    continue
                for horizon in horizons:
                    if event_time <= until_by_horizon[horizon]:
                        labels_by_horizon[horizon].add(event["_app_id"])

            if not any(labels_by_horizon.values()):
                continue

            group_id = group_vocab.get(current["user_group"], default_group_id)
            sample = {
                "user_id": user_id,
                "timestamp": current["timestamp"],
                "time_feature": time_feature(now),
                "user_group": int(group_id),
                "history_apps": history,
                "opened_apps": multihot(opened_ids, app_count),
            }
            for horizon in horizons:
                sample[f"label_{horizon}min"] = multihot(labels_by_horizon[horizon], app_count)
            samples.append(sample)

    print(f"read rows: {len(rows)}")
    print(f"users: {len(grouped)}")
    print(f"apps: {app_count}")
    print(f"skipped unknown app events: {skipped_unknown_app}")
    print(f"samples: {len(samples)}")
    return samples


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build application prediction samples.")
    parser.add_argument("--input", default="data/raw/app_events.csv")
    parser.add_argument("--app-vocab", default="data/vocab/app_vocab.json")
    parser.add_argument("--group-vocab", default="data/vocab/user_group_vocab.json")
    parser.add_argument("--output", default="data/processed/app_samples.pkl")
    parser.add_argument("--history-len", type=int, default=5)
    parser.add_argument("--horizons", nargs="+", type=int, default=[3, 5, 10])
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    samples = build_samples(args)
    save_pickle(samples, args.output)
    print(f"saved: {args.output}")


if __name__ == "__main__":
    main()
