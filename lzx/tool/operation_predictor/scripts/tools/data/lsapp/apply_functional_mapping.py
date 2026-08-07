#!/usr/bin/env python3
"""Apply an auditable functional LSApp -> Linux-app mapping.

The source remains untouched. Unmapped LSApp names are kept in the output so
the normal preparation step can drop them against the test1 vocabulary while
the report records exactly what was excluded.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
from collections import Counter
from pathlib import Path
from typing import TextIO


def open_text(path: Path, mode: str) -> TextIO:
    if path.suffix == ".gz":
        gzip_mode = mode if "t" in mode else f"{mode}t"
        return gzip.open(path, gzip_mode, encoding="utf-8", errors="replace", newline="")
    return path.open(mode, encoding="utf-8", errors="replace", newline="")


def load_mapping(path: Path, vocab_path: Path) -> tuple[dict[str, str], list[str]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    vocab = json.loads(vocab_path.read_text(encoding="utf-8"))
    mapping: dict[str, str] = {}
    for rule in payload.get("mapping_rules", []):
        target = str(rule["mapped_app"])
        if target not in vocab:
            raise ValueError(f"mapping target {target!r} is not in {vocab_path}")
        for source in rule.get("lsapp_apps", []):
            source = str(source).strip()
            if not source:
                continue
            if source in mapping and mapping[source] != target:
                raise ValueError(f"duplicate source with conflicting targets: {source!r}")
            mapping[source] = target
    return mapping, list(vocab)


def apply_mapping(args: argparse.Namespace) -> dict[str, object]:
    mapping, vocab_names = load_mapping(Path(args.mapping), Path(args.app_vocab))
    counts = Counter()
    target_counts = Counter()
    unmapped_counts = Counter()
    total = 0
    mapped = 0

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with open_text(args.input, "r") as source, open_text(args.output, "w") as target:
        reader = csv.DictReader(source, delimiter="\t")
        if not reader.fieldnames:
            raise ValueError(f"input has no header: {args.input}")
        app_column = next((name for name in reader.fieldnames if name.strip().lower() == "app_name"), None)
        if app_column is None:
            raise ValueError(f"input has no app_name column: {reader.fieldnames}")
        writer = csv.DictWriter(target, fieldnames=reader.fieldnames, delimiter="\t", lineterminator="\n")
        writer.writeheader()
        for row in reader:
            total += 1
            source_app = (row.get(app_column) or "").strip()
            counts[source_app] += 1
            target_app = mapping.get(source_app)
            if target_app:
                row[app_column] = target_app
                mapped += 1
                target_counts[target_app] += 1
            else:
                unmapped_counts[source_app] += 1
            writer.writerow(row)

    report = {
        "input": str(args.input),
        "output": str(args.output),
        "mapping": str(args.mapping),
        "vocab": vocab_names,
        "mapping_entries": len(mapping),
        "total_rows": total,
        "mapped_rows": mapped,
        "unmapped_rows": total - mapped,
        "mapped_fraction": mapped / total if total else 0.0,
        "mapped_target_counts": dict(target_counts),
        "unmapped_app_counts": dict(unmapped_counts.most_common()),
        "source_app_counts": dict(counts.most_common()),
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--mapping", type=Path, required=True)
    parser.add_argument("--app-vocab", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    return parser.parse_args()


def main() -> None:
    report = apply_mapping(parse_args())
    for key in ("total_rows", "mapped_rows", "unmapped_rows", "mapped_fraction", "mapping_entries"):
        print(f"{key}: {report[key]}")
    print(f"saved: {report['output']}")
    print(f"report: {report['mapping']}")


if __name__ == "__main__":
    main()
