from __future__ import annotations

import json
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

from .models import DamonEvent, RegionObservation, VMARecord
from .region_vocab import RegionVocab
from .stable_region_key import stable_region_for_address
from .vma_interval_index import VMAIntervalIndex


class WindowAggregator:
    def __init__(self, *, output_dir: Path, vocab: RegionVocab, bucket_bytes: int, window_ms: int) -> None:
        self.output_dir = output_dir
        self.vocab = vocab
        self.bucket_bytes = bucket_bytes
        self.window_ns = int(window_ms * 1_000_000)
        self.current_start_ns: int | None = None
        self.rows: dict[tuple[int, str], dict[str, Any]] = {}
        self.window_file = (output_dir / "region_windows.jsonl").open("a", encoding="utf-8")
        self.event_file = (output_dir / "region_events.jsonl").open("a", encoding="utf-8")
        self.total_events = 0
        self.mapped_events = 0
        self.unmapped_events = 0
        self.low_resolution_events = 0
        self.flushed_windows = 0

    def close(self) -> None:
        self.flush_all()
        self.window_file.close()
        self.event_file.close()

    def add_event(
        self,
        *,
        app_id: str,
        foreground_epoch_id: str,
        event: DamonEvent,
        pid: int,
        process_starttime: int,
        process_role: str,
        index: VMAIntervalIndex,
        cgroup_features: dict[str, Any] | None = None,
    ) -> None:
        self.total_events += 1
        observations = map_event_to_regions(
            app_id=app_id,
            event=event,
            pid=pid,
            process_starttime=process_starttime,
            process_role=process_role,
            index=index,
            vocab=self.vocab,
            bucket_bytes=self.bucket_bytes,
        )
        if observations:
            self.mapped_events += 1
        else:
            self.unmapped_events += 1
        if any(obs.low_resolution for obs in observations):
            self.low_resolution_events += 1
        event_row = {
            "timestamp_ns": event.timestamp_ns,
            "app_id": app_id,
            "foreground_epoch_id": foreground_epoch_id,
            "pid": pid,
            "target_id": event.target_id,
            "start": event.start,
            "end": event.end,
            "nr_accesses": event.nr_accesses,
            "age": event.age,
            "mapped_region_count": len(observations),
            "raw_line": event.raw_line,
        }
        self.event_file.write(json.dumps(event_row, ensure_ascii=False) + "\n")
        self.event_file.flush()
        window_start = event.timestamp_ns - (event.timestamp_ns % self.window_ns)
        key = (window_start, app_id)
        row = self.rows.setdefault(
            key,
            {
                "window_start_ns": window_start,
                "window_end_ns": window_start + self.window_ns,
                "app_id": app_id,
                "foreground_epoch_id": foreground_epoch_id,
                "pid_count": 0,
                "pid_roles": [],
                "damon_event_count": 0,
                "mapped_event_count": 0,
                "unmapped_event_count": 0,
                "low_resolution_event_count": 0,
                "cgroup_features": cgroup_features or {},
                "region_sparse_vector": {},
                "collection_quality": "OK",
            },
        )
        row["damon_event_count"] += 1
        row["mapped_event_count"] += 1 if observations else 0
        row["unmapped_event_count"] += 0 if observations else 1
        row["low_resolution_event_count"] += 1 if any(obs.low_resolution for obs in observations) else 0
        roles = set(row.get("pid_roles", []))
        roles.add(process_role)
        row["pid_roles"] = sorted(roles)
        row["pid_count"] = max(int(row.get("pid_count", 0)), 1)
        for obs in observations:
            rid = str(obs.region_id)
            cell = row["region_sparse_vector"].setdefault(
                rid,
                {
                    "region_id": obs.region_id,
                    "region_type": obs.region_type,
                    "weighted_accesses": 0.0,
                    "access_rate": 0.0,
                    "age": obs.age,
                    "observed_bytes": 0,
                    "resolution_confidence": obs.resolution_confidence,
                    "identity_confidence": obs.identity_confidence,
                    "process_role": obs.process_role,
                    "canonical_region_id": obs.canonical_region_id,
                    "observer_process_count": 0,
                    "_observer_pids": set(),
                },
            )
            cell["weighted_accesses"] += obs.weighted_accesses
            cell["observed_bytes"] += obs.observed_bytes
            cell["resolution_confidence"] = min(cell["resolution_confidence"], obs.resolution_confidence)
            cell["_observer_pids"].add(pid)

    def flush_expired(self, now_ns: int | None = None) -> None:
        now_ns = now_ns or time.time_ns()
        for key in sorted(list(self.rows)):
            if key[0] + self.window_ns <= now_ns:
                self._flush_key(key)

    def flush_all(self) -> None:
        for key in sorted(list(self.rows)):
            self._flush_key(key)

    def _flush_key(self, key: tuple[int, str]) -> None:
        row = self.rows.pop(key)
        duration_s = max(0.001, (row["window_end_ns"] - row["window_start_ns"]) / 1_000_000_000)
        for cell in row["region_sparse_vector"].values():
            cell["observer_process_count"] = len(cell.pop("_observer_pids", set()))
            cell["access_rate"] = cell["weighted_accesses"] / duration_s
        self.window_file.write(json.dumps(row, ensure_ascii=False) + "\n")
        self.window_file.flush()
        self.flushed_windows += 1


def map_event_to_regions(
    *,
    app_id: str,
    event: DamonEvent,
    pid: int,
    process_starttime: int,
    process_role: str,
    index: VMAIntervalIndex,
    vocab: RegionVocab,
    bucket_bytes: int,
) -> list[RegionObservation]:
    if event.size_bytes <= 0:
        return []
    observations: list[RegionObservation] = []
    overlaps = index.overlaps(event.start, event.end)
    if not overlaps:
        return []
    event_bucket_count = max(1, (event.size_bytes + bucket_bytes - 1) // bucket_bytes)
    low_resolution = event_bucket_count > 16 or len(overlaps) > 8
    for vma, overlap_start, overlap_end in overlaps:
        cursor = overlap_start
        while cursor < overlap_end:
            bucket_end = min(overlap_end, ((cursor // bucket_bytes) + 1) * bucket_bytes)
            observed_bytes = max(0, bucket_end - cursor)
            if observed_bytes <= 0:
                break
            stable = stable_region_for_address(app_id, vma, cursor, bucket_bytes)
            region_id, canonical_region_id = vocab.get_or_create(
                stable_key=stable.stable_key,
                canonical_key=stable.canonical_key,
                region_type=stable.region_type,
                app_id=app_id,
                process_role=stable.process_role,
                path_metadata=stable.path_metadata,
                identity_confidence=stable.identity_confidence,
                now_ns=event.timestamp_ns,
            )
            contribution = observed_bytes / event.size_bytes
            observations.append(
                RegionObservation(
                    region_id=region_id,
                    region_type=stable.region_type,
                    stable_key=stable.stable_key,
                    canonical_key=stable.canonical_key,
                    weighted_accesses=event.nr_accesses * contribution,
                    age=event.age,
                    observed_bytes=observed_bytes,
                    resolution_confidence=min(1.0, bucket_bytes / event.size_bytes),
                    identity_confidence=stable.identity_confidence,
                    process_role=stable.process_role,
                    canonical_region_id=canonical_region_id,
                    low_resolution=low_resolution,
                )
            )
            cursor = bucket_end
    return observations

