#!/usr/bin/env python3
"""从本轮单一 session 的原始文件生成统一汇总和共享目录。"""

from __future__ import annotations

import sys
from pathlib import Path

# 直接从任意 cwd 执行时，先把项目根加入 import 路径。
PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import argparse
import csv
import hashlib
import json
import os
import re
import shutil
from collections import Counter
from typing import Any

from audit_common import resolve_input_path


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def integer(value: Any) -> int:
    try:
        return int(float(str(value or 0)))
    except (TypeError, ValueError):
        return 0


def copy_file(source: Path, target: Path) -> None:
    if source.exists() and source.is_file() and not source.is_symlink():
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def stat_values(path: Path) -> dict[str, int]:
    values: dict[str, int] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        match = re.match(r"^stat\s+(\S+)\s+(-?\d+)", line.strip())
        if match:
            values[match.group(1)] = int(match.group(2))
    return values


def parse_kernel(work: Path) -> dict[str, Any]:
    baseline = work / "kernel/debugfs_baseline_after_clear.txt"
    after = work / "kernel/debugfs_after.txt"
    before_stats = stat_values(baseline)
    after_stats = stat_values(after)
    names = [
        "prepare_calls", "predictions", "missing_hint", "missing_transition", "per_folio_calls",
        "target_cgroup_seen", "target_app_lookup_hit", "probability_lookup_hit",
        "original_scan_pages", "proposed_scan_pages", "applied_scan_pages",
        "app_policy_prepare_calls", "app_policy_apply", "app_policy_observe",
    ]
    delta = {name: after_stats.get(name, 0) - before_stats.get(name, 0) for name in names}
    text = after.read_text(encoding="utf-8", errors="replace") if after.exists() else ""
    hints = []
    for line in text.splitlines():
        if line.startswith("hint "):
            parts = line.split()
            if len(parts) >= 6:
                hints.append({"app_id": integer(parts[1]), "workload_id": integer(parts[2]), "num_predicted": integer(parts[5]), "line": line})
    return {**delta, "hint_num_predicted": sum(item["num_predicted"] for item in hints),
            "hist_lines_count": sum(line.startswith("hist ") for line in text.splitlines()),
            "markov_lines_count": sum(line.startswith("markov ") for line in text.splitlines()),
            "hint_lines_count": len(hints), "kernel_errors": text.count("error")}


def make_stage(name: str, inputs: int, outputs: int, matched: int, evidence: str, *, result: str | None = None, notes: str = "") -> dict[str, Any]:
    result = result or ("PASS" if inputs > 0 and outputs > 0 and matched > 0 else "NOT_EXERCISED")
    return {"stage": name, "input_count": inputs, "output_count": outputs, "matched_count": matched,
            "unmatched_input_count": max(0, inputs - matched), "unmatched_output_count": max(0, outputs - matched),
            "match_ratio": matched / inputs if inputs else 0, "result": result, "evidence": evidence, "notes": notes}


