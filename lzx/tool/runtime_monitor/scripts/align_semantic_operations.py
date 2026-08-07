#!/usr/bin/env python3
"""将语义 operation 请求与真实采集结果对齐，绝不由请求标签推导 workload。"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
from collections import Counter
from pathlib import Path
from typing import Any


FIELDS = ["session_id", "scenario_id", "phase_id", "operation_id", "app_key", "operation_start_ns", "operation_end_ns", "foreground_epoch_id", "foreground_match_ratio", "scope_match_ratio", "classifier_sample_count", "observed_workloads", "dominant_observed_workload", "continue_updates", "continue_predictions", "reentry_event_id", "reentry_sample_workload", "debugfs_write_success", "alignment_status", "notes"]


def rows(path: Path | None) -> list[dict[str, str]]:
    if path is None or not path.exists(): return []
    with path.open(encoding="utf-8", newline="") as handle: return list(csv.DictReader(handle))


def integer(value: str) -> int:
    try: return int(float(value))
    except (TypeError, ValueError): return 0


def timestamp_ns(row: dict[str, str]) -> int:
    for key in ("ts_ns", "timestamp_ns", "start_time_ns", "timestamp"):
        value = row.get(key, "")
        if not value: continue
        numeric = integer(value)
        if numeric > 1_000_000_000_000: return numeric
        if numeric > 0: return numeric * 1_000_000_000
        try: return int(dt.datetime.fromisoformat(value).timestamp() * 1_000_000_000)
        except ValueError: pass
    return 0


def operation_intervals(trace: list[dict[str, str]]) -> list[dict[str, str]]:
    open_rows: dict[tuple[str, str, str], list[dict[str, str]]] = {}
    out: list[dict[str, str]] = []
    for row in trace:
        if row.get("op_type") != "semantic_operation": continue
        key = (row.get("session_id", ""), row.get("phase_id", ""), row.get("operation_id", ""))
        if not key[2]: continue
        if row.get("event_type") == "OP_START": open_rows.setdefault(key, []).append(row)
        elif row.get("event_type") in {"OP_DONE", "OP_FAILED"} and open_rows.get(key):
            start = open_rows[key].pop(0)
            merged = dict(start); merged["operation_end_ns"] = str(timestamp_ns(row)); merged["terminal_status"] = row.get("status", "")
            out.append(merged)
    return out


def in_interval(row: dict[str, str], start: int, end: int) -> bool:
    value = timestamp_ns(row)
    return value >= start and (not end or value <= end)


def main() -> int:
    parser = argparse.ArgumentParser(description="语义 operation 与运行数据对齐")
    parser.add_argument("--trace", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--foreground", type=Path)
    parser.add_argument("--workload", type=Path)
    parser.add_argument("--continue", dest="continue_path", type=Path)
    parser.add_argument("--reentry", type=Path)
    parser.add_argument("--debugfs-writes", type=Path)
    args = parser.parse_args()
    trace = rows(args.trace); foreground = rows(args.foreground); workload = rows(args.workload)
    continues = rows(args.continue_path); reentries = rows(args.reentry); writes = rows(args.debugfs_writes)
    output_rows: list[dict[str, Any]] = []
    for operation in operation_intervals(trace):
        start, end = timestamp_ns(operation), integer(operation.get("operation_end_ns", ""))
        app_key = operation.get("app_key", "")
        samples = [row for row in workload if in_interval(row, start, end) and row.get("app_key", "") == app_key]
        observed = [row.get("workload_name") or row.get("workload_id", "") for row in samples]
        foreground_samples = [row for row in foreground if in_interval(row, start, end)]
        foreground_match = [row for row in foreground_samples if row.get("foreground_app", row.get("app_key", "")) == app_key]
        matching_scope = [row for row in samples if row.get("scope_name", "")]
        reentry_rows = [row for row in reentries if in_interval(row, start, end) and (not row.get("app_key") or row.get("app_key") == app_key)]
        related_writes = [row for row in writes if in_interval(row, start, end) and row.get("status", "") in {"ok", "success"}]
        output_rows.append({
            "session_id": operation.get("session_id", ""), "scenario_id": operation.get("scenario_id", ""), "phase_id": operation.get("phase_id", ""), "operation_id": operation.get("operation_id", ""), "app_key": app_key,
            "operation_start_ns": start, "operation_end_ns": end, "foreground_epoch_id": operation.get("foreground_epoch_id", ""),
            "foreground_match_ratio": f"{len(foreground_match) / len(foreground_samples):.3f}" if foreground_samples else "", "scope_match_ratio": f"{len(matching_scope) / len(samples):.3f}" if samples else "",
            "classifier_sample_count": len(samples), "observed_workloads": "|".join(sorted(set(filter(None, observed)))), "dominant_observed_workload": Counter(observed).most_common(1)[0][0] if observed else "",
            "continue_updates": sum(1 for row in continues if in_interval(row, start, end)), "continue_predictions": 0,
            "reentry_event_id": reentry_rows[0].get("event_id", "") if reentry_rows else "", "reentry_sample_workload": reentry_rows[0].get("workload_name", reentry_rows[0].get("workload_id", "")) if reentry_rows else "",
            "debugfs_write_success": len(related_writes), "alignment_status": "ALIGNED" if samples else "NO_OBSERVED_WORKLOAD", "notes": "requested_operation 仅用于定位区间；observed_workload 只来自 classifier 输出。",
        })
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS); writer.writeheader(); writer.writerows(output_rows)
    print(f"semantic_alignment={args.output} rows={len(output_rows)}")
    return 0


if __name__ == "__main__": raise SystemExit(main())
