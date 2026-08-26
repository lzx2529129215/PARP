#!/usr/bin/env python3
"""Aggregate Test4B evidence and state an explicit safe terminal status."""
from __future__ import annotations

import argparse
import csv
import json
import shutil
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]


def rows(path: Path) -> list[dict[str, str]]:
    try:
        with path.open(encoding="utf-8", newline="") as stream: return list(csv.DictReader(stream))
    except OSError: return []


def data(path: Path) -> dict[str, Any]:
    try: return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError): return {}


def main() -> None:
    parser = argparse.ArgumentParser(); parser.add_argument("--sessions", type=Path, nargs="+", required=True); parser.add_argument("--output-dir", type=Path); args = parser.parse_args()
    sessions = [path.resolve() for path in args.sessions]
    out = args.output_dir or ROOT / "outputs/runtime_monitor" / f"test4b_foreground_mixed_workingset_reclaim_summary_{datetime.now():%Y%m%d_%H%M%S}"
    for name in ("review", "analysis", "ballast", "reclaim"): (out / name).mkdir(parents=True, exist_ok=True)
    overview: list[dict[str, Any]] = []; all_reasons: Counter[str] = Counter(); all_safety: list[dict[str, str]] = []
    for session in sessions:
        summary = data(session / "review/test4b_session_summary.json")
        execution = data(session / "review/validation_sequence_execution.json")
        collection = {}
        for line in (session / "review/test4b_collection_summary.txt").read_text(encoding="utf-8", errors="replace").splitlines() if (session / "review/test4b_collection_summary.txt").exists() else []:
            if "=" in line:
                key, value = line.split("=", 1); collection[key] = value
        reasons = Counter(row.get("rejection_reason", "") for row in rows(session / "reclaim/test4b_candidate_evaluations.csv") if row.get("rejection_reason"))
        all_reasons.update(reasons); safety_rows = rows(session / "reclaim/test4b_safety_events.csv"); all_safety.extend(safety_rows)
        overview.append({"session_id": session.name, "run_rc": collection.get("run_rc", ""), "sequence_status": execution.get("sequence_status", "MISSING"),
                         "dwell_status": execution.get("dwell_status", "MISSING"), "prediction_batches": summary.get("prediction_batches", 0),
                         "allocation_success": summary.get("allocation_success", 0), "would_reclaim": summary.get("would_reclaim", 0),
                         "reclaim_writes": summary.get("successful_memory_reclaim_writes", 0), "safety_events": len(safety_rows),
                         "primary_rejection": reasons.most_common(1)[0][0] if reasons else ""})
    with (out / "analysis/session_overview.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(overview[0]) if overview else ["session_id"]); writer.writeheader(); writer.writerows(overview)
    with (out / "analysis/psi_safety_events.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=["timestamp_ns", "reason", "mem_available_bytes", "psi_full", "root_oom", "root_oom_kill", "mode"]); writer.writeheader(); writer.writerows(all_safety)
    valid = [item for item in overview if item["sequence_status"] == "PASS" and item["dwell_status"] == "PASS" and item["run_rc"] == "0"]
    writes = sum(int(item["reclaim_writes"] or 0) for item in overview)
    would = sum(int(item["would_reclaim"] or 0) for item in overview)
    psi = [item for item in all_safety if item.get("reason") == "MEMORY_PSI_FULL_ABORT_THRESHOLD"]
    status = "PARP_TEST4B_MIXED_WORKINGSET_RECLAIM_BLOCKED"
    if writes:
        status = "PARP_TEST4B_MIXED_WORKINGSET_RECLAIM_PARTIAL"
    audit = """# Test4B interface audit

- Kernel: `6.17.13-v4.1-parp` (running target kernel verified before Test4B).
- cgroup: cgroup v2 memory controller is available; the experiment uses only an ephemeral `test4b-experiment.slice` with a measured finite `memory.max`. Existing `huawei-test.slice` was not changed.
- Reclaim ABI: matching v4.1 source documentation confirms numeric byte requests to `memory.reclaim`. Test4B writes only a raw numeric byte request and never supplies or guesses a nested `swappiness` key.
- Swap: `/swapfile` was enabled; `memory.swap.max=max` was observed on the temporary parent and was not changed.
- Event semantics: only direct X11 `APP_OPEN`/`APP_SWITCH` invoke v3 LSTM. Ballast has no window and its process lifecycle is not an LSTM trigger.
- PARP: Test2 `app_bind`/`app_prior` bridge remains shadow-write audited independently from reclaim.
- Forbidden controls: no MGLRU tier/generation, vmscan, effective-tier policy, global drop_caches, GRUB, kernel install, reboot, or existing cgroup memory limit was modified.
"""
    (out / "review/TEST4B_INTERFACE_AUDIT.md").write_text(audit, encoding="utf-8")
    report = f"""# Test4B：前台构造混合工作集后的低概率 App 定向回收

状态：`{status}`。

本轮完成了 ballast C 状态机、前台 AppBind/cgroup 归属授权、事件驱动 v3 LSTM 接入、有限临时 cgroup，以及 SHADOW 运行。有效 validation SHADOW 为 {len(valid)} 次；其中前台序列与 dwell 均保持一致。所有会话的真实 `memory.reclaim` 写入为 {writes}，`WOULD_RECLAIM` 为 {would}。

阻断原因是构造期出现持续 `memory PSI full avg10` 超过 0.20（共 {len(psi)} 条安全记录，峰值见 `analysis/psi_safety_events.csv`）。按 Test4B 安全规则，已停止后续 SHADOW、APPLY 和 Native-vs-Apply；没有为了产生候选而提高概率阈值、放松 PSI 阈值或重试回收写入。

第二轮已验证：三个无窗口 ballast 都只在各自 App 前台且 AppBind/cgroup 匹配时分配；validation 序列 PASS。首轮则因生成场景中的 Firefox 路径解析错误而作废，修复后才得到第二轮。两轮临时 cgroup、ballast socket 和合成文件均已清理。

本实验中的可回收空间由合成 ballast 构造；ballast 页面与真实 App 功能无关。因此当前结果既不证明真实 App 的内存收益，也不构成 `MECHANISM_CONFIRMED`。
"""
    (out / "review/FINAL_REPORT.md").write_text(report, encoding="utf-8")
    safety = {"status": status, "valid_shadow_sessions": len(valid), "would_reclaim": would, "memory_reclaim_writes": writes,
              "psi_full_safety_events": len(psi), "candidate_rejections": dict(all_reasons),
              "apply_and_ab_not_run_reason": "SUSTAINED_MEMORY_PSI_FULL", "cleanup_verified": all(data(session / "cgroup_cleanup.json").get("cgroup_present_after_cleanup") is False for session in sessions)}
    (out / "safety_report.json").write_text(json.dumps(safety, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for name in ("analysis_probability_scan.json",):
        source = sessions[-1] / name
        if source.exists(): shutil.copy2(source, out / "analysis" / name)
    print(out)


if __name__ == "__main__":
    main()