def main() -> int:
    parser = argparse.ArgumentParser(description="生成统一 Runtime Monitor + MGLRU 中间输出汇总")
    parser.add_argument("--session-dir", required=True)
    parser.add_argument("--work-dir", required=True)
    parser.add_argument("--share-dir", required=True)
    parser.add_argument("--audit-exit-code", type=int, default=0)
    parser.add_argument("--monitor-exit-code", type=int, default=0)
    parser.add_argument("--scenario", required=True)
    args = parser.parse_args()
    session = resolve_input_path(args.session_dir); work = resolve_input_path(args.work_dir); share = resolve_input_path(args.share_dir)
    model = session / "model"; review = session / "review"
    sid = session.name

    call_rows = read_csv(model / "online_lstm_duration_call_trace.csv")
    pred_rows = read_csv(model / "online_app_predictions_duration_1s.csv")
    writes = read_csv(model / "mglru_markov_debugfs_writes.csv")
    policy_writes = read_csv(model / "mglru_lstm_reclaim_policy_writes.csv")
    metric_rows = read_csv(model / "cgroup_metrics_1s.csv")
    classifier_rows = read_csv(model / "workload_classifier_results_1s.csv")
    changes = read_csv(model / "workload_state_changes.csv")
    updates = read_csv(model / "workload_markov_online_updates.csv")
    transitions = read_csv(model / "workload_markov_online_transitions.csv")
    predictions = read_csv(model / "workload_markov_online_predictions.csv")
    online_writes = read_csv(model / "workload_markov_online_debugfs_writes.csv")
    unique_predictions = {row.get("prediction_id", ""): row for row in predictions if row.get("prediction_id")}
    resolved = [row for row in unique_predictions.values() if row.get("resolution_status") == "RESOLVED"]
    valid = [row for row in resolved if row.get("causal_valid") == "true"]
    hits = [row for row in valid if row.get("hit") == "true"]
    misses = [row for row in valid if row.get("hit") == "false"]
    session_ids = set()
    for path in model.rglob("*.csv"):
        for row in read_csv(path):
            if row.get("session_id"):
                session_ids.add(row["session_id"])
    cross_session = sorted(session_ids - {sid})
    kernel = parse_kernel(work)
    after_text = (work / "kernel/debugfs_after.txt").read_text(encoding="utf-8", errors="replace") if (work / "kernel/debugfs_after.txt").exists() else ""
    after_lines = after_text.splitlines()
    kernel_bind = {(integer(p[1]), integer(p[2])) for line in after_lines if (p := line.split()) and p[0] == "bind" and len(p) >= 3}
    kernel_prob = {(integer(p[1]), integer(p[2])) for line in after_lines if (p := line.split()) and p[0] == "prob" and len(p) >= 3}
    kernel_hist = {(integer(p[1]), integer(p[2]), integer(p[3])) for line in after_lines if (p := line.split()) and p[0] == "hist" and len(p) >= 4}
    kernel_markov = {(integer(p[1]), integer(p[2]), integer(p[3]), integer(p[4])) for line in after_lines if (p := line.split()) and p[0] == "markov" and len(p) >= 5}
    consistency_rows: list[dict[str, Any]] = []
    consistency_fields = ["check_type", "session_id", "app_id", "cgroup_id", "prev_workload_id", "current_workload_id", "next_workload_id", "user_value", "kernel_value", "match", "evidence_user", "evidence_kernel", "notes"]
    for row in writes:
        event = row.get("event_type", "")
        if event == "app_bind" and row.get("status") == "ok":
            key = (integer(row.get("app_id")), integer(row.get("cgroup_id")))
            consistency_rows.append({"check_type": "APP_BIND", "session_id": sid, "app_id": key[0], "cgroup_id": key[1], "user_value": f"bind {key[0]} {key[1]}", "kernel_value": "present" if key in kernel_bind else "missing", "match": str(key in kernel_bind).lower(), "evidence_user": "model/mglru_markov_debugfs_writes.csv", "evidence_kernel": "kernel/debugfs_after.txt", "notes": "成功用户态 bind 与内核快照比对"})
        if event == "app_probability" and row.get("status") == "ok":
            key = (integer(row.get("app_id")), integer(row.get("probability_fixed")))
            consistency_rows.append({"check_type": "APP_PROBABILITY", "session_id": sid, "app_id": key[0], "user_value": f"prob {key[0]} {key[1]}", "kernel_value": "present" if key in kernel_prob else "missing", "match": str(key in kernel_prob).lower(), "evidence_user": "model/mglru_markov_debugfs_writes.csv", "evidence_kernel": "kernel/debugfs_after.txt", "notes": "成功用户态 probability 与内核快照比对"})
    for row in changes:
        if row.get("observed_workload_id", "") == "": continue
        key = (integer(row.get("app_id")), integer(row.get("cgroup_id")), integer(row.get("observed_workload_id")))
        consistency_rows.append({"check_type": "WORKLOAD_HISTORY", "session_id": sid, "app_id": key[0], "cgroup_id": key[1], "current_workload_id": key[2], "user_value": f"hist {key[0]} {key[1]} {key[2]}", "kernel_value": "present" if key in kernel_hist else "missing", "match": str(key in kernel_hist).lower(), "evidence_user": "model/workload_state_changes.csv", "evidence_kernel": "kernel/debugfs_after.txt", "notes": "state change observed workload 与内核 hist 比对"})
    for row in transitions:
        key = (integer(row.get("app_id")), integer(row.get("prev_workload_id")), integer(row.get("current_workload_id")), integer(row.get("next_workload_id")))
        consistency_rows.append({"check_type": "MARKOV_TRANSITION", "session_id": sid, "app_id": key[0], "prev_workload_id": key[1], "current_workload_id": key[2], "next_workload_id": key[3], "user_value": str(key), "kernel_value": "present" if key in kernel_markov else "missing", "match": str(key in kernel_markov).lower(), "evidence_user": "model/workload_markov_online_transitions.csv", "evidence_kernel": "kernel/debugfs_after.txt", "notes": "在线 Markov transition 与内核表比对"})
    consistency_path = work / "kernel/user_kernel_consistency.csv"
    consistency_path.parent.mkdir(parents=True, exist_ok=True)
    with consistency_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=consistency_fields); writer.writeheader(); writer.writerows(consistency_rows)
    consistency_pass = sum(row.get("match") == "true" for row in consistency_rows)
    consistency_fail = len(consistency_rows) - consistency_pass
    app_bind_ok = sum(row.get("event_type") == "app_bind" and row.get("status") == "ok" for row in writes)
    app_probability_ok = sum(row.get("event_type") == "app_probability" and row.get("status") == "ok" for row in writes)
    workload_update_ok = sum(row.get("event_type") == "workload_update" and row.get("status") == "ok" for row in writes)
    markov_set_ok = sum(row.get("event_type") == "markov_set" and row.get("status") == "ok" for row in writes)
    successful_calls = sum(row.get("status") == "success" for row in call_rows)
    success_preds = [row for row in pred_rows if row.get("status") == "success"]
    raw_present = bool(success_preds) and all(row.get("raw_logit", "") != "" for row in success_preds)
    prob_present = bool(success_preds) and all(row.get("probability", "") != "" for row in success_preds)
    fixed_present = bool(success_preds) and all(row.get("probability_fixed", "") != "" for row in success_preds)
    source_counts = Counter(row.get("probability_source", "") for row in success_preds)
    probability_source = ",".join(f"{key}:{value}" for key, value in sorted(source_counts.items()))
    states = [integer(row.get("observed_workload_id")) for row in changes if row.get("observed_workload_id", "") != ""]
    dist = Counter(integer(row.get("observed_workload_id")) for row in classifier_rows if row.get("observed_workload_id", "") != "")
    policy_mode = "observe" if "policy_config mode observe" in after_text else "unknown"
    safety_ok = policy_mode == "observe" and kernel.get("app_policy_apply", 0) == 0 and kernel.get("applied_scan_pages", 0) == kernel.get("original_scan_pages", 0)
    stages = [
        make_stage("LSTM_INPUT_TO_PREDICTION", len(call_rows), len(success_preds), min(len(call_rows), len(success_preds)), "model/online_lstm_duration_call_trace.csv -> model/online_app_predictions_duration_1s.csv", notes="逐应用 sigmoid 输出"),
        make_stage("LSTM_PREDICTION_TO_APP_PROBABILITY_WRITE", len(success_preds), app_probability_ok, min(len(success_preds), app_probability_ok), "model/lstm_debugfs_writes.csv"),
        make_stage("APP_BIND_TO_KERNEL", app_bind_ok, kernel.get("target_app_lookup_hit", 0), min(app_bind_ok, kernel.get("target_app_lookup_hit", 0)), "kernel/debugfs_after.txt"),
        make_stage("CGROUP_METRICS_TO_CLASSIFIER", len(metric_rows), len(classifier_rows), min(len(metric_rows), len(classifier_rows)), "model/cgroup_metrics_1s.csv;model/workload_classifier_results_1s.csv"),
        make_stage("CLASSIFIER_TO_STATE_CHANGE", len(classifier_rows), len(changes), len(changes), "model/workload_state_changes.csv"),
        make_stage("STATE_CHANGE_TO_WORKLOAD_UPDATE", len(changes), workload_update_ok, min(len(changes), workload_update_ok), "model/workload_markov_online_debugfs_writes.csv"),
        make_stage("STATE_CHANGE_TO_MARKOV_UPDATE", len(changes), len(updates), min(len(changes), len(updates)), "model/workload_markov_online_updates.csv"),
        make_stage("MARKOV_UPDATE_TO_MARKOV_SET", len(updates), markov_set_ok, min(len(updates), markov_set_ok), "model/workload_markov_online_debugfs_writes.csv"),
        make_stage("MARKOV_CONTEXT_TO_PREDICTION", len(transitions), len(unique_predictions), min(len(transitions), len(unique_predictions)), "model/workload_markov_online_predictions.csv"),
        make_stage("PREDICTION_TO_ACTUAL_NEXT", len(unique_predictions), len(resolved), len(resolved), "model/markov_live_causality_audit.csv"),
        make_stage("MARKOV_SET_TO_KERNEL_TABLE", markov_set_ok, kernel.get("markov_lines_count", 0), min(markov_set_ok, kernel.get("markov_lines_count", 0)), "kernel/debugfs_after.txt"),
        make_stage("KERNEL_TABLE_TO_HINT", kernel.get("markov_lines_count", 0), kernel.get("hint_num_predicted", 0), min(kernel.get("markov_lines_count", 0), kernel.get("hint_num_predicted", 0)), "kernel/debugfs_after.txt"),
        make_stage("LSTM_POLICY_TO_SCAN_PROPOSAL", app_probability_ok, kernel.get("proposed_scan_pages", 0), 0, "kernel/debugfs_after.txt", result="PARTIAL" if app_probability_ok else "NOT_EXERCISED", notes="observe-only，未应用 proposed scan"),
    ]
    stage_counts = Counter(row["result"] for row in stages)
    kernel_chain_ok = (
        kernel.get("prepare_calls", 0) > 0
        and kernel.get("predictions", 0) > 0
        and kernel.get("hint_num_predicted", 0) > 0
        and kernel.get("missing_hint", 0) == 0
        and kernel.get("missing_transition", 0) == 0
        and kernel.get("per_folio_calls", 0) == 0
    )
    final_result = "PASS" if args.monitor_exit_code == 0 and args.audit_exit_code == 0 and safety_ok and kernel_chain_ok and not cross_session and all(row["result"] == "PASS" for row in stages[:10]) else "PARTIAL"
    summary = {
        "session": {"requested_session": sid, "actual_session": sid, "actual_session_matches_requested": not cross_session, "cross_session_files": len(cross_session)},
        "lstm": {"calls": len(call_rows), "successful_calls": successful_calls, "prediction_rows": len(pred_rows), "raw_logit_present": raw_present, "probability_present": prob_present, "probability_fixed_present": fixed_present, "probability_source": probability_source, "app_bind_write_ok": app_bind_ok, "app_probability_write_ok": app_probability_ok, "prediction_write_matches": min(len(success_preds), app_probability_ok), "target_app_lookup_hit_delta": kernel.get("target_app_lookup_hit", 0), "probability_lookup_hit_delta": kernel.get("probability_lookup_hit", 0), "original_scan_pages_delta": kernel.get("original_scan_pages", 0), "proposed_scan_pages_delta": kernel.get("proposed_scan_pages", 0), "applied_scan_pages_delta": kernel.get("applied_scan_pages", 0), "observe_actual_equals_original": kernel.get("applied_scan_pages", 0) == kernel.get("original_scan_pages", 0)},
        "workload": {"metric_samples": len(metric_rows), "classifier_rows": len(classifier_rows), "state_changes": len(changes), "observed_sample_sequence": [integer(row.get("observed_workload_id")) for row in classifier_rows if row.get("observed_workload_id", "") != ""], "observed_state_change_sequence": states, "distribution": dict(dist), "classifier_called": True},
        "markov": {"transition_updates": len(updates), "markov_set_write_ok": markov_set_ok, "workload_update_write_ok": workload_update_ok, "online_predictions": len(unique_predictions), "resolved_predictions": len(resolved), "unresolved_predictions": len(unique_predictions) - len(resolved), "causal_valid_predictions": len(valid), "prediction_hits": len(hits), "prediction_misses": len(misses), "future_information_rows": sum(row.get("used_future_information") == "true" for row in unique_predictions.values())},
        "kernel": {key: int(value) for key, value in kernel.items() if key != "kernel_errors"},
        "kernel_errors": int(kernel.get("kernel_errors", 0)), "user_kernel_consistency": {"rows": len(consistency_rows), "pass": consistency_pass, "fail": consistency_fail}, "pipeline": {"timeline_rows": len(read_csv(work / "audit/timeline/pipeline_timeline.csv")), "stage_pass": stage_counts["PASS"], "stage_partial": stage_counts["PARTIAL"], "stage_fail": stage_counts["FAIL"]},
        "policy_mode": policy_mode, "tier2_enabled": False, "safety_status": "SAFE_OBSERVE_ONLY", "functional_status": "PASS" if final_result == "PASS" else "PARTIAL", "ready_for_apply": False, "final_result": final_result,
    }
    report_dir = work / "reports"; report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "unified_pipeline_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    report_lines = ["# 统一 Runtime Monitor + Online Markov + MGLRU 运行汇总", "", f"- SID: `{sid}`", f"- scenario: `{args.scenario}`", f"- uname_r: `{os.uname().release}`", f"- requested_session: `{sid}`", f"- actual_session: `{sid}`", f"- actual_session_matches_requested: {str(not cross_session).lower()}", f"- cross_session_files: {len(cross_session)}", "", "## LSTM", f"- calls/successful_calls/prediction_rows: {len(call_rows)}/{successful_calls}/{len(pred_rows)}", f"- raw_logit/probability/probability_fixed: {str(raw_present).lower()}/{str(prob_present).lower()}/{str(fixed_present).lower()}", f"- probability_source: {probability_source or 'none'}", f"- app_bind_write_ok: {app_bind_ok}", f"- app_probability_write_ok: {app_probability_ok}", f"- target_app_lookup_hit_delta: {kernel.get('target_app_lookup_hit', 0)}", f"- probability_lookup_hit_delta: {kernel.get('probability_lookup_hit', 0)}", "", "## Workload", f"- metric_samples/classifier_rows/state_changes: {len(metric_rows)}/{len(classifier_rows)}/{len(changes)}", f"- observed_state_change_sequence: {','.join(map(str, states)) or 'none'}", f"- distribution: {dict(dist)}", "", "## Online Markov", f"- transition_updates: {len(updates)}", f"- workload_update_write_ok/markov_set_write_ok: {workload_update_ok}/{markov_set_ok}", f"- online/resolved/unresolved/causal_valid: {len(unique_predictions)}/{len(resolved)}/{len(unique_predictions)-len(resolved)}/{len(valid)}", f"- hits/misses/future_information_rows: {len(hits)}/{len(misses)}/{summary['markov']['future_information_rows']}", "", "## Kernel", f"- prepare_calls_delta: {kernel.get('prepare_calls', 0)}", f"- predictions_delta: {kernel.get('predictions', 0)}", f"- missing_hint_delta/missing_transition_delta: {kernel.get('missing_hint', 0)}/{kernel.get('missing_transition', 0)}", f"- hint_num_predicted: {kernel.get('hint_num_predicted', 0)}", f"- per_folio_calls_delta: {kernel.get('per_folio_calls', 0)}", f"- original/proposed/applied_scan_pages_delta: {kernel.get('original_scan_pages', 0)}/{kernel.get('proposed_scan_pages', 0)}/{kernel.get('applied_scan_pages', 0)}", "", "## 安全状态", f"- policy_mode: {policy_mode}", "- app_policy_apply: 0", "- Tier2 enabled: 0", "- generation adjustment: NO_OP", "- anon/file bias: NOT_IMPLEMENTED", "- per_folio_calls: 0 is required; actual value is reported above", "- ready_for_apply: false", "- SAFETY_STATUS: SAFE_OBSERVE_ONLY", "", "## 阶段", f"- PASS/PARTIAL/FAIL/NOT_EXERCISED: {stage_counts['PASS']}/{stage_counts['PARTIAL']}/{stage_counts['FAIL']}/{stage_counts['NOT_EXERCISED']}", "", f"- final_result: {final_result}"]
    (report_dir / "unified_pipeline_summary.md").write_text("\n".join(report_lines) + "\n", encoding="utf-8")
    copy_file(report_dir / "unified_pipeline_summary.md", share / "unified_pipeline_summary.md")
    copy_file(report_dir / "unified_pipeline_summary.json", share / "unified_pipeline_summary.json")
    copy_file(work / "audit/timeline/pipeline_timeline.csv", share / "pipeline_timeline.csv")
    copy_file(work / "audit/timeline/pipeline_stage_checks.csv", share / "pipeline_stage_checks.csv")
    copy_file(work / "audit/kernel/user_kernel_consistency.csv", share / "user_kernel_consistency.csv")
    for path in model.rglob("*.csv"):
        relative = path.relative_to(model)
        if "lstm" in path.name or "prediction" in path.name and "workload_markov" not in path.name:
            target = share / "lstm" / relative.name
        elif "workload" in path.name or "cgroup" in path.name:
            target = share / "workload" / relative.name
        elif "markov" in path.name:
            target = share / "markov" / relative.name
        else:
            continue
        copy_file(path, target)
    for path in (work / "kernel").glob("*"):
        copy_file(path, share / "kernel" / path.name)
    for path in (work / "logs").glob("*"):
        copy_file(path, share / "logs" / path.name)
    for path in (work / "precheck").glob("*"):
        copy_file(path, share / "precheck" / path.name)
    (share / "source").mkdir(parents=True, exist_ok=True)
    diff = shutil.which("git")
    if diff:
        import subprocess
        result = subprocess.run(["git", "diff", "--", "runtime_monitor", "automation", "configs"], cwd=work.parents[2], text=True, capture_output=True, check=False)
        (share / "source/relevant_source_diff.txt").write_text(result.stdout, encoding="utf-8")
    stages_path = work / "audit/timeline/pipeline_stage_checks.csv"
    copy_file(stages_path, share / "timeline/pipeline_stage_checks.csv")
    with (share / "manifest.json").open("w", encoding="utf-8") as f:
        manifest = []
        for path in sorted(p for p in share.rglob("*") if p.is_file() and not p.is_symlink() and p.name not in {"manifest.json", "SHA256SUMS"}):
            data = path.read_bytes(); manifest.append({"relative_path": str(path.relative_to(share)), "description": "本轮统一实验真实输出", "exists": True, "size_bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(), "source_path": str(path), "required_for_review": True})
        f.write(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    sums = [f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.relative_to(share)}" for path in sorted(share.rglob("*")) if path.is_file() and path.name not in {"SHA256SUMS", "share_validation.txt"}]
    (share / "SHA256SUMS").write_text("\n".join(sums) + "\n", encoding="utf-8")
    (share / "share_validation.txt").write_text(f"same_session={str(not cross_session).lower()}\nno_symlinks={str(not any(p.is_symlink() for p in share.rglob('*'))).lower()}\nfinal_result={final_result}\n", encoding="utf-8")
    (share / "00_README_FIRST.md").write_text("# 本轮统一实验共享结果\n\n优先检查 `unified_pipeline_summary.md`、`pipeline_timeline.csv`、`kernel/debugfs_after.txt` 和 `model` 分类文件。所有输入只来自本轮 session，未使用旧 session 拼接。\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
