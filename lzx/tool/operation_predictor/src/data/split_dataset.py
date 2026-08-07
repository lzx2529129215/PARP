#!/usr/bin/env python3
"""Chronologically split app or operation samples into train/val/test."""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.utils.io_utils import ensure_dir, load_pickle, save_pickle


def parse_time(sample: dict) -> datetime:
    return datetime.strptime(sample["timestamp"], "%Y-%m-%d %H:%M:%S")


def split(samples: list[dict], train_ratio: float, val_ratio: float, test_ratio: float) -> tuple[list[dict], list[dict], list[dict]]:
    total_ratio = train_ratio + val_ratio + test_ratio
    if abs(total_ratio - 1.0) > 1e-6:
        raise ValueError(f"train/val/test ratios must sum to 1.0, got {total_ratio}")
    samples = sorted(samples, key=parse_time)
    n = len(samples)
    train_end = int(n * train_ratio)
    val_end = train_end + int(n * val_ratio)
    return samples[:train_end], samples[train_end:val_end], samples[val_end:]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Split samples by timestamp.")
    parser.add_argument("--input", required=True)
    parser.add_argument("--task", choices=["app", "op"], required=True)
    parser.add_argument("--output-dir", default="data/processed")
    parser.add_argument("--train-ratio", type=float, default=0.7)
    parser.add_argument("--val-ratio", type=float, default=0.15)
    parser.add_argument("--test-ratio", type=float, default=0.15)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    samples = load_pickle(args.input)
    if not isinstance(samples, list):
        raise ValueError(f"input pickle must contain list[dict]: {args.input}")
    if not samples:
        raise ValueError(f"input samples are empty: {args.input}")

    train, val, test = split(samples, args.train_ratio, args.val_ratio, args.test_ratio)
    output_dir = ensure_dir(args.output_dir)
    save_pickle(train, output_dir / f"train_{args.task}.pkl")
    save_pickle(val, output_dir / f"val_{args.task}.pkl")
    save_pickle(test, output_dir / f"test_{args.task}.pkl")

    print(f"input samples: {len(samples)}")
    print(f"train/val/test: {len(train)}/{len(val)}/{len(test)}")
    print(f"saved to: {output_dir}")


if __name__ == "__main__":
    main()
