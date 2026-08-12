"""Future 10/30/60-second labels with explicit availability."""

from collections import defaultdict
from typing import Any, Dict, Iterable, List

HORIZONS = (10, 30, 60)


def attach_future_labels(rows: Iterable[Dict[str, Any]]):
    groups = defaultdict(list)
    for row in rows:
        key = (row.get("session_id"), row.get("file_id"),
               row.get("requested_bins"), row.get("segment_id"))
        groups[key].append(dict(row))
    output = []
    for group in groups.values():
        group.sort(key=lambda row: row["window_start_ns"])
        last_start = group[-1]["window_start_ns"]
        by_start = {row["window_start_ns"]: row for row in group}
        for row in group:
            current = row["window_start_ns"]
            for horizon in HORIZONS:
                limit = current + horizon * 1_000_000_000
                future = [item for start, item in by_start.items()
                          if current < start <= limit]
                available = last_start >= limit
                row[f"label_available_{horizon}s"] = available
                if not available:
                    row[f"label_next_{horizon}s_access"] = None
                    row[f"label_next_{horizon}s_max_coverage"] = None
                    row[f"label_next_{horizon}s_weighted_coverage"] = None
                    row[f"future_active_50_{horizon}s"] = None
                    row[f"future_active_80_{horizon}s"] = None
                    continue
                max_coverage = max((item.get("coverage_ratio", 0.0)
                                    for item in future), default=0.0)
                max_weighted = max((item.get("weighted_coverage_ratio", 0.0)
                                    for item in future), default=0.0)
                row[f"label_next_{horizon}s_access"] = int(
                    max_coverage > 0 or max_weighted > 0)
                row[f"label_next_{horizon}s_max_coverage"] = max_coverage
                row[f"label_next_{horizon}s_weighted_coverage"] = max_weighted
                row[f"future_active_50_{horizon}s"] = max_coverage >= .5
                row[f"future_active_80_{horizon}s"] = max_coverage >= .8
            output.append(row)
    return sorted(output, key=lambda row: (row.get("window_start_ns", 0),
                                           row.get("window_id", "")))
