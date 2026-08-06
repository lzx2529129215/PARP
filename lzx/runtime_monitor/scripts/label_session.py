#!/usr/bin/env python3
"""Apply one manual label to all rows in a features_1s.csv file."""

from __future__ import annotations

import csv
import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) != 4:
        print("usage: label_session.py <input_features.csv> <output_features.csv> <LABEL>", file=sys.stderr)
        return 2
    src = Path(sys.argv[1])
    dst = Path(sys.argv[2])
    label = sys.argv[3]
    with src.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        fieldnames = list(reader.fieldnames or [])
    if "manual_label" not in fieldnames:
        fieldnames.append("manual_label")
    for row in rows:
        row["manual_label"] = label
    dst.parent.mkdir(parents=True, exist_ok=True)
    with dst.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    print(f"saved: {dst}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
