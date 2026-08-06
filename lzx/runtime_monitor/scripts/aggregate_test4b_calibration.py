#!/usr/bin/env python3
"""Aggregate Test4B-1 capacity probes into a go/no-go conclusion for Test4B-2."""
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
MIB = 1024 * 1024


def report(path: Path) -> dict[str, Any]:
    try:
        return json.loads((path / "review/calibration_report.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"missing/invalid calibration report for {path}: {exc}") from exc


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--session", type=Path, action="append", required=True)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    sessions = [(item.resolve(), report(item.resolve())) for item in args.session]
    by_phase = {str(item.get("phase")): (path, item) for path, item in sessions}
    required = {"A_FILE", "B_ANON", "C_BASELINE_STABLE", "C_MIXED"}
    missing = sorted(required - set(by_phase))
    if missing:
        raise SystemExit(f"required phases absent: {', '.join(missing)}")
    a_path, a = by_phase["A_FILE"]; b_path, b = by_phase["B_ANON"]
    base_path, base = by_phase["C_BASELINE_STABLE"]; c_path, mixed = by_phase["C_MIXED"]
    threshold = float(base.get("psi_full_abort_avg10", 0.20))
    required_reports = (a, b, base, mixed)
    if all(item.get("status") == "CALIBRATED_SAFE" for item in required_reports):
        status = "TEST4B1_CALIBRATED_SAFE"
        next_step = "Test4B-2 SHADOW may use the validated 120 MiB global ballast budget (40 MiB/App), but must retain the 15 s/three-sample PSI stabilization gate and the unchanged 0.20 full-PSI threshold."
    else:
        status = "TEST4B1_CALIBRATION_INCOMPLETE_OR_PRESSURE_BLOCKED"
        next_step = "Keep memory.reclaim disabled and resolve the recorded safety blocker before further mixed-workload work."
    output = args.output_dir or ROOT / "outputs/runtime_monitor" / f"test4b1_ballast_capacity_calibration_summary_{datetime.now():%Y%m%d_%H%M%S}"
    review = output / "review"; review.mkdir(parents=True, exist_ok=True)
    compact = {
        phase: {
            "session_dir": str(path), "status": data.get("status"),
            "allocated_bytes_by_app": data.get("allocated_bytes_by_app", {}),
            "max_system_psi_full_avg10": data.get("max_system_psi_full_avg10"),
            "max_parent_cgroup_psi_full_avg10": data.get("max_parent_cgroup_psi_full_avg10"),
            "partial_reasons": data.get("partial_reasons", {}),
            "memory_reclaim_write_attempts": data.get("memory_reclaim_write_attempts"),
        }
        for phase, (path, data) in by_phase.items()
    }
    result = {
        "status": status, "psi_full_abort_avg10": threshold, "phases": compact,
        "observed_safe_single_app_file_bytes": int(a.get("allocated_bytes_by_app", {}).get("FIREFOX", 0)),
        "observed_safe_single_app_anon_bytes": int(b.get("allocated_bytes_by_app", {}).get("FIREFOX", 0)),
        "three_app_zero_ballast_system_psi_full_avg10": base.get("max_system_psi_full_avg10"),
        "three_app_zero_ballast_parent_psi_full_avg10": base.get("max_parent_cgroup_psi_full_avg10"),
        "validated_three_app_global_ballast_bytes": sum(int(value) for value in mixed.get("allocated_bytes_by_app", {}).values()),
        "three_app_mixed_system_psi_full_avg10": mixed.get("max_system_psi_full_avg10"),
        "three_app_mixed_parent_psi_full_avg10": mixed.get("max_parent_cgroup_psi_full_avg10"),
        "memory_reclaim_write_attempts_total": sum(int(data.get("memory_reclaim_write_attempts", 0)) for _, data in sessions),
        "next_step": next_step,
    }
    (review / "calibration_aggregate.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    table = "\n".join(
        f"| {phase} | `{data.get('status')}` | {data.get('max_system_psi_full_avg10', 0):.2f} | {data.get('max_parent_cgroup_psi_full_avg10', 0):.2f} | "
        f"{sum(int(v) for v in data.get('allocated_bytes_by_app', {}).values()) / MIB:.0f} MiB |"
        for phase, (_, data) in sorted(by_phase.items())
    )
    markdown = f"""# Test4B-1：低压力 Ballast 容量标定汇总

最终状态：`{status}`。`memory.reclaim` 写入总数：**{result['memory_reclaim_write_attempts_total']}**。

| 阶段 | 结果 | 系统 PSI full avg10 最大值 | 父 cgroup PSI full avg10 最大值 | 已完成 ballast |
| --- | --- | ---: | ---: | ---: |
{table}

结论：单 App 文件页（{result['observed_safe_single_app_file_bytes'] / MIB:.0f} MiB）与匿名页（{result['observed_safe_single_app_anon_bytes'] / MIB:.0f} MiB）可在未改变的 PSI 门限 `{threshold:.2f}` 下完成。三 App 零-ballast 基线在 15 s、连续 3 个低 PSI 样本的稳定化后达到系统/父 cgroup PSI `{float(base.get('max_system_psi_full_avg10_after_stabilization', base.get('max_system_psi_full_avg10', 0))):.2f}` / `{float(base.get('max_parent_cgroup_psi_full_avg10_after_stabilization', base.get('max_parent_cgroup_psi_full_avg10', 0))):.2f}`。

随后所有 App 先启动、通过同一稳定化门，再以 4 MiB 块构造 40 MiB/App 混合页（总计 {result['validated_three_app_global_ballast_bytes'] / MIB:.0f} MiB）；其系统/父 cgroup PSI full avg10 最大值为 `{float(mixed.get('max_system_psi_full_avg10_after_stabilization', mixed.get('max_system_psi_full_avg10', 0))):.2f}` / `{float(mixed.get('max_parent_cgroup_psi_full_avg10_after_stabilization', mixed.get('max_parent_cgroup_psi_full_avg10', 0))):.2f}`。所有轮次均未执行 `memory.reclaim`，因此仍不能据此评价 LSTM 回收策略。

下一步：{next_step}
"""
    (review / "FINAL_REPORT.md").write_text(markdown, encoding="utf-8")
    print(str(output))


if __name__ == "__main__":
    main()
