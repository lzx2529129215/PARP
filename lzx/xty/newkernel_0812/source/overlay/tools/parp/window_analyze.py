#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0
"""Timestamp-based 10/30/60 second PARP evidence aggregation."""

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, Hashable, Iterable, List, Tuple

WINDOWS = (10, 30, 60)
OUT_OF_ORDER_TOLERANCE_NS = 2_000_000_000


@dataclass
class WindowState:
    samples: List[Dict] = field(default_factory=list)
    seen_ids: set = field(default_factory=set)
    newest_ns: int = 0
    duplicate_count: int = 0
    out_of_order_count: int = 0

    def add(self, sample: Dict) -> bool:
        sample_id = sample["sample_id"]
        timestamp = sample["timestamp_ns"]
        if sample_id in self.seen_ids:
            self.duplicate_count += 1
            return False
        if self.newest_ns and timestamp + OUT_OF_ORDER_TOLERANCE_NS < self.newest_ns:
            self.out_of_order_count += 1
            return False
        self.seen_ids.add(sample_id)
        self.newest_ns = max(self.newest_ns, timestamp)
        self.samples.append(sample)
        self.samples = [item for item in self.samples
                        if item["timestamp_ns"] + 60_000_000_000 >= self.newest_ns]
        return True

    def summarize(self, now_ns: int = None) -> Dict:
        now_ns = self.newest_ns if now_ns is None else now_ns
        output = {"first_seen_ns": min((item["timestamp_ns"] for item in self.samples),
                                       default=0),
                  "last_seen_ns": max((item["timestamp_ns"] for item in self.samples),
                                      default=0),
                  "last_access_ns": max((item["timestamp_ns"] for item in self.samples
                                         if item.get("nr_accesses", 0)), default=0),
                  "duplicate_count": self.duplicate_count,
                  "out_of_order_count": self.out_of_order_count}
        for window in WINDOWS:
            selected = [item for item in self.samples
                        if now_ns - window * 1_000_000_000 < item["timestamp_ns"] <= now_ns]
            output[f"access_evidence_{window}s"] = sum(
                item.get("nr_accesses", 0) for item in selected)
            output[f"active_intervals_{window}s"] = sum(
                bool(item.get("nr_accesses", 0)) for item in selected)
        return output


def region_key(record: Dict) -> Tuple[Hashable, ...]:
    if record.get("region_type") == "FILE":
        return ("FILE", record.get("domain_id"), record.get("dev_major"),
                record.get("dev_minor"), record.get("inode"),
                record.get("file_version"), record.get("start_index"),
                record.get("nr_pages"))
    if record.get("region_type") == "ANON":
        return ("ANON", record.get("domain_id"),
                record.get("foreground_epoch_id"), record.get("mm_cookie"),
                record.get("vma_signature"), record.get("relative_start_pages"),
                record.get("nr_pages"))
    return (record.get("region_type", "UNRESOLVED"), record.get("sample_id"))


def aggregate(records: Iterable[Dict]) -> Tuple[Dict[Tuple, Dict], Dict]:
    states: Dict[Tuple, WindowState] = defaultdict(WindowState)
    for record in sorted(records, key=lambda item: item["timestamp_ns"]):
        states[region_key(record)].add(record)
    summaries = {key: state.summarize() for key, state in states.items()}
    quality = {
        "regions": len(states),
        "duplicates": sum(state.duplicate_count for state in states.values()),
        "out_of_order": sum(state.out_of_order_count for state in states.values()),
    }
    return summaries, quality


def domain_windows(records: Iterable[Dict], window: int) -> List[Dict]:
    grouped: Dict[int, Dict] = defaultdict(lambda: {
        "file_hot_regions": 0, "anon_hot_regions": 0,
        "file_active_pages": 0, "anon_active_pages": 0,
        "file_new_regions": 0, "file_reused_regions": 0,
        "anon_new_regions": 0, "anon_disappeared_regions": 0,
        "file_region_overlap": 0, "anon_activity_decay": 0.0,
        "unresolved_bytes": 0, "alignment_failure_count": 0})
    latest = max((item["timestamp_ns"] for item in records), default=0)
    active = [item for item in records
              if latest - window * 1_000_000_000 < item["timestamp_ns"] <= latest]
    seen = set()
    all_counts = defaultdict(int)
    for item in records:
        all_counts[region_key(item)] += 1
    for item in active:
        domain = item.get("domain_id", 0)
        row = grouped[domain]
        if item.get("region_type") == "UNRESOLVED":
            row["unresolved_bytes"] += item.get("region_end", 0) - item.get("region_start", 0)
            row["alignment_failure_count"] += 1
            continue
        key = region_key(item)
        if item.get("nr_accesses", 0) and key not in seen:
            seen.add(key)
            prefix = "file" if item["region_type"] == "FILE" else "anon"
            row[f"{prefix}_hot_regions"] += 1
            row[f"{prefix}_active_pages"] += item.get("nr_pages", 0)
            if all_counts[key] > 1 and prefix == "file":
                row["file_reused_regions"] += 1
            else:
                row[f"{prefix}_new_regions"] += 1
    for domain, row in grouped.items():
        if window < 60:
            active_anon = {region_key(item) for item in active
                           if item.get("domain_id") == domain and
                           item.get("region_type") == "ANON"}
            all_anon = {region_key(item) for item in records
                        if item.get("domain_id") == domain and
                        item.get("region_type") == "ANON"}
            row["anon_disappeared_regions"] = len(all_anon - active_anon)
        row["anon_activity_decay"] = 1.0 - ratio_or_zero(
            row["anon_active_pages"],
            sum(item.get("nr_pages", 0) for item in records
                if item.get("domain_id") == domain and
                item.get("region_type") == "ANON"))
    return [{"domain_id": domain, "window_seconds": window, **values}
            for domain, values in sorted(grouped.items())]


def ratio_or_zero(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0
