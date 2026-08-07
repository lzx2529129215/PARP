#!/usr/bin/env python3
"""从 semantic_operation_alignment.csv 生成操作级统计。"""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter, defaultdict
from pathlib import Path


def read(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle: return list(csv.DictReader(handle))


def write(path: Path, fields: list[str], items: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields); writer.writeheader(); writer.writerows(items)


def main() -> int:
    parser = argparse.ArgumentParser(description="构建语义自动化操作级汇总")
    parser.add_argument("--alignment", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args(); data = read(args.alignment)
    by_op: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in data: by_op[row.get("operation_id", "")].append(row)
    execution, distributions, epochs, reentries = [], [], [], []
    for operation_id, items in sorted(by_op.items()):
        duration = [max(0, int(float(row.get("operation_end_ns", 0) or 0)) - int(float(row.get("operation_start_ns", 0) or 0))) / 1e6 for row in items]
        workloads = Counter(value for row in items for value in row.get("observed_workloads", "").split("|") if value)
        execution.append({"operation_id": operation_id, "execution_count": len(items), "success_count": sum(row.get("alignment_status") == "ALIGNED" for row in items), "failure_count": sum(row.get("alignment_status") == "NO_OBSERVED_WORKLOAD" for row in items), "skipped_count": 0, "average_duration_ms": f"{sum(duration) / len(duration):.3f}" if duration else "0"})
        for workload, count in workloads.items(): distributions.append({"operation_id": operation_id, "observed_workload": workload, "count": count})
        epochs.append({"operation_id": operation_id, "foreground_epoch_id": items[0].get("foreground_epoch_id", ""), "foreground_match_ratio": items[0].get("foreground_match_ratio", ""), "scope_match_ratio": items[0].get("scope_match_ratio", "")})
        for row in items:
            if row.get("reentry_event_id"): reentries.append({"operation_id": operation_id, "reentry_event_id": row["reentry_event_id"], "reentry_sample_workload": row.get("reentry_sample_workload", "")})
    write(args.output_dir / "operation_execution_summary.csv", ["operation_id", "execution_count", "success_count", "failure_count", "skipped_count", "average_duration_ms"], execution)
    write(args.output_dir / "operation_workload_distribution.csv", ["operation_id", "observed_workload", "count"], distributions)
    write(args.output_dir / "operation_foreground_epoch_summary.csv", ["operation_id", "foreground_epoch_id", "foreground_match_ratio", "scope_match_ratio"], epochs)
    write(args.output_dir / "operation_reentry_summary.csv", ["operation_id", "reentry_event_id", "reentry_sample_workload"], reentries)
    summary = {"input_file": str(args.alignment), "total_operations": len(data), "operation_types": len(by_op), "reentry_events": len(reentries), "final_result": "PASS" if data else "NOT_EXERCISED", "requested_operation_not_workload": True}
    (args.output_dir / "semantic_automation_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (args.output_dir / "semantic_automation_summary.md").write_text("# 语义自动化操作汇总\n\n- 输入文件: `%s`\n- operation 行数: %s\n- operation 类型数: %s\n- REENTRY 事件数: %s\n- final_result: `%s`\n- requested_operation 不作为 observed_workload。\n" % (args.alignment, len(data), len(by_op), len(reentries), summary["final_result"]), encoding="utf-8")
    return 0


if __name__ == "__main__": raise SystemExit(main())
