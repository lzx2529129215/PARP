from __future__ import annotations

import csv
import math
import statistics
from pathlib import Path
from typing import Any


def analyze_frame_times(
    path: str | Path,
    budget_ms: float = 16.7,
    window_start_ns: int | None = None,
    window_end_ns: int | None = None,
) -> dict[str, Any]:
    rows: list[tuple[int, float]] = []
    with Path(path).open(newline="", encoding="utf-8") as stream:
        reader = csv.DictReader(stream)
        for row in reader:
            rows.append((int(row["timestamp_ns"]), float(row["duration_ms"])))
    if not rows:
        raise ValueError("frame-time CSV contains no frames")

    rows.sort(key=lambda row: row[0])
    start_ns = window_start_ns if window_start_ns is not None else rows[0][0]
    end_ns = window_end_ns if window_end_ns is not None else max(ts + int(duration * 1e6) for ts, duration in rows)
    if end_ns <= start_ns:
        raise ValueError("frame observation window must have positive duration")
    if any(timestamp < start_ns or timestamp >= end_ns for timestamp, _ in rows):
        raise ValueError("frame timestamp lies outside the observation window")
    elapsed_s = max((end_ns - start_ns) / 1e9, 1e-9)
    bucket_counts: dict[int, int] = {}
    for timestamp_ns, _ in rows:
        bucket = math.floor((timestamp_ns - start_ns) / 1e9)
        bucket_counts[bucket] = bucket_counts.get(bucket, 0) + 1
    bucket_total = max(1, math.ceil(elapsed_s))
    fps_samples = [bucket_counts.get(bucket, 0) for bucket in range(bucket_total)]
    janks = sum(duration > budget_ms for _, duration in rows)
    return {
        "frames": len(rows),
        "elapsed_s": elapsed_s,
        "average_fps": len(rows) / elapsed_s,
        "fps_per_second_mean": statistics.fmean(fps_samples),
        "fps_per_second_stddev": statistics.pstdev(fps_samples),
        "fps_bucket_counts": fps_samples,
        "zero_frame_seconds": sum(count == 0 for count in fps_samples),
        "jank_budget_ms": budget_ms,
        "jank_count": janks,
        "jank_ratio": janks / len(rows),
        "frame_time_ms_p50": statistics.median(duration for _, duration in rows),
        "frame_time_ms_max": max(duration for _, duration in rows),
    }
