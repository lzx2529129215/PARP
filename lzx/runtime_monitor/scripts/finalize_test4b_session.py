#!/usr/bin/env python3
"""Produce auditable Test4B session evidence without inferring real-App benefit."""
from __future__ import annotations

import argparse
import csv
import json
import shutil
from collections import Counter
from pathlib import Path
from typing import Any


def rows(path: Path) -> list[dict[str, str]]:
    try:
        with path.open(encoding="utf-8", newline="") as stream: return list(csv.DictReader(stream))
    except OSError: return []


def main() -> None:
    parser = argparse.ArgumentParser(); parser.add_argument("--session-dir", type=Path, required=True); parser.add_argument("--base-coverage", type=Path, required=True); args = parser.parse_args()
    session = args.session_dir.resolve(); review = session / "review"; review.mkdir(exist_ok=True)
    for name in ("events", "prediction", "analysis", "latency", "ballast", "reclaim"):
        (session / name).mkdir(exist_ok=True)
    for source, target in ((session / "model/direct_app_events.csv", session / "events/direct_app_events.csv"),
                           (session / "model/online_lstm_predictions.csv", session / "prediction/online_lstm_predictions.csv")):
        if source.exists(): shutil.copy2(source, target)
    if args.base_coverage.exists(): shutil.copy2(args.base_coverage, review / "validation_sequence_coverage.json")
    decisions = rows(session / "reclaim/test4b_reclaim_decisions.csv")
    attempts = rows(session / "reclaim/test4b_memory_reclaim_attempts.csv")
    evaluations = rows(session / "reclaim/test4b_candidate_evaluations.csv")
    allocations = rows(session / "ballast/ballast_allocation_audit.csv")
    reaccess = rows(session / "ballast/ballast_reaccess.csv")
    direct = rows(session / "model/direct_app_events.csv")
    writes = [row for row in attempts if row.get("write_success") == "true"]
    would = [row for row in decisions if row.get("decision") == "WOULD_RECLAIM"]
    unsafe = [row for row in decisions if row.get("decision") in {"RECLAIM", "WOULD_RECLAIM"} and row.get("candidate_background") != "true"]
    events_before = []
    for source in sorted((session / "ballast").glob("raw_events_*.csv")):
        events_before.extend(rows(source))
    fields = list(events_before[0]) if events_before else ["timestamp_ns", "app_key", "pid", "state_before", "state_after", "command", "anon_cold_bytes", "anon_hot_bytes", "file_cold_bytes", "file_hot_bytes", "operation_latency_us", "status", "error"]
    with (session / "ballast/ballast_events.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields); writer.writeheader(); writer.writerows(sorted(events_before, key=lambda item: item.get("timestamp_ns", "")))
    raw_counts = Counter(row.get("command", "") for row in events_before)
    background_cold_operations = sum(
        row.get("state_before") == "BACKGROUND_IDLE" and "COLD" in row.get("command", "")
        for row in events_before
    )
    correctness = {
        "foreground_only_allocation_success_count": sum(row.get("command") == "ALLOCATE" and row.get("status") == "OK" for row in events_before),
        "background_allocate_rejected_count": sum(row.get("command") == "ALLOCATE" and row.get("status") == "REJECTED" for row in events_before),
        "background_hot_tick_count": raw_counts.get("BACKGROUND_HOT_TICK", 0),
        "background_cold_operation_count": background_cold_operations,
        "allocation_audit_rejections": Counter(row.get("reject_reason", "") for row in allocations if row.get("status") == "REJECTED"),
        "reaccess_command_count": len(reaccess),
    }
    # A cold REACCESS/VERIFY after a foreground return is expected.  A raw
    # background-only command must never mention a cold allocation/prefetch.
    (session / "ballast/ballast_correctness.json").write_text(json.dumps(correctness, ensure_ascii=False, indent=2, default=dict) + "\n", encoding="utf-8")
    report_data = {
        "mode": next((row.get("mode") for row in decisions if row.get("mode")), "unknown"),
        "prediction_batches": sum(row.get("prediction_triggered") == "1" for row in direct),
        "would_reclaim": len(would), "successful_memory_reclaim_writes": len(writes),
        "foreground_candidate_violations": len(unsafe), "candidate_rejection_counts": Counter(row.get("rejection_reason", "") for row in evaluations if row.get("rejection_reason")),
        "allocation_success": correctness["foreground_only_allocation_success_count"],
        "background_allocate_rejected": correctness["background_allocate_rejected_count"],
        "reaccess_commands": len(reaccess),
    }
    (review / "test4b_session_summary.json").write_text(json.dumps(report_data, ensure_ascii=False, indent=2, default=dict) + "\n", encoding="utf-8")
    report = f"""# Test4B：前台构造混合工作集后的低概率 App 定向回收

模式：`{report_data['mode']}`。这是合成工作集机制验证；ballast 页面与真实 App 功能无关，不能直接外推为真实 App 收益。

- 事件驱动 v3 LSTM batch：{report_data['prediction_batches']}；ballast 进程不创建 X11 窗口，未作为预测触发源。
- 前台安全 ALLOCATE 成功：{report_data['allocation_success']}；后台 ALLOCATE 拒绝：{report_data['background_allocate_rejected']}。
- `WOULD_RECLAIM`：{len(would)}；真实 `memory.reclaim` 成功写入：{len(writes)}；前台候选违规：{len(unsafe)}。
- Ground truth 仅在 `BACKGROUND_IDLE` 冷区静默后用于合成验证；Observed Referenced/RSS 继续记录但不作为本实验 APPLY 的否决条件。
- Apply 标签：`SYNTHETIC_GROUND_TRUTH_APPLY`（若写入发生）。
"""
    (review / "FINAL_REPORT.md").write_text(report, encoding="utf-8")
    print(json.dumps(report_data, ensure_ascii=False, default=dict))


if __name__ == "__main__":
    main()
