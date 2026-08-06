#!/usr/bin/env python3
"""只读快速检查器，兼容 event_type/write_type/type 三种字段名。"""

from __future__ import annotations

import argparse
from pathlib import Path

from audit_common import PROJECT_ROOT, event_type, read_csv, resolve_input_path, status_ok


def inspect_rows(root: Path, app: str = "", limit: int = 20, only_state_changes: bool = False,
                 only_resolved: bool = False) -> tuple[str, bool]:
    workload = read_csv(root / "workload/workload_classifier_results_1s.csv")
    updates = read_csv(root / "markov/workload_markov_online_updates.csv")
    predictions = read_csv(root / "markov/workload_markov_online_predictions.csv")
    writes_path = root / "markov/workload_markov_online_debugfs_writes.csv"
    writes_fields = []
    if writes_path.exists():
        import csv
        with writes_path.open(encoding="utf-8", newline="") as stream:
            writes_fields = list(csv.DictReader(stream).fieldnames or [])
    writes = read_csv(writes_path)
    if app:
        match = lambda row: app in (row.get("app_key", ""), row.get("app_id", ""), row.get("runtime_app_id", ""))
        workload, updates, predictions = ([row for row in rows if match(row)] for rows in (workload, updates, predictions))
    if only_state_changes:
        workload = [row for row in workload if row.get("state_changed", "").lower() == "true"]
    if only_resolved:
        predictions = [row for row in predictions if row.get("resolution_status") == "RESOLVED"]
    lines = [
        "Runtime Monitor 中间输出快速检查", "",
        f"workload_rows={len(workload)}", f"markov_updates={len(updates)}",
        f"predictions={len(predictions)}", f"debugfs_writes={len(writes)}", "",
        "状态变化:",
    ]
    lines.extend(str({key: row.get(key, "") for key in ("timestamp_ns", "app_key", "observed_workload_id", "observed_workload_name", "classifier_rule")}) for row in workload[-limit:])
    lines += ["", "Markov updates:"]
    lines.extend(str({key: row.get(key, "") for key in ("timestamp_ns", "app_key", "prev_workload_id", "current_workload_id", "observed_next_workload_id", "debugfs_write_status")}) for row in updates[-limit:])
    lines += ["", "Resolved predictions:"]
    lines.extend(str({key: row.get(key, "") for key in ("prediction_id", "app_key", "prediction_time_ns", "actual_next_time_ns", "resolution_status", "causal_valid", "hit")}) for row in predictions[-limit:])
    normalized = {event_type(row) for row in writes}
    required = {"workload_update", "markov_set"}
    if writes and not (normalized & required):
        lines.append(f"COLUMN_NOT_FOUND: 实际字段={writes_fields}")
        result = "INCONCLUSIVE"
    elif not writes:
        lines.append("COLUMN_NOT_FOUND: 写入文件不存在或没有数据")
        result = "INCONCLUSIVE"
    else:
        counts = {name: sum(event_type(row) == name and status_ok(row) for row in writes) for name in ("workload_update", "markov_set", "app_bind", "app_probability", "app_current")}
        lines.append(f"Debugfs 写入统计: {counts}")
        result = "PASS"
    lines.append(f"result={result}")
    return "\n".join(lines) + "\n", result == "PASS"


def main() -> int:
    parser = argparse.ArgumentParser(description="查看 LSTM/workload/Markov 中间输出")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--session-dir")
    parser.add_argument("--share-dir")
    parser.add_argument("--app")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--only-state-changes", action="store_true")
    parser.add_argument("--only-resolved-predictions", action="store_true")
    args = parser.parse_args()
    root = resolve_input_path(args.output_dir)
    text, ok = inspect_rows(root, args.app or "", args.limit, args.only_state_changes, args.only_resolved_predictions)
    output = root / "reports/quick_inspection_fixed.txt"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(text, encoding="utf-8")
    print(output)
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
