#!/usr/bin/env python3
"""Generate a deterministic Phase2.7-schema fixture; never marks data as real."""

import argparse
import hashlib
import json
from pathlib import Path

from .config import WINDOW_NS
from .dataset_builder import DatasetBuilder
from .operation_alignment import OperationEvent


SOURCE = "SYNTHETIC_PHASE27_SCHEMA_FIXTURE"
OPERATIONS = ("OPEN", "READ", "SCROLL", "EDIT", "SAVE", "SEARCH")


def generate_rows(session_count=6, windows_per_session=12):
    file_rows = []
    anon_rows = []
    operations = []
    base = 10_000_000_000
    for session_index in range(session_count):
        session = f"synthetic-session-{session_index:02d}"
        session_start = base + session_index * 300_000_000_000
        for window_index in range(windows_per_session):
            start = session_start + window_index * WINDOW_NS
            operation = OPERATIONS[(window_index + session_index) % len(OPERATIONS)]
            operations.append(OperationEvent(
                f"op-{session_index}-{window_index}", "synthetic-app",
                start, start + WINDOW_NS, operation, "synthetic", 1.0,
                session))
            active_start = ((window_index * 73) + session_index * 11) % 1100
            for offset, accesses in ((active_start, 150),
                                     ((active_start + 400) % 1100, 40)):
                file_rows.append({
                    "event_time_ns": start + 1_000_000_000 + offset,
                    "sample_timestamp_ns": start + 1_000_000_000 + offset,
                    "boot_id": "synthetic-boot", "session_id": session,
                    "run_id": "phase27-synthetic", "sample_id": window_index,
                    "domain_id": 901, "app_id": "synthetic-app",
                    "dev_major": 8, "dev_minor": 5, "inode": 424242,
                    "file_version": 1, "file_size_bytes": 1200 * 4096,
                    "file_page_count": 1200, "logical_start": offset,
                    "nr_pages": 100, "nr_accesses": accesses, "age": window_index,
                    "sample_interval_us": 5000,
                    "aggregation_interval_us": 1_000_000,
                    "foreground_epoch": session_index + 1,
                    "foreground_state": "FOREGROUND", "rss_bytes": 128 << 20,
                    "pss_bytes": 96 << 20, "swap_bytes": 0,
                    "path_hash": hashlib.sha256(b"synthetic-document").hexdigest(),
                    "file_class": "SYNTHETIC_DOCUMENT",
                })
            anon_rows.append({
                "event_time_ns": start + 2_000_000_000,
                "boot_id": "synthetic-boot", "session_id": session,
                "run_id": "phase27-synthetic", "domain_id": 901,
                "app_id": "synthetic-app", "foreground_epoch": session_index + 1,
                "mm_cookie": session_index + 1000, "vma_signature": 77,
                "nr_pages": 256, "nr_accesses": 100 if window_index % 3 else 0,
                "age": window_index, "sample_interval_us": 5000,
                "aggregation_interval_us": 1_000_000,
            })
    return file_rows, anon_rows, operations


def write_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as stream:
        for row in rows:
            payload = row.__dict__ if hasattr(row, "__dict__") else row
            stream.write(json.dumps(payload, sort_keys=True) + "\n")


def build_fixture(output, session_count=6, windows_per_session=12):
    output = Path(output)
    raw = output / "raw"
    file_rows, anon_rows, operations = generate_rows(
        session_count, windows_per_session)
    write_jsonl(raw / "file_regions.jsonl", file_rows)
    write_jsonl(raw / "anon_regions.jsonl", anon_rows)
    write_jsonl(raw / "operation_events.jsonl", operations)
    metadata = DatasetBuilder(output / "dataset", "phase27-synthetic", SOURCE).build(
        file_rows, anon_rows, operations)
    manifest = {
        "source": SOURCE, "real_application_data": False,
        "eligible_for_training_claims": False,
        "purpose": "schema and executable-pipeline validation only",
        "session_count": session_count,
        "windows_per_session": windows_per_session,
        "metadata": metadata,
    }
    (output / "fixture_manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return manifest


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--sessions", type=int, default=6)
    parser.add_argument("--windows-per-session", type=int, default=12)
    args = parser.parse_args()
    print(json.dumps(build_fixture(args.output, args.sessions,
                                   args.windows_per_session), sort_keys=True))


if __name__ == "__main__":
    main()
