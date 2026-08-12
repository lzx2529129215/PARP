#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0
"""Create a dependency-free Markdown summary from data-readiness metrics."""

import argparse
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("metrics", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    metrics = json.loads(args.metrics.read_text(encoding="utf-8"))
    lines = ["# PARP Phase 2 data summary", "", "| Metric | Value |", "|---|---:|"]
    for key, value in sorted(metrics.items()):
        lines.append(f"| `{key}` | {value} |")
    args.output.write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
