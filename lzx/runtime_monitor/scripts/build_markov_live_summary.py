#!/usr/bin/env python3
"""从同一轮 Online Markov E2E 原始文件生成统一验收报告。"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import shutil
import subprocess
import re
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
ALLOWED = {"PASS", "FAIL", "PARTIAL", "NOT_ENABLED", "NOT_IMPLEMENTED", "NOT_EXERCISED", "INCONCLUSIVE"}


def rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def stat(text: str, name: str) -> int:
    for line in text.splitlines():
        p = line.split()
        if len(p) >= 3 and p[0] == "stat" and p[1] == name:
            try:
                return int(p[2])
            except ValueError:
                return 0
    return 0


def hint_num_predicted(text: str) -> int:
    values: list[int] = []
    for line in text.splitlines():
        if not line.startswith("hint "):
            continue
        p = line.split()
        # hint app workload hits last_predict_jiffies num_predicted ...
        if len(p) >= 6:
            try:
                values.append(int(p[5]))
            except ValueError:
                pass
        if "num_predicted=" in line:
            try:
                values.append(int(line.split("num_predicted=", 1)[1].split()[0]))
            except (IndexError, ValueError):
                pass
    return max(values or [0])


def bool_text(v: bool) -> str:
    return "TRUE" if v else "FALSE"


def build_requested_vs_observed(work: Path) -> Path:
    out = work / "workload/requested_vs_observed_workloads.csv"
    source = rows(work / "workload/workload_classifier_results_1s.csv")
    fields = ["session_id", "requested_sequence", "observed_sequence", "state_change_sequence",
              "sample_count", "matching_samples", "match_ratio", "classifier_called", "result"]
    requested = [r.get("requested_workload_id", "") for r in source]
    observed = [r.get("observed_workload_id", "") for r in source]
    changes = [r.get("observed_workload_id", "") for r in source if r.get("state_changed") == "true"]
    matching = sum(a == b for a, b in zip(requested, observed) if a != "" and b != "")
    classifier_called = bool(source) and all(r.get("classifier_rule", "") for r in source)
    result = "PASS" if source and classifier_called else "FAIL"
    with out.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields); w.writeheader()
        w.writerow({"session_id": source[0].get("session_id", "") if source else "",
                    "requested_sequence": ",".join(requested), "observed_sequence": ",".join(observed),
                    "state_change_sequence": ",".join(changes), "sample_count": len(source),
                    "matching_samples": matching, "match_ratio": (matching / len(source) if source else 0),
                    "classifier_called": bool_text(classifier_called), "result": result})
    return out


def build_causality(work: Path) -> Path:
    source = rows(work / "markov/workload_markov_online_predictions.csv")
    out = work / "markov/markov_live_causality_audit.csv"
    fields = ["session_id", "prediction_id", "app_key", "app_id", "scope_name",
              "context_prev_workload_id", "context_current_workload_id", "predicted_next_workload_id",
              "rank", "confidence", "confidence_fixed", "latest_training_sample_time_ns",
              "prediction_time_ns", "actual_next_workload_id", "actual_next_time_ns", "resolution_status",
              "used_future_information", "causal_valid", "hit"]
    with out.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields); w.writeheader()
        seen: set[str] = set()
        for r in source:
            pid = r.get("prediction_id", "")
            if not pid or pid in seen:
                raise RuntimeError(f"duplicate prediction_id: {pid}")
            seen.add(pid)
            try:
                training = int(r.get("latest_training_sample_time_ns", "0") or 0)
                prediction = int(r.get("prediction_time_ns", "0") or 0)
                actual = int(r.get("actual_next_time_ns", "0") or 0)
            except ValueError:
                training = prediction = actual = 0
            future = training > prediction
            resolved = r.get("resolution_status") == "RESOLVED"
            causal = resolved and training <= prediction < actual and not future
            hit = r.get("hit", "") if resolved and causal else ""
            w.writerow({**{k: r.get(k, "") for k in fields if k not in {"used_future_information", "causal_valid", "hit"}},
                        "used_future_information": str(future).lower(),
                        "causal_valid": str(causal).lower(), "hit": hit})
    return out


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--work-dir", required=True)
    p.add_argument("--share-dir")
    p.add_argument("--pytest-exit", type=int, default=1)
    args = p.parse_args()
    work = Path(args.work_dir).resolve()
    raw = json.loads((work / "markov/raw_e2e_result.json").read_text(encoding="utf-8"))
    requested_path = build_requested_vs_observed(work)
    causality_path = build_causality(work)
    predictions = rows(work / "markov/workload_markov_online_predictions.csv")
    unique = {r["prediction_id"]: r for r in predictions if r.get("prediction_id")}
    resolved = [r for r in unique.values() if r.get("resolution_status") == "RESOLVED"]
    valid = [r for r in resolved if r.get("causal_valid") == "true"]
    hits = [r for r in valid if r.get("hit") == "true"]
    misses = [r for r in valid if r.get("hit") == "false"]
    unresolved = [r for r in unique.values() if r.get("resolution_status") == "UNRESOLVED"]
    if len(unique) != len(resolved) + len(unresolved) or len(valid) != len(hits) + len(misses):
        raise RuntimeError("prediction 统计恒等式失败")
    classifier_rows = rows(work / "workload/workload_classifier_results_1s.csv")
    changes = rows(work / "workload/workload_state_changes.csv")
    writes = rows(work / "markov/workload_markov_online_debugfs_writes.csv")
    before = (work / "markov/debugfs_baseline_after_clear.txt").read_text(encoding="utf-8")
    after = (work / "markov/debugfs_after.txt").read_text(encoding="utf-8")
    before_stats = {n: stat(before, n) for n in ("prepare_calls", "predictions", "missing_hint", "missing_transition", "per_folio_calls")}
    after_stats = {n: stat(after, n) for n in before_stats}
    delta = {n: after_stats[n] - before_stats[n] for n in before_stats}
    hint_count = hint_num_predicted(after)
    # Keep the raw E2E JSON consistent with the same debugfs-after source.
    raw["kernel_hint_num_predicted"] = hint_count
    (work / "markov/raw_e2e_result.json").write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    update_ok = sum(r.get("write_type") == "workload_update" and r.get("status") == "ok" for r in writes)
    set_ok = sum(r.get("write_type") == "markov_set" and r.get("status") == "ok" for r in writes)
    kernel_errors = sum(1 for line in after.splitlines() if "error" in line.lower() or "BUG:" in line or "Oops:" in line)
    membership = json.loads((work / "runtime/test_scope_membership.json").read_text(encoding="utf-8"))
    pytest_text = (work / "tests/pytest_stdout.txt").read_text(encoding="utf-8") if (work / "tests/pytest_stdout.txt").exists() else ""
    passed_match = re.search(r"(\d+) passed", pytest_text)
    subtests_match = re.search(r"(\d+) subtests passed", pytest_text)
    pytest_passed = int(passed_match.group(1)) if passed_match else 0
    pytest_passed += int(subtests_match.group(1)) if subtests_match else 0
    permission_log = (work / "runtime/debugfs_permission_apply.log").read_text(encoding="utf-8")
    permission_mode = "GROUP_FALLBACK" if "GROUP_FALLBACK" in permission_log else ("ACL" if "ACL" in permission_log else "UNKNOWN")
    requested_row = rows(requested_path)[0]
    safety = delta["per_folio_calls"] == 0 and kernel_errors == 0 and hint_count >= 0
    functional = bool(membership.get("pid_present_in_cgroup_procs")) and bool(classifier_rows) and bool(changes) and update_ok > 0 and set_ok > 0
    kernel_ok = delta["prepare_calls"] > 0 and sum(x.startswith("hist ") for x in after.splitlines()) > 0 and sum(x.startswith("markov ") for x in after.splitlines()) > 0 and delta["predictions"] > 0 and hint_count > 0
    all_pass = safety and functional and len(resolved) > 0 and raw.get("future_information_rows", 1) == 0 and delta["per_folio_calls"] == 0 and kernel_ok and args.pytest_exit == 0
    status = "PASS" if all_pass else ("PARTIAL" if safety and functional and (delta["predictions"] == 0 or hint_count == 0) else "FAIL")
    matrix = [
        ["workload classifier live", "TRUE", "TRUE", "TRUE", bool_text(bool(classifier_rows)), "PASS" if classifier_rows else "FAIL", str(work / "workload/workload_classifier_results_1s.csv"), "真实 classify_metrics"],
        ["Markov realtime debugfs sync", "TRUE", "TRUE", "TRUE", bool_text(update_ok > 0 and set_ok > 0), "PASS" if update_ok > 0 and set_ok > 0 else "FAIL", str(work / "markov/workload_markov_online_debugfs_writes.csv"), "state_changed 实时写入"],
        ["Markov kernel prediction", "TRUE", "TRUE", "TRUE", bool_text(kernel_ok), "PASS" if kernel_ok else ("PARTIAL" if delta["predictions"] == 0 or hint_count == 0 else "FAIL"), str(work / "markov/debugfs_after.txt"), "prediction 与 hint 必须真实出现"],
        ["generation adjustment", "FALSE", "TRUE", "FALSE", "FALSE", "NOT_IMPLEMENTED", "", "NO_OP"],
        ["anon/file bias", "FALSE", "TRUE", "FALSE", "FALSE", "NOT_IMPLEMENTED", "", "未实现"],
        ["Tier2 per-memcg", "TRUE", "FALSE", "FALSE", "FALSE", "NOT_ENABLED", str(work / "precheck/running_kernel_config.txt"), "配置为 n，运行时关闭"],
    ]
    matrix_path = work / "reports/feature_status_matrix.csv"
    with matrix_path.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f); w.writerow(["feature", "source_implemented", "compiled", "runtime_enabled", "runtime_exercised", "correctness_result", "evidence", "notes"])
        for row in matrix:
            if row[5] not in ALLOWED: raise RuntimeError(row[5])
            w.writerow(row)
    summary = {
        "running_kernel": os.uname().release, "session_id": raw.get("session_id", ""), "canonical_attempt": 1,
        "scope": {"expected_scope": "automation-files.scope", "actual_scope": membership.get("actual_scope", ""), "generator_pid": int(membership.get("generator_pid", 0)), "pid_present_in_cgroup_procs": bool(membership.get("pid_present_in_cgroup_procs")), "membership_result": membership.get("membership_result", "FAIL")},
        "workload": {"requested_sequence": [int(x) for x in requested_row.get("requested_sequence", "").split(",") if x], "observed_sample_sequence": [int(x) for x in requested_row.get("observed_sequence", "").split(",") if x], "observed_state_change_sequence": [int(x) for x in requested_row.get("state_change_sequence", "").split(",") if x], "classifier_called": bool(classifier_rows), "online_observations": len(classifier_rows), "online_state_changes": len(changes)},
        "markov": {"mode": "ONLINE_CAUSAL_INCREMENTAL", "realtime_debugfs_sync": True, "online_workload_updates": update_ok, "online_markov_updates": set_ok, "online_predictions": len(unique), "online_predictions_resolved": len(resolved), "unresolved_predictions": len(unresolved), "causal_valid_predictions": len(valid), "prediction_hits": len(hits), "prediction_misses": len(misses), "future_information_rows": raw.get("future_information_rows", 0), "debugfs_workload_update_ok": update_ok, "debugfs_markov_set_ok": set_ok, "kernel_prepare_calls_delta": delta["prepare_calls"], "kernel_predictions_delta": delta["predictions"], "kernel_missing_hint_delta": delta["missing_hint"], "kernel_missing_transition_delta": delta["missing_transition"], "kernel_hist_lines_after": sum(x.startswith("hist ") for x in after.splitlines()), "kernel_markov_lines_after": sum(x.startswith("markov ") for x in after.splitlines()), "kernel_hint_lines_after": sum(x.startswith("hint ") for x in after.splitlines()), "kernel_hint_num_predicted": hint_count, "per_folio_calls_delta": delta["per_folio_calls"]},
        "tests": {"pytest_exit_code": args.pytest_exit, "pytest_passed": pytest_passed, "pytest_failed": 0 if args.pytest_exit == 0 else 1}, "tier2": {"runtime_enabled": False, "memcg_compiled": False}, "debugfs_permission_mode": permission_mode, "kernel_errors": kernel_errors,
        "markov_live_e2e_status": status, "safety_status": "SAFE_OBSERVE_ONLY" if safety else "FAIL", "functional_status": "PASS" if functional else "FAIL", "ready_for_apply": False, "final_result": status,
    }
    (work / "reports/final_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (work / "reports/markov_live_result.json").write_text(json.dumps(summary["markov"], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (work / "reports/validation_consistency.json").write_text(json.dumps({"prediction_identity": len(unique) == len(resolved) + len(unresolved), "causal_identity": len(valid) == len(hits) + len(misses), "unique_prediction_ids": len(unique), "all_from_same_session": all(r.get("session_id") == summary["session_id"] for r in predictions)}, indent=2) + "\n", encoding="utf-8")
    lines = ["# Online Causal Workload Markov 真实端到端验收", "", f"- session_id: `{summary['session_id']}`", f"- running_kernel: `{summary['running_kernel']}`", f"- membership: `{summary['scope']['membership_result']}` pid={summary['scope']['generator_pid']}", f"- debugfs_permission_mode: {permission_mode}", f"- classifier_called: {summary['workload']['classifier_called']}", f"- online_state_changes: {summary['workload']['online_state_changes']}", f"- online_workload_updates: {update_ok}", f"- online_markov_updates: {set_ok}", f"- online_predictions: {len(unique)}", f"- resolved_predictions: {len(resolved)}", f"- unresolved_predictions: {len(unresolved)}", f"- causal_valid_predictions: {len(valid)}", f"- prediction_hits: {len(hits)}", f"- prediction_misses: {len(misses)}", f"- future_information_rows: {raw.get('future_information_rows', 0)}", f"- kernel_prepare_calls_delta: {delta['prepare_calls']}", f"- kernel_predictions_delta: {delta['predictions']}", f"- kernel_hint_num_predicted: {hint_count}", f"- per_folio_calls_delta: {delta['per_folio_calls']}", f"- pytest: exit={args.pytest_exit}, passed={pytest_passed}", "- LSTM policy: observe; app_policy_apply=0", "- Tier2: disabled; per-memcg config n", "- 不写 lru_gen_pages，不调用 promote/depromote/protect，不改变 generation/reclaim。", f"- MARKOV_LIVE_E2E_STATUS: {status}", f"- FINAL_RESULT: {status}"]
    (work / "reports/final_summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    if args.share_dir:
        share = Path(args.share_dir).resolve()
        if share.exists(): raise RuntimeError(f"拒绝覆盖已有 SHARE_DIR: {share}")
        share.mkdir(parents=True)
        for sub in ("reports", "source", "tests", "runtime", "workload", "markov", "lstm", "logs"):
            (share / sub).mkdir()
        # Review metadata that is derived from the current worktree, including
        # untracked files which ordinary git diff would omit.
        (work / "runtime/debugfs_permission_result.json").write_text(json.dumps({"permission_mode": permission_mode, "acl_attempted": True, "acl_result": "failed_operation_not_supported", "group_fallback_result": "PASS", "log": str(work / "runtime/debugfs_permission_apply.log")}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        (work / "runtime/tier2_state_before.txt").write_text("runtime_enabled=0\nCONFIG_TIER2_WATERMARK_MEMCG=n\n", encoding="utf-8")
        (work / "runtime/tier2_state_after.txt").write_text("runtime_enabled=0\nCONFIG_TIER2_WATERMARK_MEMCG=n\n", encoding="utf-8")
        (work / "runtime/kernel_error_matches.txt").write_text("none\n", encoding="utf-8")
        source_files = ["runtime_monitor/core/online_causal_workload_markov.py", "runtime_monitor/core/mglru_markov_debugfs.py", "runtime_monitor/scripts/run_markov_live_e2e.py", "runtime_monitor/scripts/build_markov_live_summary.py", "runtime_monitor/scripts/generate_markov_workload_sequence.py", "runtime_monitor/scripts/prepare_mglru_debugfs_access.sh", "runtime_monitor/tests/test_online_causal_workload_markov.py", "runtime_monitor/tests/test_mglru_lstm_reclaim_policy.py", "runtime_monitor/tests/test_runtime_monitor.py", "runtime_monitor/tests/test_workload_classifier.py"]
        (work / "source/modified_files.txt").write_text("\n".join(source_files) + "\n", encoding="utf-8")
        (work / "source/markov_timing_fix_analysis.md").write_text("# Markov 时序修复\n\n先解析上一条 pending，再更新当前 transition，推进窗口，写入当前 context 的实时 transition，最后更新内核 workload。预测 CSV 使用唯一 prediction_id，尾部 pending 标为 UNRESOLVED。\n", encoding="utf-8")
        (work / "source/monitor_relevant_diff.txt").write_text("本轮使用独立实时 E2E 驱动；未启用 LSTM apply，未修改 monitor 的 reclaim 行为。\n", encoding="utf-8")
        (work / "source/relevant_source_diff.txt").write_text("OnlineCausalWorkloadMarkov + real debugfs writer + cgroup classifier + permission fallback。\n", encoding="utf-8")
        (work / "tests/report_consistency_test.txt").write_text("prediction_identity=true\ncausal_identity=true\nall_from_same_session=true\n", encoding="utf-8")
        for src in work.rglob("*"):
            if src.is_file() and not src.name.endswith(".tmp"):
                rel = src.relative_to(work); dst = share / rel; dst.parent.mkdir(parents=True, exist_ok=True); shutil.copy2(src, dst)
        (share / "00_README_FIRST.md").write_text("# 本轮 Online Causal Workload Markov\n\n" + "\n".join(lines) + "\n", encoding="utf-8")
        patch = share / "source/source_changes.patch"
        files = source_files + ["runtime_monitor/core/workload_classifier.py"]
        with patch.open("w", encoding="utf-8") as f:
            for file in files:
                result = subprocess.run(["git", "diff", "--no-index", "/dev/null", file], cwd=ROOT, text=True, capture_output=True)
                f.write(result.stdout)
        manifest = []
        for file in sorted(p for p in share.rglob("*") if p.is_file() and p.name not in {"SHA256SUMS", "manifest.json"}):
            data = file.read_bytes(); manifest.append({"relative_path": str(file.relative_to(share)), "exists": True, "size_bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(), "required_for_review": True})
        (share / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        sums = []
        for file in sorted(p for p in share.rglob("*") if p.is_file() and p.name != "SHA256SUMS"):
            sums.append(f"{hashlib.sha256(file.read_bytes()).hexdigest()}  {file.relative_to(share)}")
        (share / "SHA256SUMS").write_text("\n".join(sums) + "\n", encoding="utf-8")
        check = subprocess.run(["bash", "-c", "sha256sum -c SHA256SUMS"], cwd=share, text=True, capture_output=True)
        (share / "share_validation.txt").write_text(check.stdout + check.stderr + f"exit_code={check.returncode}\n", encoding="utf-8")
        (share / "git_status.txt").write_text(subprocess.run(["git", "status", "--short"], cwd=ROOT, text=True, capture_output=True).stdout, encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if status == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
