#!/usr/bin/env python3
"""Build in-app next-operation samples from operation event logs."""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.utils.io_utils import load_json, read_csv, save_pickle

REQUIRED_FIELDS = {"user_id", "timestamp", "app", "operation", "user_group"}
DEFAULT_GROUP = "通用用户"


def parse_time(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")


def time_feature(ts: datetime) -> list[int]:
    weekday = ts.weekday()
    return [ts.hour, weekday, int(weekday >= 5)]


def build_samples(args: argparse.Namespace) -> list[dict]:
    rows = read_csv(args.input)
    if not rows:
        raise ValueError(f"input file is empty: {args.input}")
    missing = REQUIRED_FIELDS - set(rows[0].keys())
    if missing:
        raise ValueError(f"missing required fields in {args.input}: {sorted(missing)}")

    op_vocab = load_json(args.op_vocab)
    group_vocab = load_json(args.group_vocab)
    default_group_id = group_vocab.get(DEFAULT_GROUP, 0)

    grouped: dict[tuple[str, str], list[dict]] = defaultdict(list)
    skipped_unknown_app = 0
    skipped_pad = 0
    for row in rows:
        app = row["app"]
        if app not in op_vocab:
            skipped_unknown_app += 1
            continue
        operation = row["operation"]
        if operation == "<PAD>":
            skipped_pad += 1
            continue
        vocab = op_vocab[app]
        op_id = int(vocab.get(operation, vocab.get("<UNK>", 1)))
        item = dict(row)
        item["_timestamp"] = parse_time(row["timestamp"])
        item["_op_id"] = op_id
        grouped[(row["user_id"], app)].append(item)

    samples: list[dict] = []
    for (user_id, app), events in grouped.items():
        events.sort(key=lambda x: x["_timestamp"])
        for idx in range(args.history_len, len(events)):
            current = events[idx]
            label = current["_op_id"]
            if label == int(op_vocab[app].get("<PAD>", 0)):
                continue
            now = current["_timestamp"]
            group_id = group_vocab.get(current["user_group"], default_group_id)
            samples.append(
                {
                    "user_id": user_id,
                    "timestamp": current["timestamp"],
                    "app": app,
                    "time_feature": time_feature(now),
                    "user_group": int(group_id),
                    "history_ops": [event["_op_id"] for event in events[idx - args.history_len : idx]],
                    "label_next_op": label,
                }
            )

    print(f"read rows: {len(rows)}")
    print(f"user-app groups: {len(grouped)}")
    print(f"apps with ops: {len(op_vocab)}")
    print(f"skipped unknown app events: {skipped_unknown_app}")
    print(f"skipped PAD events: {skipped_pad}")
    print(f"samples: {len(samples)}")
    return samples


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build in-app operation prediction samples.")
    parser.add_argument("--input", default="data/raw/op_events.csv")
    parser.add_argument("--op-vocab", default="data/vocab/op_vocab.json")
    parser.add_argument("--group-vocab", default="data/vocab/user_group_vocab.json")
    parser.add_argument("--output", default="data/processed/op_samples.pkl")
    parser.add_argument("--history-len", type=int, default=4)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    samples = build_samples(args)
    save_pickle(samples, args.output)
    print(f"saved: {args.output}")


if __name__ == "__main__":
    main()
