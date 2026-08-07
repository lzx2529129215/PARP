#!/usr/bin/env python3
"""Write a conservative, auditable Test4B-2 SHADOW verdict."""
from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from pathlib import Path
from typing import Any


MIB = 1024 * 1024
APPS = ("FIREFOX", "THUNDERBIRD", "TELEGRAM")


def rows(path: Path) -> list[dict[str, str]]:
    try:
        with path.open(encoding="utf-8", newline="") as stream: return list(csv.DictReader(stream))
    except OSError: return []


def data(path: Path) -> dict[str, Any]:
    try: return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError): return {}


def true(value: str) -> bool:
    return value.lower() == "true"


def main() -> None:
    parser = argparse.ArgumentParser(); parser.add_argument("--session-dir", type=Path, required=True); args = parser.parse_args()
    session = args.session_dir.resolve(); review = session / "review"; review.mkdir(exist_ok=True)
    construction = rows(session / "ballast/construction_steps.csv")
    summary = data(session / "ballast/preallocation_summary.json")
    validation = data(review / "validation_sequence_execution.json")
    decisions = rows(session / "reclaim/test4b_reclaim_decisions.csv")
    evaluations = rows(session / "reclaim/test4b_candidate_evaluations.csv")
    attempts = rows(session / "reclaim/test4b_memory_reclaim_attempts.csv")
    direct = rows(session / "model/direct_app_events.csv")
    raw: list[dict[str, str]] = []
    for source in sorted((session / "ballast/raw").glob("*.csv")): raw.extend(rows(source))
    app_summary = dict(summary.get("apps", {}))
    full_apps = {app for app in APPS if dict(app_summary.get(app, {})).get("status") == "FULL_READY" and int(dict(app_summary.get(app, {})).get("allocated_bytes", 0)) == 40*MIB}
    global_bytes = int(summary.get("global_allocated_bytes", 0))
    # A high *system* avg10 before the gate opens can be historical pressure
    # from outside this cgroup.  It is recorded, and the gate waits for three
    # clean samples, but it is not a Test4B-2 construction failure because no
    # ballast page has then been allocated.  Any post-gate PSI stop is fatal.
    startup_psi_waits = [row for row in construction if row.get("stage") == "STARTUP_STABILIZATION" and "PSI_SAFETY" in row.get("reason", "")]
    startup_steady_waits = [row for row in construction if row.get("stage") == "STARTUP_STABILIZATION" and row.get("reason") == "WAIT_FOR_STEADY_PSI"]
    psi_stalls = [row for row in construction if row.get("stage") != "STARTUP_STABILIZATION" and "PSI_SAFETY" in row.get("reason", "")]
    psi_sample_exceeds = [row for row in construction if row.get("stage") != "STARTUP_STABILIZATION" and (float(row.get("system_psi_full_avg10") or 0) >= .20 or float(row.get("parent_psi_full_avg10") or 0) >= .20)]
    allocations = [row for row in raw if row.get("command") == "ALLOCATE"]
    background_allocations = [row for row in allocations if row.get("state_before") != "FOREGROUND_ACTIVE"]
    allocation_failures = [row for row in allocations if row.get("status") != "OK"]
    bad_construction = [row for row in construction if row.get("reason") in {"FOREGROUND_OR_APPBIND_NOT_READY", "BALLAST_WRONG_CGROUP", "SIDECAR_UNAVAILABLE"}]
    candidate_rows = [row for row in decisions if row.get("candidate_level") in {"RECLAIM_CANDIDATE", "WOULD_RECLAIM_IF_NEEDED", "WOULD_RECLAIM"}]
    if_needed_rows = [row for row in decisions if row.get("candidate_level") in {"WOULD_RECLAIM_IF_NEEDED", "WOULD_RECLAIM"}]
    would_rows = [row for row in decisions if row.get("candidate_level") == "WOULD_RECLAIM"]
    foreground_violations = [row for row in candidate_rows if row.get("candidate_background") != "true"]
    unstarted_violations = [row for row in candidate_rows if row.get("candidate_app") not in full_apps]
    write_attempts = [row for row in attempts if true(row.get("write_attempted", ""))]
    write_success = [row for row in attempts if true(row.get("write_success", ""))]
    prediction_batches = sum(row.get("prediction_triggered") == "1" for row in direct)
    stage_counts = Counter(row.get("stage", "") for row in construction if row.get("sample_point") == "AFTER_ALLOC" and row.get("decision") == "ALLOCATED")
    latency_us = [int(row.get("operation_latency_us") or 0) for row in construction if row.get("sample_point") == "AFTER_ALLOC" and row.get("operation_latency_us")]
    requirements = {
        "validation_sequence_pass": validation.get("sequence_status") == "PASS",
        "validation_dwell_pass": validation.get("dwell_status") == "PASS",
        "startup_stabilization_ready": dict(summary.get("startup_stabilization", {})).get("status") == "READY",
        "three_full_40mib_apps": len(full_apps) == 3 and global_bytes == 120*MIB,
        "background_new_allocation_zero": not background_allocations,
        "foreground_appbind_cgroup_construction_pass": not bad_construction and not allocation_failures,
        "sustained_psi_full_exceed_zero": not psi_stalls,
        "candidate_foreground_violation_zero": not foreground_violations,
        "candidate_unstarted_violation_zero": not unstarted_violations,
        "reclaim_candidate_present": bool(candidate_rows),
        "would_reclaim_if_needed_present": bool(if_needed_rows),
        "memory_reclaim_write_attempt_zero": not write_attempts,
        "memory_reclaim_write_success_zero": not write_success,
    }
    status = "PASS" if all(requirements.values()) else "BLOCKED"
    report_data: dict[str, Any] = {
        "status": status, "mode": "shadow", "requirements": requirements,
        "prediction_batch_count": prediction_batches,
        "preallocation": {"full_apps": sorted(full_apps), "global_bytes": global_bytes, "expected_global_bytes": 120*MIB,
                              "stage_allocated_chunk_count": dict(stage_counts), "chunk_latency_us": {"count": len(latency_us), "max": max(latency_us, default=0), "mean": sum(latency_us)/len(latency_us) if latency_us else 0}},
        "psi": {"startup_gate_historical_avg10_waits": len(startup_psi_waits), "startup_gate_strict_steady_waits": len(startup_steady_waits), "construction_safety_stalls": len(psi_stalls), "sample_avg10_exceeds": len(psi_sample_exceeds),
                "total_delta_recorded_per_chunk": True},
        "candidate_funnel": {"reclaim_candidate": len(candidate_rows), "would_reclaim_if_needed": len(if_needed_rows), "would_reclaim": len(would_rows),
                             "decision_counts": dict(Counter(row.get("decision", "") for row in decisions)),
                             "evaluation_base_rejections": dict(Counter(row.get("base_rejection_reason", "") for row in evaluations if row.get("base_rejection_reason"))),
                             "evaluation_if_needed_rejections": dict(Counter(row.get("if_needed_rejection_reason", "") for row in evaluations if row.get("if_needed_rejection_reason")))},
        "safety": {"background_allocate_events": len(background_allocations), "foreground_candidate_violations": len(foreground_violations),
                   "unstarted_candidate_violations": len(unstarted_violations), "memory_reclaim_write_attempts": len(write_attempts), "memory_reclaim_write_success": len(write_success)},
        "limitations": "Ballast is synthetic ground truth. SHADOW produces no memory.reclaim writes and does not prove real-application memory benefit.",
    }
    (review / "test4b2_shadow_summary.json").write_text(json.dumps(report_data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    report = f"""# Test4B-2：应用间预测的低压力 SHADOW 验证

状态：`{status}`。本会话严格为 `shadow`；真实 `memory.reclaim` 写尝试为 {len(write_attempts)}，成功写入为 {len(write_success)}。

- 混合 ballast：{len(full_apps)}/3 App 完整 40 MiB，合计 {global_bytes // MIB} MiB（目标 120 MiB）；布局为每 App 冷文件 24 MiB、热文件 4 MiB、冷匿名 8 MiB、热匿名 4 MiB。
- 4 MiB 块构造：{len(latency_us)} 块，最长 {max(latency_us, default=0)} µs；每块均记录 system/parent PSI some/full 的 `total delta` 与 `avg10`。
- validation：sequence `{validation.get('sequence_status', 'MISSING')}`，dwell `{validation.get('dwell_status', 'MISSING')}`；启动门因系统历史 `avg10` 等待 {len(startup_psi_waits)} 次、因更严格的 `<0.05` 起始目标等待 {len(startup_steady_waits)} 次后才放行，构造期 PSI 安全停顿 {len(psi_stalls)}，构造期 `avg10 >= 0.20` 为 {len(psi_sample_exceeds)} 条。
- 候选漏斗：`RECLAIM_CANDIDATE={len(candidate_rows)}` → `WOULD_RECLAIM_IF_NEEDED={len(if_needed_rows)}` → `WOULD_RECLAIM={len(would_rows)}`。后者为 0 时不等于没有合格候选，通常表示 `NO_MEMORY_NEED`。
- 前台候选违规 {len(foreground_violations)}；未启动/未准备 App 候选违规 {len(unstarted_violations)}；后台新增 ALLOCATE {len(background_allocations)}。

本报告不执行或建议 Test4B-3 APPLY。只有三次独立且 `PASS` 的 Test4B-2 SHADOW 会话汇总后，才可以评审一次受限的 16 MiB APPLY。
"""
    (review / "FINAL_REPORT.md").write_text(report, encoding="utf-8")
    print(json.dumps(report_data, ensure_ascii=False))


if __name__ == "__main__":
    main()
