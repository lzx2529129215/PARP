#!/usr/bin/env python3
"""Aggregate exactly the independently auditable Test4B-2 SHADOW runs."""
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]


def data(path: Path) -> dict[str, Any]:
    try: return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError): return {}


def main() -> int:
    parser = argparse.ArgumentParser(); parser.add_argument("--sessions", type=Path, nargs="+", required=True); parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args(); sessions = [path.resolve() for path in args.sessions]
    out = args.output_dir or ROOT / "outputs/runtime_monitor" / f"test4b2_low_pressure_shadow_summary_{datetime.now():%Y%m%d_%H%M%S}"
    (out / "review").mkdir(parents=True, exist_ok=True)
    overview: list[dict[str, Any]] = []
    for session in sessions:
        summary = data(session / "review/test4b2_shadow_summary.json")
        requirement = dict(summary.get("requirements", {}))
        safety = dict(summary.get("safety", {}))
        funnel = dict(summary.get("candidate_funnel", {}))
        preallocation = dict(summary.get("preallocation", {}))
        overview.append({"session_id": session.name, "status": summary.get("status", "MISSING"),
                         "validation_sequence": requirement.get("validation_sequence_pass", False), "validation_dwell": requirement.get("validation_dwell_pass", False),
                         "full_120mib": requirement.get("three_full_40mib_apps", False), "psi_safe": requirement.get("sustained_psi_full_exceed_zero", False),
                         "candidate": requirement.get("reclaim_candidate_present", False), "if_needed": requirement.get("would_reclaim_if_needed_present", False),
                         "global_ballast_bytes": int(preallocation.get("global_bytes", 0)),
                         "reclaim_attempts": int(safety.get("memory_reclaim_write_attempts", 0)),
                         "reclaim_writes": int(safety.get("memory_reclaim_write_success", 0)),
                         "would_reclaim": int(funnel.get("would_reclaim", 0))})
    valid = [row for row in overview if row["status"] == "PASS"]
    attempts = sum(row["reclaim_attempts"] for row in overview)
    writes = sum(row["reclaim_writes"] for row in overview)
    status = "PARP_TEST4B2_SHADOW_COMPLETE" if len(sessions) == 3 and len(valid) == 3 and attempts == 0 and writes == 0 else "PARP_TEST4B2_SHADOW_INCOMPLETE"
    result = {"status": status, "required_valid_sessions": 3, "valid_sessions": len(valid), "memory_reclaim_attempts": attempts, "memory_reclaim_writes": writes, "sessions": overview,
              "next_step": "Review a separate, one-shot 16 MiB APPLY design only if status is COMPLETE; this aggregate performs no apply."}
    (out / "review/test4b2_shadow_aggregate.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    report = f"""# Test4B-2：低压力 SHADOW 汇总

状态：`{status}`。有效 SHADOW 会话 {len(valid)}/3；真实 `memory.reclaim` 写尝试总数 {attempts}，成功写入总数 {writes}。

| 会话 | 120 MiB ballast | validation/dwell | PSI 安全 | 候选 / IF_NEEDED / WOULD | reclaim 写尝试 |
| --- | --- | --- | --- | --- | --- |
{chr(10).join(f'| {row["session_id"]} | {row["full_120mib"]} | {row["validation_sequence"]}/{row["validation_dwell"]} | {row["psi_safe"]} | {row["candidate"]}/{row["if_needed"]}/{row["would_reclaim"]} | {row["reclaim_attempts"]} |' for row in overview)}

只有每个会话都通过 120 MiB 混合 ballast、validation/dwell、PSI 安全、候选漏斗和零写入约束时，才允许把 Test4B-3 的一次性 16 MiB APPLY 作为独立任务评审。本汇总没有执行 APPLY。
"""
    (out / "review/FINAL_REPORT.md").write_text(report, encoding="utf-8")
    print(out)
    return 0 if status == "PARP_TEST4B2_SHADOW_COMPLETE" else 1


if __name__ == "__main__":
    raise SystemExit(main())
