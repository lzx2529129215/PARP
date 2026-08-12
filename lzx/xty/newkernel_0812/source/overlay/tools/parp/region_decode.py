#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0
"""Decode key=value PARP region trace lines without exposing raw pointers."""

import re
from typing import Dict, Optional

FIELD = re.compile(r"([a-z_][a-z0-9_]*)=([0-9a-fx]+)")
TRACE_TIMESTAMP = re.compile(r"\s(\d+)\.(\d{1,9}):\s+parp_region_evidence:")


def decode_trace_line(line: str) -> Optional[Dict]:
    if "parp_region_evidence" not in line and "sample=" not in line:
        return None
    values = {key: int(value, 0) for key, value in FIELD.findall(line)}
    required = {"sample", "pid", "tgid", "domain", "app", "mm_cookie",
                "type", "align", "logical_start", "nr_pages",
                "access_evidence", "age", "confidence_q15", "reasons"}
    if not required.issubset(values):
        return None
    record = {
        "schema_version": 1,
        "sample_id": values["sample"], "pid": values["pid"],
        "tgid": values["tgid"], "domain_id": values["domain"],
        "app_id": values["app"], "mm_cookie": values["mm_cookie"],
        "region_type": "FILE" if values["type"] == 0 else "ANON",
        "alignment_status": values["align"],
        "logical_start": values["logical_start"],
        "nr_pages": values["nr_pages"],
        "nr_accesses": values["access_evidence"], "age": values["age"],
        "alignment_confidence": values["confidence_q15"],
        "reason_flags": values["reasons"], "source": "parp_region_evidence",
        "address_redacted": True,
    }
    extended = {
        "sample_time": "sample_timestamp_ns",
        "bind_generation": "bind_generation",
        "foreground_epoch": "foreground_epoch",
        "model": "model_version",
        "region_start": "region_start",
        "region_end": "region_end",
        "dev_major": "dev_major",
        "dev_minor": "dev_minor",
        "inode": "inode",
        "file_version": "file_version",
        "file_size": "file_size_bytes",
        "file_pages": "file_page_count",
        "vma_signature": "vma_signature",
        "sample_us": "sample_interval_us",
        "aggregation_us": "aggregation_interval_us",
    }
    for trace_name, output_name in extended.items():
        if trace_name in values:
            record[output_name] = values[trace_name]
    if "file_page_count" in record:
        record["file_page_start"] = record["logical_start"]
        record["file_page_end_exclusive"] = (
            record["logical_start"] + record["nr_pages"])
        record["max_possible_accesses"] = max(
            1, (record["aggregation_interval_us"] +
                record["sample_interval_us"] - 1) //
            record["sample_interval_us"])
        record["schema_version"] = 2
    timestamp = TRACE_TIMESTAMP.search(line)
    if timestamp:
        seconds, fraction = timestamp.groups()
        record["timestamp_ns"] = (int(seconds) * 1_000_000_000 +
                                  int(fraction.ljust(9, "0")))
    return record
