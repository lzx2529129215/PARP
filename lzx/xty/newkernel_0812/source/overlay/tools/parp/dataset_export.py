#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0
"""Export synthetic or decoded PARP Phase-2 JSONL/CSV datasets."""

import argparse
import csv
import json
from pathlib import Path
import sys

from region_schema import Q15_ONE, VMA, split_region, public_record
from window_analyze import aggregate, domain_windows, region_key


def synthetic_records():
    base_ns = 10_000_000_000_000
    vmas = [
        VMA(0x10000, 0x18000, "file", vm_pgoff=7, dev_major=8,
            dev_minor=1, inode=101, file_version=3,
            backing_class="REGULAR_FILE"),
        VMA(0x1a000, 0x22000, "heap", flags=3),
        VMA(0x22000, 0x26000, "stack", flags=0x103),
    ]
    records = []
    sample_id = 1
    for age_seconds, accesses in ((50, 1), (20, 2), (0, 3)):
        timestamp = base_ns - age_seconds * 1_000_000_000
        for segment in split_region(0x12000, 0x24000, vmas):
            row = {
                "schema_version": 1, "timestamp_ns": timestamp,
                "sample_id": sample_id, "target_cookie": 44,
                "pid": 1001, "tgid": 1001, "mm_cookie": 55,
                "domain_id": 7, "app_id": 9, "foreground_epoch_id": 11,
                "bind_generation": 2, "bind_expiry": base_ns + 60_000_000_000,
                "owner_source": "OBSERVATION_OWNER_TASK_MEMCG",
                "owner_confidence": Q15_ONE,
                "region_start": segment["start"], "region_end": segment["end"],
                "nr_accesses": accesses, "age": age_seconds,
                "damon_sampling_interval_us": 5000,
                "damon_aggregation_interval_us": 1_000_000,
                "alignment_status": segment.get("alignment_status", "UNRESOLVED"),
                "alignment_confidence": segment.get("alignment_confidence", 0),
                "reason_flags": 0, "source": "SYNTHETIC_LEVEL1",
                **{key: value for key, value in segment.items()
                   if key not in {"start", "end"}},
            }
            records.append(public_record(row, synthetic=True))
            sample_id += 1
    return records


def read_jsonl(path: Path):
    with path.open(encoding="utf-8") as source:
        return [json.loads(line) for line in source if line.strip()]


def write_jsonl(path: Path, rows):
    with path.open("w", encoding="utf-8") as target:
        for row in rows:
            target.write(json.dumps(row, sort_keys=True) + "\n")


def write_csv(path: Path, rows):
    rows = list(rows)
    fields = sorted({key for row in rows for key in row})
    with path.open("w", newline="", encoding="utf-8") as target:
        writer = csv.DictWriter(target, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def export(records, output: Path, source_kind: str):
    output.mkdir(parents=True, exist_ok=True)
    write_jsonl(output / "raw_regions.jsonl", records)
    subsets = {
        "file_regions.jsonl": [row for row in records if row["region_type"] == "FILE"],
        "anon_regions.jsonl": [row for row in records if row["region_type"] == "ANON"],
        "unresolved_regions.jsonl": [row for row in records
                                     if row["region_type"] in {"UNRESOLVED", "SPECIAL"}],
    }
    for name, rows in subsets.items():
        write_jsonl(output / name, rows)
    summaries, quality = aggregate(records)
    region_rows = []
    for key, summary in summaries.items():
        region_rows.append({"region_key": json.dumps(key), **summary})
    write_csv(output / "file_region_windows.csv",
              [row for row in region_rows if json.loads(row["region_key"])[0] == "FILE"])
    write_csv(output / "anon_region_windows.csv",
              [row for row in region_rows if json.loads(row["region_key"])[0] == "ANON"])
    for window in (10, 30, 60):
        write_csv(output / f"domain_windows_{window}s.csv",
                  domain_windows(records, window))
    total = len(records)
    exact = sum(row.get("alignment_status") in {"EXACT", "SPLIT_EXACT"}
                for row in records)
    unresolved = len(subsets["unresolved_regions.jsonl"])
    statuses = {name: sum(row.get("alignment_status") == name for row in records)
                for name in ("EXACT", "SPLIT_EXACT", "PARTIAL", "AMBIGUOUS",
                             "STALE", "UNRESOLVED")}
    total_bytes = sum(row["region_end"] - row["region_start"] for row in records)
    exact_bytes = sum(row["region_end"] - row["region_start"] for row in records
                      if row.get("alignment_status") in {"EXACT", "SPLIT_EXACT"})
    alignment = {
        "records": total, "exact_records": exact,
        "exact_alignment_rate": exact / total if total else 0,
        "unresolved_records": unresolved,
        "unresolved_rate": unresolved / total if total else 0,
        "duplicates": quality["duplicates"],
        "out_of_order": quality["out_of_order"],
        "source_kind": source_kind,
        "status_counts": statuses,
        "byte_weighted_coverage": exact_bytes / total_bytes if total_bytes else 0,
        "region_count_coverage": exact / total if total else 0,
    }
    (output / "alignment_summary.json").write_text(
        json.dumps(alignment, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    metadata = {
        "schema_version": 1, "source_kind": source_kind,
        "runtime_data": source_kind == "RUNTIME",
        "records": total, "address_policy": "synthetic or redacted logical ranges",
        "damon_nr_accesses_semantics": "sampling evidence per aggregation, not CPU accesses",
    }
    (output / "collection_metadata.json").write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--synthetic", action="store_true")
    group.add_argument("--input", type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    if args.synthetic:
        records, source_kind = synthetic_records(), "SYNTHETIC_LEVEL1"
    else:
        records, source_kind = read_jsonl(args.input), "OFFLINE_IMPORTED"
        records = [public_record(record) for record in records]
    export(records, args.output_dir, source_kind)


if __name__ == "__main__":
    main()
