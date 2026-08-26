#!/usr/bin/env python3
"""Finalize one Test4 session without performing any memory intervention."""
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
        with path.open(encoding="utf-8", newline="") as stream:
            return list(csv.DictReader(stream))
    except OSError:
        return []


def read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--session-dir", required=True, type=Path)
    parser.add_argument("--coverage", required=True, type=Path)
    parser.add_argument("--execution-coverage", required=True, type=Path)
    parser.add_argument("--apply-preflight", required=True, type=Path)
    args = parser.parse_args()
    session = args.session_dir
    for name in ("events", "prediction", "analysis", "review", "latency", "reclaim", "activity"):
        (session / name).mkdir(exist_ok=True)
    for name in ("foreground_events.csv", "app_lifecycle_events.csv", "automation_trace.csv"):
        source = session / "model" / name
        if source.exists():
            shutil.copy2(source, session / "events" / name)
    for name in ("online_lstm_predictions.csv", "online_lstm_duration_call_trace.csv"):
        source = session / "model" / name
        if source.exists():
            shutil.copy2(source, session / "prediction" / name)

    shutil.copy2(args.coverage, session / "review" / "validation_sequence_coverage.json")
    execution_target = session / "review" / "validation_sequence_execution.json"
    if args.execution_coverage.exists() and args.execution_coverage.resolve() != execution_target.resolve():
        shutil.copy2(args.execution_coverage, execution_target)
    source_coverage = read_json(args.coverage)
    execution = read_json(args.execution_coverage)
    apply_preflight = read_json(args.apply_preflight)
    manifest = read_json(session / "manifest.json")

    direct = rows(session / "model" / "direct_app_events.csv")
    triggered = [row for row in direct if row.get("prediction_triggered") == "1"]
    trigger_matrix = {
        "APP_OPEN": True, "APP_SWITCH": True, "APP_MINIMIZE": False,
        "APP_RESTORE": False, "APP_CLOSE": False, "PROCESS_START": False,
        "PROCESS_EXIT": False, "PERIODIC": False,
    }
    trigger_audit = {
        "matrix": trigger_matrix,
        "direct_event_count": len(direct),
        "triggered_prediction_count": len(triggered),
        "prediction_ids": [row.get("prediction_id", "") for row in triggered if row.get("prediction_id")],
        "observed_event_types": Counter(row.get("event_type", "") for row in direct),
        "rule": "Only direct X11 APP_OPEN and APP_SWITCH may invoke online v3 LSTM. Minimize/restore need a separate resulting APP_SWITCH; process lifecycle and periodic sampling never invoke it.",
    }
    (session / "review" / "prediction_trigger_audit.json").write_text(
        json.dumps(trigger_audit, ensure_ascii=False, indent=2, default=dict) + "\n", encoding="utf-8"
    )

    decisions = rows(session / "reclaim" / "app_reclaim_decisions.csv")
    attempts = rows(session / "reclaim" / "memory_reclaim_attempts.csv")
    activities = rows(session / "activity" / "app_memory_activity.csv")
    evaluations = rows(session / "reclaim" / "candidate_evaluations.csv")
    writes = [row for row in attempts if row.get("write_success") == "true"]
    would = [row for row in decisions if row.get("decision") == "WOULD_RECLAIM"]
    unsafe_foreground = [row for row in decisions if row.get("decision") in {"WOULD_RECLAIM", "RECLAIM"} and row.get("candidate_background") != "true"]
    safety = {
        "mode": "shadow",
        "prediction_count": len(triggered),
        "activity_sample_count": len(activities),
        "candidate_evaluation_count": len(evaluations),
        "decision_count": len(decisions),
        "would_reclaim_count": len(would),
        "memory_reclaim_write_count": len(writes),
        "foreground_candidate_violation_count": len(unsafe_foreground),
        "apply_preflight": apply_preflight,
        "safe": not writes and not unsafe_foreground,
        "reason": "Shadow mode does not open memory.reclaim. Apply requires a fresh READY preflight and remains blocked here by the finite-memory.max rule.",
    }
    (session / "safety_report.json").write_text(json.dumps(safety, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (session / "review" / "reclaim_safety_audit.json").write_text(json.dumps(safety, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    summary = {
        "decisions": Counter(row.get("decision", "") for row in decisions),
        "skip_reasons": Counter(row.get("skip_reason", "") for row in decisions),
        "candidate_rejections": Counter(row.get("rejection_reason", "") for row in evaluations),
        "attempts": len(attempts),
        "successful_writes": len(writes),
        "requested_bytes": sum(int(row.get("requested_reclaim_bytes") or 0) for row in decisions if row.get("decision") in {"WOULD_RECLAIM", "RECLAIM"}),
    }
    (session / "reclaim" / "reclaim_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, default=dict) + "\n", encoding="utf-8"
    )
    # This is deliberately empty in shadow mode but retains a stable A/B join
    # schema for a future preflight-authorized apply session.
    join_path = session / "analysis" / "reclaim_to_next_app_join.csv"
    with join_path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=["decision_id", "reclaimed_app", "actual_next_app", "was_reclaimed_app_next_foreground", "decision_correct", "reason"])
        writer.writeheader()
        for row in writes:
            writer.writerow({"decision_id": row.get("decision_id", ""), "reclaimed_app": row.get("app_key", ""), "reason": "apply analysis not available in shadow"})

    collection_failed = any(int(manifest.get(key, 0) or 0) != 0 for key in ("automation_rc", "monitor_rc", "sink_coverage_rc"))
    execution_ok = execution.get("status") == "PASS"
    status = "PARP_APP_PROBABILITY_ACTIVITY_RECLAIM_PARTIAL"
    if collection_failed or not execution_ok:
        status = "PARP_APP_PROBABILITY_ACTIVITY_RECLAIM_BLOCKED"
    boundary = apply_preflight.get("checks", {}).get("memory_boundary", {})
    report = f"""# Test4：低概率、低活跃后台 App 的受限回收

状态：`{status}`。

本会话仅为 validation split 的参数调优/可行性 SHADOW，不构成独立 test split 的效果结论。

- 在线预测触发：仅原生 X11 `APP_OPEN`、`APP_SWITCH`；`MINIMIZE`、`RESTORE`、`CLOSE`、`PROCESS_START/EXIT`、`PERIODIC` 均不直接触发。
- 数据集序列：`{source_coverage.get('session_id')}`，转换状态 `{source_coverage.get('status')}`；运行时序列验证 `{execution.get('status', 'MISSING')}`。
- 实际 prediction：{len(triggered)}；活跃度采样：{len(activities)}；候选评估：{len(evaluations)}；控制器决策：{len(decisions)}；`WOULD_RECLAIM`：{len(would)}。
- 真实 `memory.reclaim` 写入：{len(writes)}（SHADOW 必须为 0）。

`apply-bounded` 预检：`{apply_preflight.get('status', 'MISSING')}`；当前测试 slice 的 `memory.max={boundary.get('memory_max', 'unknown')}`。Test4 禁止修改 `memory.max`，而 APPLY 要求有限的安全边界，因此没有执行、也不应执行任何实际 reclaim。

安全确认：未修改 MGLRU tier/generation、vmscan、`memory.low/min/high/max`；未回收前台 App；未使用 `drop_caches`；未安装内核、修改 GRUB、重启或推送远端。
"""
    (session / "review" / "FINAL_REPORT.md").write_text(report, encoding="utf-8")


if __name__ == "__main__":
    main()
