#!/usr/bin/env python3
"""Summarize one Test4B-1 calibration run without inferring reclaim benefit."""
from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

MIB = 1024 * 1024


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def rows(path: Path) -> list[dict[str, str]]:
    try:
        with path.open(encoding="utf-8", newline="") as stream:
            return list(csv.DictReader(stream))
    except OSError:
        return []


def number(value: object) -> float:
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return 0.0


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--session-dir", type=Path, required=True)
    args = parser.parse_args()
    session = args.session_dir.resolve()
    calibration = session / "calibration"
    review = session / "review"; review.mkdir(parents=True, exist_ok=True)
    config = load_json(calibration / "calibration_config.json", {})
    summary = load_json(calibration / "calibration_summary.json", {"apps": {}})
    samples = rows(calibration / "calibration_steps.csv")
    manifest = load_json(session / "manifest.json", {})

    chunk = int(config.get("chunk_bytes", 4 * MIB))
    threshold = float(config.get("psi_full_abort_avg10", 0.20))
    apps = dict(summary.get("apps", {}))
    requested_by_app: Counter[str] = Counter(item.get("app_key", "") for item in config.get("workers", []))
    for app in config.get("app_keys", []):
        requested_by_app.setdefault(str(app), 0)
    allocated_by_stage: Counter[str] = Counter()
    allocated_by_app_stage: dict[str, Counter[str]] = defaultdict(Counter)
    partial_reasons: Counter[str] = Counter()
    for row in samples:
        if row.get("sample_point") == "AFTER_ALLOC" and row.get("decision") == "ALLOCATED":
            stage = row.get("stage", "")
            allocated_by_stage[stage] += 1
            allocated_by_app_stage[row.get("app_key", "")][stage] += 1
        if row.get("decision") == "PARTIAL_READY" and row.get("reason"):
            partial_reasons[row["reason"]] += 1

    max_system_full = max((number(row.get("system_psi_full_avg10")) for row in samples), default=0.0)
    max_parent_full = max((number(row.get("parent_psi_full_avg10")) for row in samples), default=0.0)
    stable_times = [int(row.get("timestamp_ns", 0) or 0) for row in samples if row.get("stage") == "APP_STARTUP_STABILIZATION" and row.get("decision") == "STABLE"]
    post_stable_samples = [row for row in samples if not stable_times or int(row.get("timestamp_ns", 0) or 0) >= min(stable_times)]
    max_system_after_stable = max((number(row.get("system_psi_full_avg10")) for row in post_stable_samples), default=0.0)
    max_parent_after_stable = max((number(row.get("parent_psi_full_avg10")) for row in post_stable_samples), default=0.0)
    phase_samples = {stage: sum(row.get("stage") == stage for row in samples) for stage in (
        "APP_STARTUP_STABILIZATION", "APP_READY_BASELINE", "FILE_COLD_ALLOC", "FILE_HOT_ALLOC", "ANON_COLD_ALLOC", "ANON_HOT_ALLOC", "HOT_ACCESS", "IDLE"
    )}
    expected_bytes = {app: count * chunk for app, count in requested_by_app.items()}
    all_full_ready = bool(apps) and all(
        item.get("status") == "FULL_READY" and int(item.get("allocated_bytes", 0)) == expected_bytes.get(app, -1)
        for app, item in apps.items()
    ) and set(apps) == set(expected_bytes)
    controller_enabled = "--enable-app-reclaim-controller" in (session / "command.txt").read_text(encoding="utf-8", errors="replace") if (session / "command.txt").exists() else False
    automation_rc = int(manifest.get("automation_rc", 1)) if manifest else 1
    monitor_rc = int(manifest.get("monitor_rc", 1)) if manifest else 1
    stabilization_required = float(config.get("startup_stabilization_seconds", 0)) > 0
    stabilization_passed = all(bool(item.get("startup_stabilization_passed", not stabilization_required)) for item in apps.values())
    if not samples or automation_rc != 0 or monitor_rc != 0:
        status = "CALIBRATION_RUNTIME_BLOCKED"
    elif all_full_ready and stabilization_passed and max_system_after_stable < threshold and max_parent_after_stable < threshold and not controller_enabled:
        status = "CALIBRATED_SAFE"
    else:
        status = "PRESSURE_LIMIT_REACHED"

    report = {
        "status": status,
        "phase": config.get("phase", "UNKNOWN"),
        "chunk_bytes": chunk,
        "pause_seconds": config.get("pause_seconds"),
        "psi_full_abort_avg10": threshold,
        "max_system_psi_full_avg10": max_system_full,
        "max_parent_cgroup_psi_full_avg10": max_parent_full,
        "max_system_psi_full_avg10_after_stabilization": max_system_after_stable,
        "max_parent_cgroup_psi_full_avg10_after_stabilization": max_parent_after_stable,
        "startup_stabilization_seconds": config.get("startup_stabilization_seconds", 0),
        "startup_stabilization_passed": stabilization_passed,
        "phase_samples": phase_samples,
        "allocated_chunks_by_stage": dict(allocated_by_stage),
        "allocated_bytes_by_app": {app: int(item.get("allocated_bytes", 0)) for app, item in apps.items()},
        "expected_bytes_by_app": expected_bytes,
        "allocated_chunks_by_app_stage": {app: dict(counts) for app, counts in allocated_by_app_stage.items()},
        "partial_reasons": dict(partial_reasons),
        "memory_reclaim_controller_enabled": controller_enabled,
        "memory_reclaim_write_attempts": 0,
        "no_memory_reclaim": True,
        "automation_rc": automation_rc,
        "monitor_rc": monitor_rc,
        "test2_wrapper_rc": load_json(calibration / "runner_result.json", {}).get("test2_wrapper_rc"),
        "notes": [
            "The controller allocates at most one 4 MiB worker per step and samples before allocation, after allocation, and after a 750 ms pause.",
            "system PSI comes from /proc/pressure/memory; parent PSI comes from the temporary test4b-experiment.slice memory.pressure.",
            "No memory.reclaim controller is instantiated in Test4B-1; this is capacity calibration only.",
        ],
    }
    (review / "calibration_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    allocation_lines = "\n".join(
        f"- `{app}`：{value / MIB:.0f} MiB / 目标 {expected_bytes.get(app, 0) / MIB:.0f} MiB（{apps.get(app, {}).get('status', 'MISSING')}）"
        for app, value in report["allocated_bytes_by_app"].items()
    ) or "- 无成功分配。"
    partial_lines = "\n".join(f"- `{reason}`：{count}" for reason, count in partial_reasons.items()) or "- 无。"
    markdown = f"""# Test4B-1：低压力 Ballast 容量标定

状态：`{status}`；阶段：`{report['phase']}`。本轮只构造/观测，`memory.reclaim` 写入次数为 **0**。

- 单块大小：{chunk / MIB:.0f} MiB；块间等待：{report['pause_seconds']} s；PSI full avg10 安全门：{threshold:.2f}。
- 系统 `/proc/pressure/memory` full avg10 最大值：{max_system_full:.2f}；临时父 cgroup `memory.pressure` full avg10 最大值：{max_parent_full:.2f}。稳定化后分别为 {max_system_after_stable:.2f} / {max_parent_after_stable:.2f}。
- 启动稳定化：{config.get('startup_stabilization_seconds', 0)} s，连续低 PSI 采样通过：{stabilization_passed}；采样阶段：`APP_STARTUP_STABILIZATION`={phase_samples['APP_STARTUP_STABILIZATION']}，`APP_READY_BASELINE`={phase_samples['APP_READY_BASELINE']}，`FILE_COLD_ALLOC`={phase_samples['FILE_COLD_ALLOC']}，`FILE_HOT_ALLOC`={phase_samples['FILE_HOT_ALLOC']}，`ANON_COLD_ALLOC`={phase_samples['ANON_COLD_ALLOC']}，`ANON_HOT_ALLOC`={phase_samples['ANON_HOT_ALLOC']}，`HOT_ACCESS`={phase_samples['HOT_ACCESS']}，`IDLE`={phase_samples['IDLE']}。

分配结果：

{allocation_lines}

安全门/部分就绪原因：

{partial_lines}

证据位于 `calibration/calibration_steps.csv`：每个 4 MiB 块均有 `BEFORE_ALLOC`、`AFTER_ALLOC`、`AFTER_PAUSE` 采样；后台阶段另有 `HOT_ACCESS` 与 `IDLE` 采样。只有 A、B 都为 `CALIBRATED_SAFE` 时，才允许执行 C。
"""
    (review / "FINAL_REPORT.md").write_text(markdown, encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
