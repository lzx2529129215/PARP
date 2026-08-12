#!/usr/bin/env python3
"""Build reproducible multi-resolution file/ANON/operation datasets."""

import argparse
from collections import defaultdict
import csv
import hashlib
import json
from pathlib import Path
import time
from typing import Any, Dict, Iterable, List

from .anon_features import summarize_anon
from .config import PAGE_SIZE, PURGE_GAP_NS, RESOLUTIONS, WINDOW_NS
from .file_identity import FileIdentity, PartitionTracker
from .future_labels import attach_future_labels
from .operation_alignment import OperationEvent, align_operations
from .segmenter import all_segments
from .splitter import chronological_split
from .weighted_coverage import IntervalEvidence, coverage_summary
from .window_builder import window_bounds


FILE_SEGMENT_FIELDS = [
    "window_id", "file_id", "requested_bins", "effective_bins",
    "segment_id", "segment_start_page", "segment_end_page_exclusive",
    "segment_page_count", "observed_unique_pages", "accessed_unique_pages",
    "coverage_ratio", "weighted_access_pages", "weighted_coverage_ratio",
    "access_intensity", "active_state", "active_50", "active_80",
    "weighted_active_50", "weighted_active_80", "last_seen_ns", "mean_age",
    "max_age", "sample_count", "region_count", "observation_state",
    "window_start_ns", "window_end_ns", "session_id", "app_id", "domain_id",
    "partition_generation", "label_available_10s", "label_next_10s_access",
    "label_next_10s_max_coverage", "label_next_10s_weighted_coverage",
    "future_active_50_10s", "future_active_80_10s", "label_available_30s",
    "label_next_30s_access", "label_next_30s_max_coverage",
    "label_next_30s_weighted_coverage", "future_active_50_30s",
    "future_active_80_30s", "label_available_60s", "label_next_60s_access",
    "label_next_60s_max_coverage", "label_next_60s_weighted_coverage",
    "future_active_50_60s", "future_active_80_60s",
]


def load_jsonl(path: Path):
    if not path or not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()
            if line.strip()]


def write_csv(path: Path, rows: List[Dict[str, Any]], fields=None):
    fields = fields or sorted({key for row in rows for key in row})
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields,
                                extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def active_state(coverage):
    if coverage == 0:
        return "NONE"
    if coverage < .2:
        return "SPARSE"
    if coverage < .5:
        return "MEDIUM"
    if coverage < .8:
        return "DENSE"
    return "VERY_DENSE"


def stable_id(*parts):
    return hashlib.sha256("|".join(str(part) for part in parts).encode()).hexdigest()[:24]


def event_time(row):
    return int(row.get("event_time_ns", row.get("sample_timestamp_ns",
                                                row.get("timestamp_ns", 0))))


def require_file_schema(rows):
    required = {
        "dev_major", "dev_minor", "inode", "file_version",
        "file_size_bytes", "file_page_count", "logical_start", "nr_pages",
        "domain_id", "app_id", "session_id", "boot_id", "nr_accesses",
        "sample_interval_us", "aggregation_interval_us",
    }
    missing = sorted({field for field in required
                      if any(field not in row for row in rows)})
    if missing:
        raise ValueError("mandatory file trace fields missing: " + ",".join(missing))


def operation_rows(path):
    events = []
    for row in load_jsonl(path):
        events.append(OperationEvent(
            str(row["operation_id"]), str(row["app_id"]), int(row["start_ns"]),
            int(row["end_ns"]), str(row["operation_type"]),
            str(row.get("source", "automation")),
            float(row.get("confidence", 1.0)), str(row["session_id"])))
    return events


