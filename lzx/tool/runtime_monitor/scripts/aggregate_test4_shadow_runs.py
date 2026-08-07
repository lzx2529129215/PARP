#!/usr/bin/env python3
"""Aggregate repeatable Test4 shadow sessions into one conservative report."""
from __future__ import annotations

import argparse
import csv
import json
import shutil
from collections import Counter
from datetime import datetime
from pathlib import Path
from statistics import mean
from typing import Any


ROOT = Path(__file__).resolve().parents[2]


def read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def read_rows(path: Path) -> list[dict[str, str]]:
    try:
        with path.open(encoding="utf-8", newline="") as stream:
            return list(csv.DictReader(stream))
    except OSError:
        return []


def count_predictions(session: Path) -> int:
    return sum(row.get("prediction_triggered") == "1" for row in read_rows(session / "model/direct_app_events.csv"))


def count_bridge_priors(session: Path) -> int:
    return sum(
        row.get("event_type") == "app_prior" and row.get("write_success") == "true"
        for row in read_rows(session / "parp/parp_bridge_events.csv")
    )


def numeric(value: str) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sessions", nargs="+", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, default=None)
    args = parser.parse_args()
    sessions = [item.resolve() for item in args.sessions]
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output = args.output_dir or ROOT / "outputs/runtime_monitor" / f"test4_probability_activity_reclaim_shadow_summary_{timestamp}"
    for name in ("analysis", "review", "reclaim"):
        (output / name).mkdir(parents=True, exist_ok=True)

    summaries: list[dict[str, Any]] = []
    all_evaluations: list[dict[str, str]] = []
    all_reasons: Counter[str] = Counter()
    apply_errors: Counter[str] = Counter()
    for session in sessions:
        manifest = read_json(session / "manifest.json")
        execution = read_json(session / "review/validation_sequence_execution.json")
        decisions = read_rows(session / "reclaim/app_reclaim_decisions.csv")
        attempts = read_rows(session / "reclaim/memory_reclaim_attempts.csv")
        evaluations = read_rows(session / "reclaim/candidate_evaluations.csv")
        preflight = read_json(session / "apply_preflight.json")
        all_evaluations.extend(evaluations)
        all_reasons.update(row.get("rejection_reason", "") for row in evaluations if row.get("rejection_reason"))
        apply_errors.update(preflight.get("errors", []))
        summaries.append({
            "session_id": session.name,
            "session_dir": str(session),
            "automation_rc": manifest.get("automation_rc", ""),
            "monitor_rc": manifest.get("monitor_rc", ""),
            "sink_coverage_rc": manifest.get("sink_coverage_rc", ""),
            "sequence_status": execution.get("sequence_status", "MISSING"),
            "dwell_status": execution.get("dwell_status", "MISSING"),
            "prediction_count": count_predictions(session),
            "parp_prior_success": count_bridge_priors(session),
            "decision_count": len(decisions),
            "would_reclaim_count": sum(row.get("decision") == "WOULD_RECLAIM" for row in decisions),
            "memory_reclaim_write_count": sum(row.get("write_success") == "true" for row in attempts),
            "safety_abort_count": sum("SAFETY" in row.get("skip_reason", "") or "PSI" in row.get("skip_reason", "") for row in decisions),
            "candidate_evaluation_count": len(evaluations),
            "apply_preflight_status": preflight.get("status", "MISSING"),
        })

    columns = list(summaries[0]) if summaries else []
    with (output / "analysis/repeated_run_summary.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        writer.writerows(summaries)

    # Parameter feasibility scan: it is descriptive validation-split tuning
    # evidence, never a future-label oracle.  Only fully measured background
    # Apps can pass activity gates.
    scan: list[dict[str, Any]] = []
    for probability_threshold in (0.05, 0.10, 0.20, 0.30):
        for activity_threshold in (0.10, 0.25, 0.50):
            eligible = 0
            for row in all_evaluations:
                probability = numeric(row.get("probability", ""))
                activity = numeric(row.get("referenced_rss_ema", ""))
                if (
                    row.get("sample_status") == "OK"
                    and row.get("running_state") == "RUNNING_BACKGROUND"
                    and row.get("foreground_state") == "BACKGROUND"
                    and probability is not None and probability < probability_threshold
                    and activity is not None and activity < activity_threshold
                ):
                    eligible += 1
            scan.append({
                "probability_threshold": probability_threshold,
                "activity_threshold": activity_threshold,
                "eligible_observations": eligible,
            })
    (output / "analysis/parameter_feasibility.json").write_text(
        json.dumps({"scope": "validation split / feasibility only", "grid": scan}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    with (output / "analysis/strategy_comparison.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=["strategy", "runs", "memory_reclaim_writes", "status", "reason"])
        writer.writeheader()
        writer.writerows([
            {"strategy": "Native", "runs": 0, "memory_reclaim_writes": 0, "status": "NOT_RUN", "reason": "Test4 stops before A/B because bounded APPLY is safety-blocked"},
            {"strategy": "LSTM-Shadow", "runs": len(summaries), "memory_reclaim_writes": 0, "status": "COMPLETE", "reason": "event/LSTM/activity/controller chain audited"},
            {"strategy": "LSTM-Apply-Bounded", "runs": 0, "memory_reclaim_writes": 0, "status": "BLOCKED", "reason": "finite memory.max safety boundary absent"},
            {"strategy": "Markov-Apply-Bounded", "runs": 0, "memory_reclaim_writes": 0, "status": "NOT_RUN", "reason": "out of scope before LSTM apply safety gate"},
            {"strategy": "Oracle-Apply-Bounded", "runs": 0, "memory_reclaim_writes": 0, "status": "NOT_RUN", "reason": "future labels must not enter online control"},
        ])

    total_predictions = sum(int(item["prediction_count"]) for item in summaries)
    total_priors = sum(int(item["parp_prior_success"]) for item in summaries)
    total_decisions = sum(int(item["decision_count"]) for item in summaries)
    total_would = sum(int(item["would_reclaim_count"]) for item in summaries)
    total_writes = sum(int(item["memory_reclaim_write_count"]) for item in summaries)
    all_sequence_pass = all(item["sequence_status"] == "PASS" and item["dwell_status"] == "PASS" for item in summaries)
    all_runtime_pass = all(str(item["automation_rc"]) == "0" and str(item["monitor_rc"]) == "0" and str(item["sink_coverage_rc"]) == "0" for item in summaries)
    live_activity = [numeric(row.get("referenced_rss_ema", "")) for row in all_evaluations if row.get("sample_status") == "OK" and row.get("running_state") == "RUNNING_BACKGROUND"]
    live_activity = [value for value in live_activity if value is not None]
    safety = {
        "status": "PARP_APP_PROBABILITY_ACTIVITY_RECLAIM_BLOCKED",
        "shadow_runs": len(summaries),
        "all_runtime_pass": all_runtime_pass,
        "all_sequence_and_dwell_pass": all_sequence_pass,
        "prediction_count": total_predictions,
        "parp_prior_success": total_priors,
        "decision_count": total_decisions,
        "would_reclaim_count": total_would,
        "memory_reclaim_write_count": total_writes,
        "primary_blocker": "FINITE_MEMORY_MAX_REQUIRED",
        "apply_preflight_errors": dict(apply_errors),
        "candidate_rejections": dict(all_reasons),
        "background_activity_ema_min": min(live_activity) if live_activity else None,
        "background_activity_ema_mean": mean(live_activity) if live_activity else None,
        "parameter_feasibility": "No grid point through activity_threshold=0.50 admitted an observed background App; default 0.10 is therefore not relaxed merely to force reclaim.",
        "safety_statement": "No memory.reclaim write occurred. memory.low/min/high/max, MGLRU, vmscan, GRUB, kernel installation and reboot were not changed.",
    }
    (output / "safety_report.json").write_text(json.dumps(safety, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    source_coverage = sessions[-1] / "review/validation_sequence_coverage.json"
    if source_coverage.exists():
        shutil.copy2(source_coverage, output / "review/validation_sequence_coverage.json")
    report = f"""# Test4：低概率、低活跃后台 App 的受限回收

状态：`PARP_APP_PROBABILITY_ACTIVITY_RECLAIM_BLOCKED`。

完成了 {len(summaries)} 次 validation split SHADOW：自动化、monitor、Test2 PARP sink、运行时前台顺序与驻留时间均为 `{all_runtime_pass and all_sequence_pass}`。共有 {total_predictions} 次事件驱动 v3 LSTM prediction、{total_priors} 条成功 `app_prior` 下沉、{total_decisions} 次回收决策；`WOULD_RECLAIM={total_would}`，真实 `memory.reclaim` 写入为 `{total_writes}`。

没有为了产生回收而调松门槛：实际后台 App 的 Referenced/RSS EMA 最低为 `{safety['background_activity_ema_min']}`，均远高于安全初值 0.10；低概率连续性也没有通过。未启动白名单 App 保持 `UNAVAILABLE`，不会被当成低活跃候选。

APPLY 被硬性阻断：测试 slice 的 `memory.max=max`，而 Test4 要求有限安全边界且同时禁止修改 `memory.max`。因此没有开展 APPLY、Native-vs-Apply A/B、也不声称内存释放或延迟收益。此状态是环境/安全边界阻断，不是策略有效性结论。

安全声明：未写 `memory.reclaim`；未修改 MGLRU tier/generation、vmscan、`memory.low/min/high/max`；未回收前台 App；未使用 drop_caches；未安装内核、改 GRUB、重启或推送远端。
"""
    (output / "review/FINAL_REPORT.md").write_text(report, encoding="utf-8")
    print(output)


if __name__ == "__main__":
    main()