class DatasetBuilder:
    def __init__(self, output: Path, run_id: str, source: str,
                 timezone: str = "Asia/Shanghai"):
        self.output = Path(output)
        self.run_id = run_id
        self.source = source
        self.timezone = timezone
        self.partition_tracker = PartitionTracker()

    def build(self, file_rows, anon_rows, operations):
        started = time.monotonic_ns()
        require_file_schema(file_rows)
        self.output.mkdir(parents=True, exist_ok=True)
        file_groups = defaultdict(list)
        window_identity = {}
        dictionary = {}
        for row in file_rows:
            timestamp = event_time(row)
            start, end = window_bounds(timestamp, WINDOW_NS)
            identity = FileIdentity(int(row["dev_major"]), int(row["dev_minor"]),
                                    int(row["inode"]), int(row["file_version"]))
            pages = int(row["file_page_count"])
            size = int(row["file_size_bytes"])
            generation = self.partition_tracker.observe(identity, pages, size)
            file_id = stable_id(identity.stable_key)
            session = str(row["session_id"])
            app = str(row["app_id"])
            domain = int(row["domain_id"])
            window_id = stable_id(row["boot_id"], session, app, start)
            key = (window_id, file_id, identity, generation, pages, size)
            max_access = max(1, (int(row["aggregation_interval_us"]) +
                                 int(row["sample_interval_us"]) - 1) //
                             int(row["sample_interval_us"]))
            interval = IntervalEvidence(
                int(row["logical_start"]),
                min(pages, int(row["logical_start"]) + int(row["nr_pages"])),
                min(1.0, int(row["nr_accesses"]) / max_access),
                int(row.get("age", 0)))
            file_groups[key].append((timestamp, interval, row))
            window_identity[window_id] = {
                "window_id": window_id, "run_id": self.run_id,
                "session_id": session, "app_id": app, "domain_id": domain,
                "window_start_ns": start, "window_end_ns": end,
                "foreground_epoch": int(row.get("foreground_epoch", 0)),
                "foreground_state": str(row.get("foreground_state", "UNKNOWN")),
                "rss_bytes": int(row.get("rss_bytes", 0)),
                "pss_bytes": int(row.get("pss_bytes", 0)),
                "referenced_bytes": int(row.get("referenced_bytes", 0)),
                "swap_bytes": int(row.get("swap_bytes", 0)),
            }
            item = dictionary.setdefault(file_id, {
                "file_id": file_id, "dev_major": identity.dev_major,
                "dev_minor": identity.dev_minor, "inode": identity.inode,
                "file_version": identity.file_version,
                "path_hash": row.get("path_hash", ""),
                "basename_optional": row.get("basename_optional", ""),
                "file_size_bytes": size, "file_page_count": pages,
                "partition_generation": generation,
                "first_seen_ns": timestamp, "last_seen_ns": timestamp,
                "observed_domains": set(), "shared_mapping_seen": False,
                "file_class": row.get("file_class", "UNKNOWN")})
            item["first_seen_ns"] = min(item["first_seen_ns"], timestamp)
            item["last_seen_ns"] = max(item["last_seen_ns"], timestamp)
            item["observed_domains"].add(domain)

        dense = {10: [], 100: []}
        sparse1000 = []
        active_counts = defaultdict(lambda: {10: 0, 100: 0, 1000: 0})
        files_by_window = defaultdict(set)
        for (window_id, file_id, identity, generation, pages, _), samples in sorted(
                file_groups.items(), key=lambda item: (item[0][0], item[0][1])):
            intervals = [item[1] for item in samples]
            timestamps = [item[0] for item in samples]
            base_window = window_identity[window_id]
            files_by_window[window_id].add(file_id)
            for resolution in RESOLUTIONS:
                segment_rows = []
                for spec in all_segments(pages, resolution, generation):
                    summary = coverage_summary(spec.start_page,
                                               spec.end_page_exclusive,
                                               intervals)
                    observation = ("NOT_OBSERVED" if not summary.observed_unique_pages
                                   else ("OBSERVED_ACTIVE" if summary.accessed_unique_pages
                                         else "OBSERVED_INACTIVE"))
                    row = {
                        "window_id": window_id, "file_id": file_id,
                        "requested_bins": resolution,
                        "effective_bins": spec.effective_bins,
                        "segment_id": spec.segment_id,
                        "segment_start_page": spec.start_page,
                        "segment_end_page_exclusive": spec.end_page_exclusive,
                        "segment_page_count": summary.segment_page_count,
                        "observed_unique_pages": summary.observed_unique_pages,
                        "accessed_unique_pages": summary.accessed_unique_pages,
                        "coverage_ratio": summary.coverage_ratio,
                        "weighted_access_pages": summary.weighted_access_pages,
                        "weighted_coverage_ratio": summary.weighted_coverage_ratio,
                        "access_intensity": summary.access_intensity,
                        "active_state": active_state(summary.coverage_ratio),
                        "active_50": summary.coverage_ratio >= .5,
                        "active_80": summary.coverage_ratio >= .8,
                        "weighted_active_50": summary.weighted_coverage_ratio >= .5,
                        "weighted_active_80": summary.weighted_coverage_ratio >= .8,
                        "last_seen_ns": max(timestamps),
                        "mean_age": summary.mean_age, "max_age": summary.max_age,
                        "sample_count": len({item[2].get("sample_id") for item in samples}),
                        "region_count": summary.region_count,
                        "observation_state": observation,
                        "window_start_ns": base_window["window_start_ns"],
                        "window_end_ns": base_window["window_end_ns"],
                        "session_id": base_window["session_id"],
                        "app_id": base_window["app_id"],
                        "domain_id": base_window["domain_id"],
                        "partition_generation": generation,
                    }
                    segment_rows.append(row)
                    if observation == "OBSERVED_ACTIVE":
                        active_counts[window_id][resolution] += 1
                if resolution in dense:
                    dense[resolution].extend(segment_rows)
                else:
                    active = [row for row in segment_rows
                              if row["coverage_ratio"] > 0 or
                              row["weighted_coverage_ratio"] > 0]
                    sparse1000.append({
                        "window_id": window_id, "file_id": file_id,
                        "requested_bins": 1000,
                        "effective_bins": segment_rows[0]["effective_bins"],
                        "active_segment_ids": [row["segment_id"] for row in active],
                        "coverage_values": [row["coverage_ratio"] for row in active],
                        "weighted_coverage_values": [row["weighted_coverage_ratio"] for row in active],
                        "negative_candidate_count": len(segment_rows) - len(active),
                        "observation_semantics": "missing sparse entry is NOT_OBSERVED_OR_INACTIVE",
                        "session_id": base_window["session_id"],
                        "window_start_ns": base_window["window_start_ns"],
                    })

        dense[10] = attach_future_labels(dense[10])
        dense[100] = attach_future_labels(dense[100])
        anon_dataset = self._build_anon(anon_rows, window_identity)
        anon_by_window = {row["window_id"]: row for row in anon_dataset}
        operations_by_session = defaultdict(list)
        for event in operations:
            operations_by_session[event.session_id].append(event)
        alignment_rows = []
        window_rows = []
        ordered_windows = sorted(window_identity.values(),
                                 key=lambda row: (row["session_id"], row["window_start_ns"]))
        for index, row in enumerate(ordered_windows):
            session_events = operations_by_session[row["session_id"]]
            next_op = "UNKNOWN"
            later = sorted((event for event in session_events
                            if event.start_ns >= row["window_end_ns"]),
                           key=lambda event: event.start_ns)
            if later:
                next_op = later[0].operation_type
            aligned = align_operations(row["window_start_ns"],
                                       row["window_end_ns"], session_events,
                                       next_op)
            alignment = {"window_id": row["window_id"], **aligned.__dict__}
            alignment_rows.append(alignment)
            anon = anon_by_window.get(row["window_id"], {})
            file_ids = files_by_window[row["window_id"]]
            window_rows.append({
                **row, "current_operation": aligned.operation_at_start,
                "dominant_operation": aligned.dominant_operation,
                "next_operation": aligned.next_operation,
                "operation_coverage_ratio": aligned.operation_coverage_ratio,
                "label_quality": aligned.label_quality,
                "file_count": len(file_ids),
                "active_file_count": sum(any(
                    item["file_id"] == file_id and item["window_id"] == row["window_id"] and
                    item["coverage_ratio"] > 0 for item in dense[10])
                    for file_id in file_ids),
                "active_segment_count_l10": active_counts[row["window_id"]][10],
                "active_segment_count_l100": active_counts[row["window_id"]][100],
                "active_segment_count_l1000": active_counts[row["window_id"]][1000],
                "anon_total_bytes": anon.get("anon_total_bytes", 0),
                "anon_hot_bytes": anon.get("anon_hot_bytes", 0),
                "anon_cold_bytes": anon.get("anon_cold_bytes", 0),
            })

        dictionary_rows = []
        for item in dictionary.values():
            domains = item.pop("observed_domains")
            item["observed_domain_count"] = len(domains)
            item["shared_mapping_seen"] = len(domains) > 1
            dictionary_rows.append(item)
        write_csv(self.output / "windows_10s.csv", window_rows)
        write_csv(self.output / "file_dictionary.csv", dictionary_rows)
        write_csv(self.output / "file_segments_l10.csv", dense[10], FILE_SEGMENT_FIELDS)
        write_csv(self.output / "file_segments_l100.csv", dense[100], FILE_SEGMENT_FIELDS)
        with (self.output / "file_segments_l1000.jsonl").open("w") as stream:
            for row in sparse1000:
                stream.write(json.dumps(row, sort_keys=True) + "\n")
        write_csv(self.output / "anon_windows_10s.csv", anon_dataset)
        write_csv(self.output / "window_operation_alignment.csv", alignment_rows)
        with (self.output / "operation_events.jsonl").open("w") as stream:
            for event in operations:
                stream.write(json.dumps(event.__dict__, sort_keys=True) + "\n")

        (self.output / "splits").mkdir(exist_ok=True)
        split_audit = {"status": "GATED", "reason": "fewer than three sessions"}
        if len({row["session_id"] for row in window_rows}) >= 3:
            split = chronological_split(window_rows, PURGE_GAP_NS)
            for name in ("train", "val", "test"):
                sessions = sorted({row["session_id"] for row in split[name]})
                (self.output / "splits" / f"{name}_sessions.txt").write_text(
                    "\n".join(sessions) + "\n")
            split_audit = split["audit"]
            split_audit["status"] = "PASS"
        (self.output / "splits/split_audit.json").write_text(
            json.dumps(split_audit, indent=2, sort_keys=True) + "\n")
        metadata = {
            "schema_version": 1, "source": self.source,
            "run_id": self.run_id, "window_ns": WINDOW_NS,
            "timezone": self.timezone, "file_regions": len(file_rows),
            "anon_regions": len(anon_rows), "windows": len(window_rows),
            "files": len(dictionary_rows), "level10_entries": len(dense[10]),
            "level100_entries": len(dense[100]),
            "level1000_sparse_entries": sum(len(row["active_segment_ids"])
                                             for row in sparse1000),
            "builder_elapsed_ns": time.monotonic_ns() - started,
            "future_features_used": False,
            "absolute_paths_in_public_tables": False,
        }
        (self.output / "dataset_metadata.json").write_text(
            json.dumps(metadata, indent=2, sort_keys=True) + "\n")
        schema = self.schema()
        (self.output / "schema.json").write_text(
            json.dumps(schema, indent=2, sort_keys=True) + "\n")
        return metadata

    def _build_anon(self, rows, window_identity):
        groups = defaultdict(list)
        for row in rows:
            timestamp = event_time(row)
            start, _ = window_bounds(timestamp)
            candidates = [window for window in window_identity.values()
                          if window["session_id"] == str(row["session_id"]) and
                          window["app_id"] == str(row["app_id"]) and
                          window["window_start_ns"] == start]
            if not candidates:
                continue
            window_id = candidates[0]["window_id"]
            key = (window_id, row["domain_id"], row.get("foreground_epoch", 0),
                   row["mm_cookie"], row["boot_id"])
            groups[key].append(row)
        by_window = defaultdict(list)
        for (window_id, domain, epoch, mm_cookie, boot_id), items in groups.items():
            max_access = max(1, max((int(item.get("aggregation_interval_us", 1000000)) +
                                     int(item.get("sample_interval_us", 5000)) - 1) //
                                    int(item.get("sample_interval_us", 5000))
                                    for item in items))
            normalized = [{**item, "foreground_epoch": epoch,
                           "mm_cookie": mm_cookie, "boot_id": boot_id}
                          for item in items]
            by_window[window_id].append(summarize_anon(normalized, max_access))
        output = []
        for window_id, summaries in by_window.items():
            total = sum(row["anon_total_bytes"] for row in summaries)
            combined = {"window_id": window_id}
            sum_fields = ("anon_total_bytes", "anon_observed_bytes", "anon_hot_bytes",
                          "anon_warm_bytes", "anon_cold_bytes",
                          "anon_active_region_count", "anon_region_count",
                          "unresolved_anon_bytes")
            for field in sum_fields:
                combined[field] = sum(row[field] for row in summaries)
            for field in ("anon_mean_access_ratio", "anon_mean_age"):
                combined[field] = sum(row[field] * row["anon_total_bytes"]
                                      for row in summaries) / total if total else 0.0
            combined["anon_max_access_ratio"] = max(row["anon_max_access_ratio"] for row in summaries)
            combined["anon_max_age"] = max(row["anon_max_age"] for row in summaries)
            combined["anon_working_set_delta"] = sum(row["anon_working_set_delta"] for row in summaries)
            combined["anon_recently_active_ratio"] = (
                (combined["anon_hot_bytes"] + combined["anon_warm_bytes"]) / total
                if total else 0.0)
            combined["anon_cooling_ratio"] = combined["anon_cold_bytes"] / total if total else 0.0
            combined["unresolved_anon_ratio"] = combined["unresolved_anon_bytes"] / (
                total + combined["unresolved_anon_bytes"]) if total + combined["unresolved_anon_bytes"] else 0.0
            output.append(combined)
        return output

    @staticmethod
    def schema():
        return {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "title": "PARP Phase2.7 dataset metadata",
            "type": "object",
            "required": ["schema_version", "source", "run_id", "window_ns",
                         "file_regions", "anon_regions", "windows"],
            "properties": {
                "schema_version": {"const": 1}, "source": {"type": "string"},
                "run_id": {"type": "string"}, "window_ns": {"const": WINDOW_NS},
                "file_regions": {"type": "integer", "minimum": 0},
                "anon_regions": {"type": "integer", "minimum": 0},
                "windows": {"type": "integer", "minimum": 0},
            },
        }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--file-regions", type=Path, required=True)
    parser.add_argument("--anon-regions", type=Path, required=True)
    parser.add_argument("--operation-events", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--source", required=True)
    args = parser.parse_args()
    metadata = DatasetBuilder(args.output, args.run_id, args.source).build(
        load_jsonl(args.file_regions), load_jsonl(args.anon_regions),
        operation_rows(args.operation_events))
    print(json.dumps(metadata, sort_keys=True))


if __name__ == "__main__":
    main()
