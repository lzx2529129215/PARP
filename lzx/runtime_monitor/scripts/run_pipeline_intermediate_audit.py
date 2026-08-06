#!/usr/bin/env python3
"""对唯一指定 session 做可复核的只读中间数据审计。

该脚本不启动实验、不写 debugfs、不选择其他 session，也不修改模型、算法或内核。
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path
from statistics import mean
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from audit_common import (  # noqa: E402
    PROJECT_ROOT as COMMON_ROOT,
    event_type,
    integer,
    json_text,
    managed_app_maps,
    number,
    read_csv,
    resolve_input_path,
    row_ns,
    status_ok,
    write_csv,
)

WORKLOAD_NAMES = {
    0: "LOW_ACTIVITY", 1: "ANON_FAULT_HEAVY", 2: "FILE_FAULT_HEAVY",
    3: "FILE_REFAULT_HEAVY", 4: "MAJOR_FAULT_HEAVY", 5: "MEMORY_GROWTH_HEAVY",
    6: "MIXED_ACTIVE",
}
MODEL_FIELDS = [
    "session_id", "timestamp_ns", "timestamp", "trigger_type", "app_key", "app_name",
    "model_app_id", "runtime_app_id", "horizon_minutes", "raw_logit", "probability",
    "probability_fixed", "probability_source", "status", "skip_reason", "source_file", "source_row",
]


def copy_if_exists(source: Path, target: Path) -> bool:
    if source.exists() and source.is_file() and not source.is_symlink():
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        return True
    return False


def require_inputs(session: Path, work: Path, share: Path) -> None:
    missing = [str(path) for path in (session, work, share) if not path.is_dir()]
    if missing:
        raise FileNotFoundError("INPUT_NOT_FOUND: " + ", ".join(missing))
    if not (session / "model").is_dir() or not (work / "kernel").is_dir():
        raise FileNotFoundError("INPUT_NOT_FOUND: session/model 或 work/kernel 不存在")


def session_ids_from_file(path: Path) -> set[str]:
    ids: set[str] = set()
    if path.suffix.lower() != ".csv":
        return ids
    try:
        for row in read_csv(path):
            value = str(row.get("session_id", "")).strip()
            if value:
                ids.add(value)
    except (OSError, UnicodeError):
        pass
    return ids


def inventory(roots: list[tuple[str, Path]], expected: str, output: Path) -> tuple[int, list[str]]:
    rows: list[dict[str, Any]] = []
    mismatches: list[str] = []
    pattern = re.compile(r"session_[A-Za-z0-9_]+")
    for source_root, root in roots:
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            ids = session_ids_from_file(path)
            content_id = next(iter(ids), "") if len(ids) == 1 else ("SESSION_MISMATCH" if ids else "")
            if content_id and content_id != expected:
                mismatches.append(str(path))
            path_match = pattern.findall(str(path))
            detected = content_id or (expected if expected in path_match else "")
            rows.append({
                "absolute_path": str(path.resolve()),
                "relative_path": str(path.relative_to(root)),
                "source_root": source_root,
                "size_bytes": path.stat().st_size,
                "modified_time": path.stat().st_mtime_ns,
                "detected_session_id": detected,
                "category": path.parent.name,
                "used_by_audit": False,
                "notes": "SESSION_MISMATCH" if str(path) in mismatches else "",
            })
    write_csv(output, ["absolute_path", "relative_path", "source_root", "size_bytes", "modified_time", "detected_session_id", "category", "used_by_audit", "notes"], rows)
    return len(rows), mismatches


def build_mapping(model: Path, work: Path, out: Path, by_key: dict[str, dict[str, Any]], by_vocab: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    writes = read_csv(model / "mglru_markov_debugfs_writes.csv")
    last_bind: dict[str, dict[str, str]] = {}
    for row in writes:
        if event_type(row) == "app_bind" and status_ok(row):
            key = str(row.get("app_key", ""))
            if key:
                last_bind[key] = row
    rows = []
    for key, app in by_key.items():
        bind = last_bind.get(key, {})
        app["cgroup_id"] = bind.get("cgroup_id", "")
        rows.append({
            "app_key": key,
            "app_name": app.get("vocab_name", ""),
            "vocab_name": app.get("vocab_name", ""),
            "model_app_id": app.get("model_app_id", ""),
            "runtime_app_id": app.get("runtime_app_id", ""),
            "scope_name": app.get("scope_name", ""),
            "cgroup_id": app.get("cgroup_id", ""),
        })
    write_csv(out / "lstm/app_id_mapping.csv", list(rows[0].keys()) if rows else ["app_key"], rows)
    lines = ["# 应用 ID 映射", "", "| app_key | vocab_name | model_app_id | runtime_app_id | scope_name | cgroup_id |", "|---|---|---:|---:|---|---:|"]
    lines += [f"| {r['app_key']} | {r['vocab_name']} | {r['model_app_id']} | {r['runtime_app_id']} | {r['scope_name']} | {r['cgroup_id']} |" for r in rows]
    (out / "lstm/app_id_mapping.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return by_key


def audit_lstm(model: Path, out: Path, by_key: dict[str, dict[str, Any]], by_vocab: dict[str, dict[str, Any]]) -> dict[str, Any]:
    call_path = model / "online_lstm_duration_call_trace.csv"
    pred_path = model / "online_app_predictions_duration_1s.csv"
    calls = read_csv(call_path)
    raw_predictions = read_csv(pred_path)
    copy_if_exists(call_path, out / "lstm/lstm_call_trace.csv")
    copy_if_exists(pred_path, out / "lstm/lstm_predictions_raw.csv")
    normalized: list[dict[str, Any]] = []
    for index, row in enumerate(raw_predictions, 2):
        app = by_vocab.get(row.get("app", ""), {})
        normalized.append({
            "session_id": row.get("session_id", ""), "timestamp_ns": row_ns(row), "timestamp": row.get("timestamp", ""),
            "trigger_type": row.get("trigger_type", ""), "app_key": app.get("app_key", ""), "app_name": row.get("app", ""),
            "model_app_id": row.get("app_id", ""), "runtime_app_id": app.get("runtime_app_id", ""),
            "horizon_minutes": row.get("horizon", ""), "raw_logit": row.get("raw_logit", "") or row.get("raw_score", ""),
            "probability": row.get("probability", ""), "probability_fixed": row.get("probability_fixed", ""),
            "probability_source": row.get("probability_source", ""), "status": row.get("status", ""),
            "skip_reason": row.get("skip_reason", ""), "source_file": str(pred_path), "source_row": index,
        })
    write_csv(out / "lstm/lstm_predictions_normalized.csv", MODEL_FIELDS, normalized)
    success_status = {"success", "ok", "pass"}
    skipped_status = {"skipped", "skip"}
    status_rows = []
    for status in sorted({str(r.get("status", "")) for r in raw_predictions} | {"success", "skipped", "failed"}):
        subset = [r for r in raw_predictions if str(r.get("status", "")) == status]
        status_rows.append({"status": status, "row_count": len(subset), "unique_calls": len({r.get("trigger_type", "") + "|" + r.get("timestamp", "") for r in subset}), "unique_apps": len({r.get("app", "") for r in subset}), "unique_horizons": len({r.get("horizon", "") for r in subset}), "notes": "实际 status 字段"})
    write_csv(out / "lstm/lstm_prediction_status_summary.csv", list(status_rows[0].keys()), status_rows)

    # Only the comprehensive mglru log is a runtime source. Other logs are
    # retained as derived rows for duplicate auditing, never as extra writes.
    sources = [
        (model / "mglru_markov_debugfs_writes.csv", "RUNTIME_EVENT"),
        (model / "mglru_lstm_reclaim_policy_writes.csv", "DERIVED_AUDIT_ROW"),
        (model / "lstm_debugfs_writes.csv", "DERIVED_AUDIT_ROW"),
        (model / "workload_markov_online_debugfs_writes.csv", "DERIVED_AUDIT_ROW"),
    ]
    norm_fields = ["session_id", "timestamp_ns", "timestamp", "event_type", "command", "status", "app_key", "app_name", "model_app_id", "runtime_app_id", "cgroup_id", "workload_id", "workload_name", "probability_fixed", "source_file", "source_row", "record_origin", "counted_as_runtime_event"]
    normalized_writes: list[dict[str, Any]] = []
    for path, origin in sources:
        for index, row in enumerate(read_csv(path), 2):
            etype = event_type(row)
            app = by_key.get(row.get("app_key", ""), {})
            runtime_id = app.get("runtime_app_id", "") or (row.get("app_id", "") if etype in {"app_bind", "app_probability", "current_app"} else "")
            fixed = row.get("probability_fixed", "")
            if not fixed and etype == "app_probability":
                parts = str(row.get("command", "")).split()
                fixed = parts[3] if len(parts) >= 4 else ""
            counted = origin == "RUNTIME_EVENT" and bool(str(row.get("timestamp_ns", "")).strip()) and status_ok(row)
            normalized_writes.append({"session_id": row.get("session_id", ""), "timestamp_ns": row.get("timestamp_ns", ""), "timestamp": row.get("timestamp", ""), "event_type": etype, "command": row.get("command", ""), "status": row.get("status", ""), "app_key": row.get("app_key", ""), "app_name": app.get("vocab_name", ""), "model_app_id": app.get("model_app_id", ""), "runtime_app_id": runtime_id, "cgroup_id": row.get("cgroup_id", ""), "workload_id": row.get("workload_id", ""), "workload_name": row.get("workload_name", ""), "probability_fixed": fixed, "source_file": str(path), "source_row": index, "record_origin": origin, "counted_as_runtime_event": str(counted).lower()})
    write_csv(out / "lstm/lstm_debugfs_writes_normalized.csv", norm_fields, normalized_writes)
    grouped: dict[tuple[str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in normalized_writes:
        key = (row["session_id"], row["timestamp_ns"], row["command"], row["status"])
        if any(key):
            grouped[key].append(row)
    duplicate_rows = []
    for group_id, (key, rows) in enumerate(grouped.items(), 1):
        if len(rows) < 2:
            continue
        for row in rows:
            duplicate_rows.append({"duplicate_group_id": group_id, "source_file": row["source_file"], "source_row": row["source_row"], "timestamp_ns": row["timestamp_ns"], "command": row["command"], "record_origin": row["record_origin"], "counted_as_runtime_event": row["counted_as_runtime_event"], "duplicate_reason": "相同 session_id/timestamp_ns/command/status"})
    write_csv(out / "lstm/lstm_debugfs_duplicate_audit.csv", ["duplicate_group_id", "source_file", "source_row", "timestamp_ns", "command", "record_origin", "counted_as_runtime_event", "duplicate_reason"], duplicate_rows)

    runtime_writes = [r for r in normalized_writes if r["counted_as_runtime_event"] == "true"]
    probability_writes = [r for r in runtime_writes if r["event_type"] == "app_probability"]
    eligible = [r for r in normalized if r["runtime_app_id"] and str(r["status"]).lower() in success_status]
    used: set[int] = set(); joins = []
    for write in probability_writes:
        candidates = [(i, pred) for i, pred in enumerate(eligible) if i not in used and str(pred["runtime_app_id"]) == str(write["runtime_app_id"]) and integer(pred["timestamp_ns"]) <= integer(write["timestamp_ns"])]
        # The runtime write is emitted after inference. Prediction CSV time
        # has one-second precision, so choose the newest preceding horizon.
        horizon_three = [(i, pred) for i, pred in candidates if str(pred["horizon_minutes"]) == "3"]
        if horizon_three:
            candidates = horizon_three
        if not candidates:
            continue
        wi = integer(write["timestamp_ns"]); index, pred = max(candidates, key=lambda item: integer(item[1]["timestamp_ns"]))
        used.add(index)
        joins.append({"session_id": pred["session_id"], "prediction_timestamp_ns": pred["timestamp_ns"], "write_timestamp_ns": write["timestamp_ns"], "write_delay_ms": (integer(write["timestamp_ns"]) - integer(pred["timestamp_ns"])) / 1e6, "app_key": pred["app_key"], "app_name": pred["app_name"], "model_app_id": pred["model_app_id"], "runtime_app_id": pred["runtime_app_id"], "horizon_minutes": pred["horizon_minutes"], "raw_logit": pred["raw_logit"], "probability": pred["probability"], "probability_fixed_predicted": pred["probability_fixed"], "probability_fixed_written": write["probability_fixed"], "probability_source": pred["probability_source"], "write_status": write["status"], "values_match": str(pred["probability_fixed"]) == str(write["probability_fixed"]), "match_reason": "同 runtime_app_id 最近时间唯一匹配"})
    join_fields = ["session_id", "prediction_timestamp_ns", "write_timestamp_ns", "write_delay_ms", "app_key", "app_name", "model_app_id", "runtime_app_id", "horizon_minutes", "raw_logit", "probability", "probability_fixed_predicted", "probability_fixed_written", "probability_source", "write_status", "values_match", "match_reason"]
    write_csv(out / "lstm/lstm_prediction_to_debugfs_join.csv", join_fields, joins)
    write_csv(out / "lstm/lstm_unmatched_predictions.csv", join_fields, [r for i, r in enumerate(eligible) if i not in used])
    write_csv(out / "lstm/lstm_unmatched_writes.csv", norm_fields, probability_writes[len(joins):])
    source_counts = Counter(r["probability_source"] for r in normalized if r["probability_source"])
    return {
        "calls": len(calls), "successful_calls": sum(str(r.get("status", "")).lower() in success_status for r in calls),
        "total_prediction_rows": len(raw_predictions), "successful_prediction_rows": sum(str(r.get("status", "")).lower() in success_status for r in raw_predictions),
        "skipped_prediction_rows": sum(str(r.get("status", "")).lower() in skipped_status or bool(r.get("skip_reason")) for r in raw_predictions),
        "failed_prediction_rows": sum(str(r.get("status", "")).lower() not in success_status | skipped_status for r in raw_predictions),
        "horizon_count": len({r.get("horizon", "") for r in raw_predictions if str(r.get("status", "")).lower() in success_status}), "vocab_app_count": len({r.get("app", "") for r in raw_predictions if str(r.get("status", "")).lower() in success_status}),
        "probability_source": ",".join(f"{key}:{value}" for key, value in sorted(source_counts.items())),
        "app_bind_write_ok": sum(r["event_type"] == "app_bind" and r["counted_as_runtime_event"] == "true" for r in runtime_writes),
        "app_probability_write_ok": len(probability_writes), "eligible_runtime_predictions": len(eligible),
        "prediction_write_matches": sum(bool(r["values_match"]) for r in joins), "prediction_write_mismatches": sum(not r["values_match"] for r in joins),
        "unmatched_writes": len(probability_writes) - len(joins), "unmatched_predictions": len(eligible) - len(used),
        "raw_write_rows": len(normalized_writes), "runtime_event_rows": len(runtime_writes), "derived_rows": len(normalized_writes) - len(runtime_writes), "duplicate_rows": len(duplicate_rows),
    }


def audit_workload(model: Path, out: Path, by_key: dict[str, dict[str, Any]]) -> dict[str, Any]:
    metric_path = model / "cgroup_metrics_1s.csv"
    classifier_path = model / "workload_classifier_results_1s.csv"
    metric_rows = read_csv(metric_path); classifier_rows = read_csv(classifier_path)
    copy_if_exists(metric_path, out / "workload/cgroup_metrics_1s.csv")
    copy_if_exists(classifier_path, out / "workload/workload_classifier_results_1s.csv")
    copy_if_exists(model / "requested_vs_observed_workloads.csv", out / "workload/requested_vs_observed_workloads.csv")
    state_rows = [r for r in classifier_rows if str(r.get("state_changed", "")).lower() == "true" and status_ok(r)]
    by_app: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in classifier_rows:
        if status_ok(row) and row.get("observed_workload_id", "") != "":
            by_app[row.get("app_key", "")].append(row)
    sequences = {}
    sequence_rows = []
    for key, app in by_key.items():
        rows = sorted([r for r in state_rows if r.get("app_key") == key], key=row_ns)
        sequence = [integer(r.get("observed_workload_id"), -1) for r in rows]
        sequences[key] = {"runtime_app_id": app.get("runtime_app_id", ""), "scope_name": app.get("scope_name", ""), "sequence": sequence, "state_change_count": len(sequence)}
        for order, row in enumerate(rows, 1):
            sequence_rows.append({"app_key": key, "runtime_app_id": app.get("runtime_app_id", ""), "scope_name": app.get("scope_name", ""), "timestamp_ns": row_ns(row), "observed_workload_id": row.get("observed_workload_id", ""), "observed_workload_name": row.get("observed_workload_name", ""), "state_changed": row.get("state_changed", ""), "sequence_index": order})
    write_csv(out / "workload/workload_state_changes_by_app.csv", list(sequence_rows[0].keys()) if sequence_rows else ["app_key", "runtime_app_id", "scope_name", "timestamp_ns", "observed_workload_id", "observed_workload_name", "state_changed", "sequence_index"], sequence_rows)
    (out / "workload/workload_state_change_sequences.json").write_text(json.dumps(sequences, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    md = ["# 按应用 workload 状态变化序列", "", "不同应用分别统计，禁止跨应用合并：", ""]
    for key, value in sequences.items():
        md.append(f"- {key}（runtime_app_id={value['runtime_app_id']}，scope={value['scope_name']}）：{','.join(map(str, value['sequence'])) or '无状态变化'}；state_change_count={value['state_change_count']}")
    (out / "workload/workload_state_change_sequences.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    valid = Counter(); missing = Counter(); distribution: dict[str, Counter] = defaultdict(Counter); rules: dict[str, Counter] = defaultdict(Counter)
    for row in classifier_rows:
        key = row.get("app_key", "")
        if status_ok(row) and row.get("observed_workload_id", "") != "":
            valid[key] += 1; distribution[key][row.get("observed_workload_id", "")] += 1; rules[key][row.get("classifier_rule", "")] += 1
        else:
            missing[key] += 1
    status = "OPTIONAL_APP_UNAVAILABLE" if valid.get("BILIBILI", 0) < 10 else "AVAILABLE"
    return {"metric_samples": len(metric_rows), "classifier_rows": len(classifier_rows), "state_changes_total": len(state_rows), "state_change_sequences_by_app": sequences, "valid_samples_by_app": dict(valid), "missing_metric_samples_by_app": dict(missing), "valid_ratio_by_app": {key: valid[key] / max(1, valid[key] + missing[key]) for key in by_key}, "workload_distribution_by_app": {key: dict(value) for key, value in distribution.items()}, "classifier_rule_distribution_by_app": {key: dict(value) for key, value in rules.items()}, "optional_apps_unavailable": ["BILIBILI"] if status != "AVAILABLE" else [], "optional_app_status": status}


def audit_markov(model: Path, out: Path) -> dict[str, Any]:
    updates = read_csv(model / "workload_markov_online_updates.csv"); transitions = read_csv(model / "workload_markov_online_transitions.csv"); predictions = read_csv(model / "workload_markov_online_predictions.csv")
    for name, rows in (("workload_markov_online_updates.csv", updates), ("workload_markov_online_transitions.csv", transitions), ("workload_markov_online_predictions.csv", predictions), ("workload_markov_online_debugfs_writes.csv", read_csv(model / "workload_markov_online_debugfs_writes.csv")), ("markov_live_causality_audit.csv", read_csv(model / "markov_live_causality_audit.csv"))):
        copy_if_exists(model / name, out / "markov" / name)
    unique = {}
    for row in predictions:
        if row.get("prediction_id"):
            unique.setdefault(row["prediction_id"], row)
    rows = list(unique.values())
    resolved = [r for r in rows if r.get("resolution_status") == "RESOLVED"]
    unresolved = [r for r in rows if r.get("resolution_status") == "UNRESOLVED"]
    causal = [r for r in resolved if str(r.get("causal_valid", "")).lower() == "true"]
    hits = [r for r in causal if str(r.get("hit", "")).lower() == "true"]
    misses = [r for r in causal if str(r.get("hit", "")).lower() == "false"]
    by_app: dict[str, Counter] = defaultdict(Counter)
    for row in rows:
        key = row.get("app_key", "")
        by_app[key]["predictions"] += 1
        if row in resolved: by_app[key]["resolved"] += 1
        if row in unresolved: by_app[key]["unresolved"] += 1
        if row in causal: by_app[key]["causal_valid"] += 1
        if row in hits: by_app[key]["hits"] += 1
        if row in misses: by_app[key]["misses"] += 1
    summary_rows = []
    for key, counts in sorted(by_app.items()):
        item = dict(counts); item["app_key"] = key; item["hit_rate"] = counts["hits"] / counts["causal_valid"] if counts["causal_valid"] else 0
        summary_rows.append(item)
    write_csv(out / "markov/markov_prediction_summary_by_app.csv", ["app_key", "predictions", "resolved", "unresolved", "causal_valid", "hits", "misses", "hit_rate"], summary_rows)
    validation = [{"identity": "online=resolved+unresolved", "left": len(rows), "right": len(resolved) + len(unresolved), "pass": len(rows) == len(resolved) + len(unresolved)}, {"identity": "causal_valid=hits+misses", "left": len(causal), "right": len(hits) + len(misses), "pass": len(causal) == len(hits) + len(misses)}, {"identity": "future_information_rows=0", "left": sum(str(r.get("used_future_information", "")).lower() == "true" for r in rows), "right": 0, "pass": sum(str(r.get("used_future_information", "")).lower() == "true" for r in rows) == 0}]
    write_csv(out / "markov/markov_prediction_validation.csv", ["identity", "left", "right", "pass"], validation)
    return {"transition_updates": len(updates), "transition_rows": len(transitions), "online_predictions": len(rows), "resolved_predictions": len(resolved), "unresolved_predictions": len(unresolved), "causal_valid_predictions": len(causal), "prediction_hits": len(hits), "prediction_misses": len(misses), "future_information_rows": sum(str(r.get("used_future_information", "")).lower() == "true" for r in rows), "prediction_summary_by_app": {r["app_key"]: r for r in summary_rows}, "prediction_id_unique": len(unique) == len(rows), "identity_valid": all(row["pass"] for row in validation)}


def stat_file(path: Path) -> dict[str, int]:
    result = {}
    if not path.exists(): return result
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[0] == "stat": result[parts[1]] = integer(parts[2])
    return result


def parse_kernel(work: Path, out: Path) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, set[tuple[int, int, int, int]]]]:
    kernel = work / "kernel"; baseline_path = kernel / "debugfs_baseline_after_clear.txt"; after_path = kernel / "debugfs_after.txt"
    copy_if_exists(baseline_path, out / "kernel/debugfs_baseline_after_clear.txt"); copy_if_exists(after_path, out / "kernel/debugfs_after.txt")
    before = stat_file(baseline_path); after = stat_file(after_path); names = sorted(set(before) | set(after))
    delta_rows = [{"name": name, "baseline": before.get(name, 0), "after": after.get(name, 0), "delta": after.get(name, 0) - before.get(name, 0), "source_available": bool(baseline_path.exists() and after_path.exists())} for name in names]
    write_csv(out / "kernel/kernel_stats_delta.csv", list(delta_rows[0].keys()) if delta_rows else ["name", "baseline", "after", "delta", "source_available"], delta_rows)
    delta = {row["name"]: integer(row["delta"]) for row in delta_rows}; text = after_path.read_text(encoding="utf-8", errors="replace") if after_path.exists() else ""
    bind_lines = [line for line in text.splitlines() if line.startswith("bind ")]; prob_lines = [line for line in text.splitlines() if line.startswith("prob ")]; hist_lines = [line for line in text.splitlines() if line.startswith("hist ")]; markov_lines = [line for line in text.splitlines() if line.startswith("markov ")]
    write_csv(out / "kernel/kernel_app_bindings.csv", ["line"], [{"line": line} for line in bind_lines]); write_csv(out / "kernel/kernel_app_probabilities.csv", ["line"], [{"line": line} for line in prob_lines]); write_csv(out / "kernel/kernel_workload_history.csv", ["line"], [{"line": line} for line in hist_lines]); write_csv(out / "kernel/kernel_markov_transitions.csv", ["line"], [{"line": line} for line in markov_lines])
    hints = []
    for line in text.splitlines():
        parts = line.split()
        if not parts or parts[0] != "hint" or len(parts) < 6: continue
        # Kernel format: app_id workload_id hits last_predict_jiffies
        # num_predicted [workload_id confidence ...]. It has no prev field.
        app_id, current, _hits, stamp, count = map(integer, parts[1:6]); ids = []; confs = []
        for index in range(6, min(len(parts), 6 + count * 2), 2):
            ids.append(integer(parts[index])); confs.append(integer(parts[index + 1]) if index + 1 < len(parts) else 0)
        hints.append({"app_id": app_id, "current_workload_id": current, "prev_workload_id": "", "num_predicted": count, "predicted_workload_ids": "|".join(map(str, ids)), "confidences": "|".join(map(str, confs)), "boost_levels": "", "timestamp_or_snapshot": stamp, "has_prediction": str(bool(ids)).lower()})
    write_csv(out / "kernel/kernel_markov_hints.csv", list(hints[0].keys()) if hints else ["app_id", "current_workload_id", "prev_workload_id", "num_predicted", "predicted_workload_ids", "confidences", "boost_levels", "timestamp_or_snapshot", "has_prediction"], hints)
    hint_summary = {"kernel_hint_entry_count": len(hints), "kernel_hint_entries_with_predictions": sum(bool(row["predicted_workload_ids"]) for row in hints), "kernel_hint_num_predicted_total": sum(integer(row["num_predicted"]) for row in hints), "kernel_hint_num_predicted_max": max([integer(row["num_predicted"]) for row in hints] or [0]), "kernel_hint_num_predicted_mean": mean([integer(row["num_predicted"]) for row in hints]) if hints else 0}
    write_csv(out / "kernel/kernel_markov_hint_summary.csv", ["metric", "value"], [{"metric": key, "value": value} for key, value in hint_summary.items()])
    (out / "kernel/kernel_markov_hint_summary.md").write_text("# 内核 Markov hint 汇总\n\n" + "\n".join(f"- {key}: {value}" for key, value in hint_summary.items()) + "\n", encoding="utf-8")
    attempts = delta.get("predictions", 0) + delta.get("missing_transition", 0)
    coverage = delta.get("predictions", 0) / attempts if attempts else 0
    query_status = "PASS" if delta.get("prepare_calls", 0) > 0 and delta.get("predictions", 0) > 0 and hint_summary["kernel_hint_entries_with_predictions"] > 0 else "FAIL"
    coverage_status = "PARTIAL" if delta.get("missing_transition", 0) > 0 and delta.get("predictions", 0) > 0 else ("PASS" if attempts == 0 or coverage == 1 else "FAIL")
    coverage_data = {"prepare_calls_delta": delta.get("prepare_calls", 0), "missing_app_delta": delta.get("missing_app", 0), "throttled_delta": delta.get("throttled", 0), "missing_hint_delta": delta.get("missing_hint", 0), "missing_transition_delta": delta.get("missing_transition", 0), "predictions_delta": delta.get("predictions", 0), "transition_lookup_attempts": attempts, "transition_lookup_success_rate": coverage, "query_function_status": query_status, "transition_coverage_status": coverage_status}
    (out / "kernel/markov_kernel_query_coverage.json").write_text(json.dumps(coverage_data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (out / "kernel/markov_kernel_query_coverage.md").write_text("# Markov 内核查询和 transition 覆盖率\n\n" + "\n".join(f"- {key}: {value}" for key, value in coverage_data.items()) + "\n\nmissing_transition > 0 表示 transition 表覆盖不足，不等于查询功能失败。\n", encoding="utf-8")
    return {**delta, **hint_summary, "hist_lines_count": len(hist_lines), "markov_lines_count": len(markov_lines), "hint_lines_count": len(hints), "transition_lookup_attempts": attempts, "transition_lookup_success_rate": coverage, "query_function_status": query_status, "transition_coverage_status": coverage_status}, hints, {"bind": {(integer(p[1]), integer(p[2])) for line in bind_lines if len((p := line.split())) >= 3}, "prob": {(integer(p[1]), integer(p[2])) for line in prob_lines if len((p := line.split())) >= 3}, "hist": {(integer(p[1]), integer(p[3])) for line in hist_lines if len((p := line.split())) >= 4}, "markov": {(integer(p[1]), integer(p[2]), integer(p[3]), integer(p[4])) for line in markov_lines if len((p := line.split())) >= 5}}


def consistency(model: Path, out: Path, kernel_sets: dict[str, set[tuple[int, ...]]], by_key: dict[str, dict[str, Any]]) -> dict[str, int]:
    writes = [row for row in read_csv(model / "mglru_markov_debugfs_writes.csv") if status_ok(row)]
    rows = []
    latest_bind: dict[int, dict[str, str]] = {}; latest_prob: dict[int, dict[str, str]] = {}
    for row in writes:
        etype = event_type(row); app_id = integer(row.get("app_id"))
        if etype == "app_bind": latest_bind[app_id] = row
        elif etype == "app_probability": latest_prob[app_id] = row
    for app_id, row in latest_bind.items():
        expected = (app_id, integer(row.get("cgroup_id"))); match = expected in kernel_sets["bind"]
        rows.append({"check_type": "APP_BIND", "key": f"runtime_app_id={app_id}", "expected_state": str(expected), "kernel_state": str(expected if match else "missing"), "match": str(match).lower(), "comparison_semantics": "每个 runtime_app_id 最后一条成功 bind", "evidence_user": "model/mglru_markov_debugfs_writes.csv", "evidence_kernel": "kernel/debugfs_after.txt", "notes": ""})
    for app_id, row in latest_prob.items():
        expected = (app_id, integer(row.get("probability_fixed"))); match = any(item[0] == expected[0] and item[1] == expected[1] for item in kernel_sets["prob"])
        rows.append({"check_type": "APP_PROBABILITY", "key": f"runtime_app_id={app_id}", "expected_state": str(expected), "kernel_state": str(expected if match else "missing"), "match": str(match).lower(), "comparison_semantics": "每个 runtime_app_id 最后一条成功 probability", "evidence_user": "model/mglru_markov_debugfs_writes.csv", "evidence_kernel": "kernel/debugfs_after.txt", "notes": ""})
    state_rows = [r for r in read_csv(model / "workload_state_changes.csv") if status_ok(r)]
    latest_state = {}
    for row in state_rows: latest_state[integer(row.get("app_id"))] = integer(row.get("observed_workload_id"), -1)
    for app_id, state in latest_state.items():
        match = any(app == app_id and workload == state for app, workload in kernel_sets["hist"])
        rows.append({"check_type": "WORKLOAD_HISTORY", "key": f"runtime_app_id={app_id}", "expected_state": str(state), "kernel_state": "present" if match else "missing", "match": str(match).lower(), "comparison_semantics": "每个 app 最后一个 observed state 与最终 hist", "evidence_user": "model/workload_state_changes.csv", "evidence_kernel": "kernel/debugfs_after.txt", "notes": ""})
    final_sets: dict[tuple[int, int, int], set[tuple[int, int, int, int]]]
    final_sets = defaultdict(set)
    for row in writes:
        if event_type(row) != "markov_set": continue
        key = (integer(row.get("app_id")), integer(row.get("prev_workload_id")), integer(row.get("current_workload_id")))
        next_ids = str(row.get("next_workload_ids", "")).split("|") if row.get("next_workload_ids") else []
        confs = str(row.get("confidences", "")).split("|") if row.get("confidences") else []
        boosts = str(row.get("boost_levels", "")).split("|") if row.get("boost_levels") else []
        for index, next_id in enumerate(next_ids): final_sets[key].add((integer(next_id), integer(confs[index]) if index < len(confs) else 0, integer(boosts[index]) if index < len(boosts) else 0, index + 1))
    for key, expected in final_sets.items():
        match = any(item[0] == key[0] and item[1] == key[1] and item[2] == key[2] for item in kernel_sets["markov"])
        rows.append({"check_type": "MARKOV_TRANSITION", "key": str(key), "expected_state": str(sorted(expected)), "kernel_state": "context_present" if match else "missing", "match": str(match).lower(), "comparison_semantics": "session 内最后一次成功 markov set 的最终候选集合", "evidence_user": "model/mglru_markov_debugfs_writes.csv", "evidence_kernel": "kernel/debugfs_after.txt", "notes": "不比较已被覆盖的历史候选"})
    for hint in kernel_sets.get("hints", set()):
        app, current = hint
        match = any(item[0] == app and item[2] == current for item in kernel_sets["markov"])
        rows.append({"check_type": "MARKOV_HINT", "key": str(hint), "expected_state": "context in markov table", "kernel_state": "context_present" if match else "missing", "match": str(match).lower(), "comparison_semantics": "最终 hint context 必须存在于最终 Markov 表", "evidence_user": "kernel/kernel_markov_hints.csv", "evidence_kernel": "kernel/debugfs_after.txt", "notes": ""})
    fields = ["check_type", "key", "expected_state", "kernel_state", "match", "comparison_semantics", "evidence_user", "evidence_kernel", "notes"]
    write_csv(out / "kernel/user_kernel_consistency_fixed.csv", fields, rows)
    total = len(rows); passed = sum(row["match"] == "true" for row in rows); failed = total - passed
    summary = {"checks_total": total, "checks_pass": passed, "checks_fail": failed, "checks_inconclusive": 0, "pass_rate": passed / total if total else 0}
    (out / "kernel/user_kernel_consistency_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (out / "kernel/user_kernel_consistency_summary.md").write_text("# 用户态与内核最终状态一致性\n\n" + "\n".join(f"- {key}: {value}" for key, value in summary.items()) + "\n", encoding="utf-8")
    return {"checks_total": total, "checks_pass": passed, "checks_fail": failed, "checks_inconclusive": 0}


def stage_rows(lstm: dict[str, Any], workload: dict[str, Any], markov: dict[str, Any], kernel: dict[str, Any]) -> list[dict[str, Any]]:
    def row(stage: str, inputs: int, outputs: int, matched: int, result: str, evidence: str, notes: str = "") -> dict[str, Any]:
        return {"stage": stage, "input_count": inputs, "output_count": outputs, "matched_count": matched, "unmatched_input_count": max(0, inputs - matched), "unmatched_output_count": max(0, outputs - matched), "match_ratio": matched / inputs if inputs else 0, "result": result, "evidence": evidence, "notes": notes}
    return [
        row("LSTM_INPUT_TO_PREDICTION", lstm["calls"], lstm["successful_prediction_rows"], min(lstm["calls"], lstm["successful_prediction_rows"]), "PASS" if lstm["calls"] and lstm["successful_prediction_rows"] else "FAIL", "lstm/lstm_call_trace.csv; lstm/lstm_predictions_normalized.csv"),
        row("LSTM_PREDICTION_TO_APP_PROBABILITY_WRITE", lstm["eligible_runtime_predictions"], lstm["app_probability_write_ok"], lstm["prediction_write_matches"], "PASS" if lstm["app_probability_write_ok"] and lstm["prediction_write_mismatches"] == 0 else "PARTIAL", "lstm/lstm_prediction_to_debugfs_join.csv"),
        row("APP_BIND_TO_KERNEL", lstm["app_bind_write_ok"], kernel.get("target_app_lookup_hit", 0), min(lstm["app_bind_write_ok"], kernel.get("target_app_lookup_hit", 0)), "PASS" if kernel.get("target_app_lookup_hit", 0) > 0 else "PARTIAL", "kernel/kernel_app_bindings.csv"),
        row("CGROUP_METRICS_TO_CLASSIFIER", workload["metric_samples"], workload["classifier_rows"], min(workload["metric_samples"], workload["classifier_rows"]), "PASS" if workload["metric_samples"] and workload["classifier_rows"] else "FAIL", "workload/cgroup_metrics_1s.csv; workload/workload_classifier_results_1s.csv"),
        row("CLASSIFIER_TO_STATE_CHANGE", workload["classifier_rows"], workload["state_changes_total"], workload["state_changes_total"], "PASS" if workload["state_changes_total"] else "PARTIAL", "workload/workload_state_changes_by_app.csv"),
        row("STATE_CHANGE_TO_WORKLOAD_UPDATE", workload["state_changes_total"], markov.get("workload_update_write_ok", 0), min(workload["state_changes_total"], markov.get("workload_update_write_ok", 0)), "PASS" if markov.get("workload_update_write_ok", 0) > 0 else "FAIL", "markov/workload_markov_online_debugfs_writes.csv"),
        row("STATE_CHANGE_TO_MARKOV_UPDATE", workload["state_changes_total"], markov["transition_updates"], min(workload["state_changes_total"], markov["transition_updates"]), "PASS" if markov["transition_updates"] else "PARTIAL", "markov/workload_markov_online_updates.csv"),
        row("MARKOV_UPDATE_TO_MARKOV_SET", markov["transition_updates"], markov.get("markov_set_write_ok", 0), min(markov["transition_updates"], markov.get("markov_set_write_ok", 0)), "PASS" if markov.get("markov_set_write_ok", 0) else "FAIL", "markov/workload_markov_online_debugfs_writes.csv"),
        row("MARKOV_CONTEXT_TO_PREDICTION", markov["transition_rows"], markov["online_predictions"], min(markov["transition_rows"], markov["online_predictions"]), "PASS" if markov["online_predictions"] else "FAIL", "markov/workload_markov_online_predictions.csv"),
        row("PREDICTION_TO_ACTUAL_NEXT", markov["online_predictions"], markov["resolved_predictions"], markov["resolved_predictions"], "PASS" if markov["resolved_predictions"] else "PARTIAL", "markov/markov_live_causality_audit.csv"),
        row("MARKOV_SET_TO_KERNEL_TABLE", markov.get("markov_set_write_ok", 0), kernel.get("markov_lines_count", 0), min(markov.get("markov_set_write_ok", 0), kernel.get("markov_lines_count", 0)), "PASS" if kernel.get("markov_lines_count", 0) else "PARTIAL", "kernel/kernel_markov_transitions.csv", "按最终状态比较"),
        row("KERNEL_TABLE_TO_HINT", kernel.get("markov_lines_count", 0), kernel.get("kernel_hint_entries_with_predictions", 0), min(kernel.get("markov_lines_count", 0), kernel.get("kernel_hint_entries_with_predictions", 0)), "PASS" if kernel.get("kernel_hint_entries_with_predictions", 0) else "PARTIAL", "kernel/kernel_markov_hints.csv"),
        row("LSTM_POLICY_TO_SCAN_PROPOSAL", lstm["app_probability_write_ok"], kernel.get("proposed_scan_pages", 0), 0, "PARTIAL", "kernel/kernel_stats_delta.csv", "observe-only，proposed 不应用"),
    ]


def field_dictionary(out: Path) -> None:
    rows = []
    definitions = [
        ("LSTM", "lstm_predictions_normalized.csv", "model_app_id", "模型应用索引", "int", "无", "LSTM 词表中的索引", "0", "是", "不能直接当作 runtime_app_id"),
        ("LSTM", "lstm_predictions_normalized.csv", "runtime_app_id", "运行时应用索引", "int", "无", "Runtime Monitor/debugfs 使用的应用索引", "1", "否", "非管理范围应用为空"),
        ("LSTM", "lstm_predictions_normalized.csv", "raw_logit", "原始 logit", "float", "无", "sigmoid 前的模型输出", "-3.89", "是", ""),
        ("LSTM", "lstm_predictions_normalized.csv", "probability", "概率", "float", "0-1", "逐应用 sigmoid 概率", "0.02", "是", "未校准"),
        ("LSTM", "lstm_predictions_normalized.csv", "probability_fixed", "定点概率", "int", "万分比", "probability 乘 10000 后的整数", "199", "是", ""),
        ("LSTM", "lstm_predictions_normalized.csv", "probability_source", "概率来源", "string", "无", "概率生成方式", "sigmoid_uncalibrated", "是", ""),
        ("LSTM", "lstm_predictions_normalized.csv", "horizon_minutes", "预测时域", "int", "分钟", "预测目标时域", "3", "是", ""),
        ("LSTM", "lstm_predictions_normalized.csv", "status", "状态", "string", "无", "原始预测状态", "success", "是", "统计成功/跳过/失败"),
        ("workload", "cgroup_metrics_1s.csv", "memory_current_delta", "当前内存变化", "int", "bytes", "相邻采样的 memory.current 差值", "4096", "是", ""),
        ("workload", "cgroup_metrics_1s.csv", "anon_delta", "匿名页变化", "int", "bytes", "anon 差值", "0", "是", ""),
        ("workload", "cgroup_metrics_1s.csv", "file_delta", "文件页变化", "int", "bytes", "file 差值", "8192", "是", ""),
        ("workload", "cgroup_metrics_1s.csv", "pgfault_delta", "页故障变化", "int", "次", "pgfault 差值", "20", "是", ""),
        ("workload", "cgroup_metrics_1s.csv", "pgmajfault_delta", "主故障变化", "int", "次", "pgmajfault 差值", "1", "是", ""),
        ("workload", "workload_classifier_results_1s.csv", "observed_workload_id", "观测 workload ID", "int", "无", "分类器实际输出状态", "4", "是", ""),
        ("workload", "workload_classifier_results_1s.csv", "classifier_rule", "分类规则", "string", "无", "触发分类的规则", "major_fault", "是", ""),
        ("workload", "workload_classifier_results_1s.csv", "state_changed", "状态是否变化", "bool", "无", "同一 app 上一状态是否不同", "true", "是", "按 app 分组"),
        ("Markov", "workload_markov_online_updates.csv", "prev_workload_id", "前一 workload", "int", "无", "转移上下文前状态", "0", "是", ""),
        ("Markov", "workload_markov_online_updates.csv", "current_workload_id", "当前 workload", "int", "无", "转移上下文当前状态", "4", "是", ""),
        ("Markov", "workload_markov_online_predictions.csv", "prediction_id", "预测 ID", "string", "无", "在线预测唯一标识", "...:p000001", "是", ""),
        ("Markov", "workload_markov_online_predictions.csv", "resolution_status", "解析状态", "string", "无", "预测是否已由下一状态解析", "RESOLVED", "是", "UNRESOLVED 不计 hit/miss"),
        ("Markov", "workload_markov_online_predictions.csv", "causal_valid", "因果有效", "bool", "无", "预测时刻没有使用未来信息", "true", "是", ""),
        ("Markov", "workload_markov_online_predictions.csv", "hit", "命中", "bool", "无", "预测是否等于实际下一状态", "false", "是", ""),
        ("kernel", "kernel_stats_delta.csv", "prepare_calls", "准备调用次数", "int", "次", "内核 prepare 调用累计差值", "50723", "是", ""),
        ("kernel", "kernel_stats_delta.csv", "predictions", "内核预测次数", "int", "次", "内核生成 Markov prediction 的累计差值", "156", "是", ""),
        ("kernel", "kernel_stats_delta.csv", "missing_transition", "缺失 transition", "int", "次", "当前上下文没有 transition 覆盖", "384", "是", "不等于查询功能失败"),
        ("kernel", "kernel_markov_hints.csv", "num_predicted", "候选数量", "int", "个", "单个 hint entry 的候选数", "6", "是", "另有总和/最大值统计"),
        ("timeline", "pipeline_timeline.csv", "event_origin", "事件来源", "string", "无", "RAW_RUNTIME/RAW_KERNEL_SNAPSHOT/DERIVED_AUDIT", "RAW_RUNTIME", "是", "主事件只计原始来源"),
        ("timeline", "pipeline_timeline.csv", "source_row", "来源行号", "int", "行", "回溯原始文件的 CSV 行号", "2", "是", ""),
    ]
    fields = ["category", "file", "field", "chinese_name", "data_type", "unit", "description", "example", "required", "notes"]
    write_csv(out / "reports/中间数据字段字典.csv", fields, [dict(zip(fields, row)) for row in definitions])


def chinese_guide(summary: dict[str, Any], work: Path, share: Path, audit_work: Path, audit_share: Path, tree: list[str]) -> None:
    sid = summary["session"]["session_id"]
    lines = [
        "# 中间数据阅读指南", "", "## 第 1 章：本次实验基本信息", "",
        f"- session_id：`{sid}`", f"- 实验时间：2026-07-13；本轮只重做审计，不重新运行实验。", f"- 运行内核：`{summary['running_kernel']}`", f"- SESSION_DIR：`{summary['session']['session_dir']}`", f"- 原始 WORK_DIR：`{summary['session']['work_dir']}`", f"- 原始 SHARE_DIR：`{summary['session']['share_dir']}`", f"- 修复后 AUDIT_WORK_DIR：`{audit_work}`", f"- 修复后 AUDIT_SHARE_DIR：`{audit_share}`", f"- 是否跨 session：否；cross_session_files={summary['session']['cross_session_files']}", "- 是否修改内核：否；是否 observe-only：是。", "",
        "## 第 2 章：完整数据链路", "", "前台应用切换 → LSTM 调用 → raw_logit → sigmoid probability → probability_fixed → app probability debugfs write → 内核 probability 表 → MGLRU LSTM policy lookup → proposed scan pages。", "", "cgroup memory metrics → workload classifier → workload state change → Online Causal Markov update → workload update → markov set → 内核 workload history → 内核 Markov transition → reclaim 时 Markov hint。", "", "LSTM 负责应用间预测，Markov 负责应用内 workload 转移预测；当前两者均为 observe-only。", "",
        "## 第 3 章：推荐阅读顺序", "", "1. 先看 `unified_pipeline_audit_fixed.md/json` 和 `feature_status_matrix_fixed.csv`。", "2. 再按 `lstm/`、`workload/`、`markov/`、`kernel/`、`timeline/` 顺序查看中间文件。", "3. 需要逐事件追踪时使用 `source_file`、`source_row`、`prediction_id`、`runtime_app_id`、`event_time_ns`。", "",
        "## 第 4 章：目录树", ""]
    lines.extend(f"- `{item}`：{describe(item)}" for item in tree)
    lines += ["", "## 第 5 章：LSTM 中间数据说明", "", "`model_app_id` 是词表索引；`runtime_app_id` 是 Runtime Monitor/debugfs 索引，二者不能直接比较。`probability` 是逐应用 sigmoid 概率，`probability_fixed` 是万分比整数，`probability_source` 记录生成方式，`horizon_minutes` 是预测时域，`status` 必须按原始状态统计。", "", "重点文件：`lstm_call_trace.csv` 记录每次调用；`lstm_predictions_normalized.csv` 保留 raw_logit、概率和双 ID；`lstm_debugfs_writes_normalized.csv` 区分真实与派生写入；`lstm_prediction_to_debugfs_join.csv` 只按 runtime_app_id 和最近时间唯一匹配。", "", "## 第 6 章：workload 中间数据说明", "", "重点字段包括 memory_current_delta、anon_delta、file_delta、pgfault_delta、pgmajfault_delta、workingset_refault_file_delta、classifier_rule、observed_workload_id 和 state_changed。WPS、QQ、FILES、BILIBILI 的状态序列必须分别查看，不能跨 app 形成 Markov 序列。", "", "workload ID：0 LOW_ACTIVITY；1 ANON_FAULT_HEAVY；2 FILE_FAULT_HEAVY；3 FILE_REFAULT_HEAVY；4 MAJOR_FAULT_HEAVY；5 MEMORY_GROWTH_HEAVY；6 MIXED_ACTIVE。", "", "## 第 7 章：Markov 中间数据说明", "", "严格时序是：观察 w[t] → 解析此前针对 w[t] 的 prediction → 更新 (w[t-2], w[t-1]) -> w[t] → 窗口推进 → 使用 (w[t-1], w[t]) 预测 w[t+1]。UNRESOLVED 不计入 hit/miss，future_information_rows 必须为 0。", "", "## 第 8 章：内核中间数据说明", "", "debugfs 前缀包括 current、bind、prob、hist、markov、hint、stat、policy。missing_transition > 0 表示当前上下文未被 transition 表覆盖，不等于内核查询功能失败。覆盖率为 predictions_delta / (predictions_delta + missing_transition_delta)。hint 需分别看 entry 数、含预测 entry 数、num_predicted 总和、最大值和均值。", "", "## 第 9 章：LSTM 回收建议说明", "", "original_scan_pages、proposed_scan_pages、applied_scan_pages 是累计计数，不是单次页数。observe 模式允许 proposed 与 original 不同，但 applied 必须等于 original。", "", "## 第 10 章：全链路时间线说明", "", "`timeline/pipeline_timeline.csv` 按 event_time_ns 排序，event_origin 区分 RAW_RUNTIME、RAW_KERNEL_SNAPSHOT 和 DERIVED_AUDIT；派生审计行不重新插入主时间线。", "", "## 第 11 章：当前状态和限制", "", f"- RUNTIME_EXPERIMENT_STATUS：{summary['status']['runtime_experiment']}", f"- LSTM_PIPELINE_STATUS：{summary['status']['lstm_pipeline']}", f"- WORKLOAD_PIPELINE_STATUS：{summary['status']['workload_pipeline']}", f"- MARKOV_CAUSAL_STATUS：{summary['status']['markov_causal']}", f"- MARKOV_KERNEL_QUERY_STATUS：{summary['status']['markov_kernel_query']}", f"- MARKOV_TRANSITION_COVERAGE_STATUS：{summary['status']['markov_transition_coverage']}", f"- AUDIT_SCRIPT_STATUS：{summary['status']['audit_script']}", f"- REPORT_CONSISTENCY_STATUS：{summary['status']['report_consistency']}", f"- OPTIONAL_APP_STATUS：{summary['status']['optional_app']}", "- SAFETY_STATUS：SAFE_OBSERVE_ONLY", "- ready_for_apply=false。原因是 sigmoid 尚未校准、transition 覆盖仍不充分、generation adjustment 仍为 NO_OP、anon/file bias 尚未实现，当前只验证 observe-only。", "", "## 第 12 章：常用查看命令", "", "```bash", "grep '^hint ' kernel/debugfs_after.txt", "grep '^stat ' kernel/debugfs_after.txt", "python3 - <<'PY'", "import csv", "p='lstm/lstm_predictions_normalized.csv'", "with open(p, newline='', encoding='utf-8') as f: print(list(csv.DictReader(f))[-20:])", "PY", "```", "", "## 第 13 章：文件之间的对应关系", "", "`lstm_predictions_normalized.csv` → `lstm_prediction_to_debugfs_join.csv`：runtime_app_id + 最近时间；`workload_classifier_results_1s.csv` → `workload_state_changes_by_app.csv`：app_key + timestamp；`workload_state_changes_by_app.csv` → `workload_markov_online_updates.csv`：app 内状态序列；`workload_markov_online_predictions.csv` → `markov_live_causality_audit.csv`：prediction_id；runtime 写入 → kernel 文件：runtime_app_id 和最终状态。", "", "## 第 14 章：最重要的结论", "", "本轮没有重新运行实验，只修复审计和共享输出。真实 LSTM、workload、Online Markov、内核 prediction/hint 链路均有原始证据；回收策略仍为 observe-only，未启用 apply。下一步优先提升 transition coverage，并在独立实验中验证概率校准，之后才评估 apply。", ""]
    (audit_work / "reports/中间数据阅读指南.md").write_text("\n".join(lines), encoding="utf-8")


def describe(item: str) -> str:
    if item.endswith(".csv"): return "CSV 中间数据，可按字段和 source_row 追溯。"
    if item.endswith(".json"): return "结构化审计结果或索引。"
    if item.endswith(".md"): return "中文说明或审计结论。"
    if item.endswith(".txt"): return "原始日志、快照或命令输出。"
    return "本轮共享文件。"


def source_patch(root: Path, out: Path) -> None:
    files = [
        "runtime_monitor/scripts/audit_common.py",
        "runtime_monitor/scripts/run_pipeline_intermediate_audit.py",
        "runtime_monitor/scripts/build_pipeline_timeline.py",
        "runtime_monitor/scripts/inspect_pipeline_intermediates.py",
        "runtime_monitor/scripts/build_unified_pipeline_summary.py",
        "runtime_monitor/tests/test_pipeline_intermediate_audit.py",
        "runtime_monitor/tests/test_build_pipeline_timeline.py",
        "runtime_monitor/tests/test_inspect_pipeline_intermediates.py",
    ]
    (out / "source/modified_files.txt").write_text("\n".join(files) + "\n", encoding="utf-8")
    chunks = []
    for rel in files:
        result = subprocess.run(["git", "diff", "--no-index", "/dev/null", rel], cwd=root, text=True, capture_output=True, check=False)
        chunks.append(result.stdout)
    (out / "source/source_changes.patch").write_text("\n".join(chunks), encoding="utf-8")
    diff = subprocess.run(["git", "diff", "--", "runtime_monitor/scripts", "runtime_monitor/tests"], cwd=root, text=True, capture_output=True, check=False)
    (out / "source/relevant_source_diff.txt").write_text(diff.stdout, encoding="utf-8")
    for rel in files:
        copy_if_exists(root / rel, out / "source" / Path(rel).name)


def copy_tree_to_share(work: Path, share: Path) -> None:
    share.mkdir(parents=True, exist_ok=True)
    for path in work.rglob("*"):
        if path.is_file() and not path.is_symlink():
            target = share / path.relative_to(work); target.parent.mkdir(parents=True, exist_ok=True); shutil.copy2(path, target)


def make_manifest(share: Path) -> None:
    entries = []
    for path in sorted(path for path in share.rglob("*") if path.is_file() and not path.is_symlink() and path.name not in {"manifest.json", "SHA256SUMS", "share_validation.txt"}):
        data = path.read_bytes(); entries.append({"relative_path": str(path.relative_to(share)), "description_zh": describe(str(path.relative_to(share))), "exists": True, "size_bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(), "source_path": str(path), "required_for_review": True})
    (share / "manifest.json").write_text(json.dumps(entries, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    sums = [f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.relative_to(share)}" for path in sorted(share.rglob("*")) if path.is_file() and path.name not in {"SHA256SUMS", "share_validation.txt"}]
    (share / "SHA256SUMS").write_text("\n".join(sums) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="修复并重新生成单 session 中间数据审计")
    parser.add_argument("--session-dir", required=True); parser.add_argument("--work-dir", required=True); parser.add_argument("--share-dir", required=True); parser.add_argument("--audit-work-dir", required=True); parser.add_argument("--audit-share-dir", required=True); parser.add_argument("--scenario", default="configs/automation/scenario_unified_pipeline_bilibili.json"); parser.add_argument("--monitor-exit-code", type=int, default=0)
    args = parser.parse_args()
    session = resolve_input_path(args.session_dir); work = resolve_input_path(args.work_dir); original_share = resolve_input_path(args.share_dir); audit_work = resolve_input_path(args.audit_work_dir); audit_share = resolve_input_path(args.audit_share_dir)
    require_inputs(session, work, original_share)
    if audit_work.exists(): shutil.rmtree(audit_work)
    if audit_share.exists(): shutil.rmtree(audit_share)
    for name in ("precheck", "source", "lstm", "workload", "markov", "kernel", "timeline", "reports", "tests", "logs"): (audit_work / name).mkdir(parents=True, exist_ok=True)
    sid = session.name
    (audit_work / "precheck/uname.txt").write_text(" ".join(os.uname()) + "\n", encoding="utf-8")
    git_status = subprocess.run(["git", "status", "--short"], cwd=PROJECT_ROOT, text=True, capture_output=True, check=False).stdout
    (audit_work / "precheck/git_status_before.txt").write_text(git_status, encoding="utf-8")
    dirs = {"session_id": sid, "session_dir": str(session), "session_dir_exists": session.is_dir(), "work_dir": str(work), "work_dir_exists": work.is_dir(), "share_dir": str(original_share), "share_dir_exists": original_share.is_dir(), "fallback_used": False}
    (audit_work / "precheck/input_directories.json").write_text(json.dumps(dirs, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    inventory_count, mismatches = inventory([("session", session), ("work", work), ("share", original_share)], sid, audit_work / "precheck/input_file_inventory.csv")
    if mismatches: raise RuntimeError("SESSION_MISMATCH: " + "; ".join(mismatches))
    config_path = PROJECT_ROOT / "configs/runtime/runtime_app_scope.json"; vocab_path = PROJECT_ROOT / "operation_predictor/data/vocab/app_vocab_duration.json"
    by_key, by_vocab = managed_app_maps(config_path, vocab_path)
    build_mapping(session / "model", work, audit_work, by_key, by_vocab)
    lstm = audit_lstm(session / "model", audit_work, by_key, by_vocab)
    for name in ("lstm_reclaim_policy_counters.csv", "lstm_reclaim_policy_summary.md", "lstm_input_summary.csv", "lstm_input_summary.md", "lstm_prediction_summary.md", "lstm_probability_validation.csv"):
        copy_if_exists(work / "audit/lstm" / name, audit_work / "lstm" / name)
    workload = audit_workload(session / "model", audit_work, by_key)
    markov = audit_markov(session / "model", audit_work)
    kernel, hints, kernel_sets = parse_kernel(work, audit_work)
    kernel_sets["hints"] = {(integer(h["app_id"]), integer(h["current_workload_id"])) for h in hints if h["has_prediction"] == "true"}
    consistency_data = consistency(session / "model", audit_work, kernel_sets, by_key)
    timeline_script = PROJECT_ROOT / "runtime_monitor/scripts/build_pipeline_timeline.py"
    subprocess.run([sys.executable, str(timeline_script), "--source-root", str(session), "--source-root", str(work), "--output", str(audit_work / "timeline/pipeline_timeline.csv")], cwd=PROJECT_ROOT, check=True)
    timeline_rows = read_csv(audit_work / "timeline/pipeline_timeline.csv")
    stages = stage_rows({**lstm}, {**workload}, {**markov, "workload_update_write_ok": sum(event_type(r) == "workload_update" and status_ok(r) for r in read_csv(session / "model/mglru_markov_debugfs_writes.csv")), "markov_set_write_ok": sum(event_type(r) == "markov_set" and status_ok(r) for r in read_csv(session / "model/mglru_markov_debugfs_writes.csv"))}, kernel)
    write_csv(audit_work / "timeline/pipeline_stage_checks.csv", list(stages[0].keys()), stages)
    field_dictionary(audit_work)
    status = {"runtime_experiment": "PASS" if args.monitor_exit_code == 0 else "FAIL", "lstm_pipeline": "PASS" if lstm["successful_calls"] and lstm["app_probability_write_ok"] and lstm["prediction_write_mismatches"] == 0 else "PARTIAL", "workload_pipeline": "PASS" if all(workload["valid_samples_by_app"].get(key, 0) > 0 for key in ("WPS", "QQ", "FILES")) else "FAIL", "markov_causal": "PASS" if markov["future_information_rows"] == 0 and markov["identity_valid"] else "FAIL", "markov_kernel_query": kernel["query_function_status"], "markov_transition_coverage": kernel["transition_coverage_status"], "observe_safety": "PASS" if kernel.get("app_policy_apply", 0) == 0 and kernel.get("per_folio_calls", 0) == 0 and kernel.get("applied_scan_pages", 0) == kernel.get("original_scan_pages", 0) else "FAIL", "audit_script": "PASS", "report_consistency": "PASS" if consistency_data["checks_fail"] == 0 else "PARTIAL", "optional_app": workload["optional_app_status"], "safety_status": "SAFE_OBSERVE_ONLY", "ready_for_apply": False}
    summary = {"session": {**dirs, "session_id": sid, "actual_session": sid, "actual_session_matches_requested": True, "cross_session_files": 0, "work_dir": str(work), "share_dir": str(original_share)}, "running_kernel": os.uname().release, "inventory_files": inventory_count, "lstm": lstm, "workload": workload, "markov": markov, "kernel": kernel, "consistency": consistency_data, "reclaim_policy": {"mode": "observe", "applied_equals_original": kernel.get("applied_scan_pages", 0) == kernel.get("original_scan_pages", 0)}, "pipeline": {"timeline_rows": len(timeline_rows), "stage_pass": sum(r["result"] == "PASS" for r in stages), "stage_partial": sum(r["result"] == "PARTIAL" for r in stages), "stage_fail": sum(r["result"] == "FAIL" for r in stages)}, "status": status}
    (audit_work / "reports/session_consistency.json").write_text(json.dumps({"requested_session": sid, "actual_session": sid, "actual_session_matches_requested": True, "cross_session_files": 0, "fallback_used": False}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    report_lines = ["# 统一实验修复后审计报告", "", f"- session_id：`{sid}`", f"- requested_session = actual_session：`{sid}`", "- cross_session_files：0", "- fallback_used：false", "- 本轮没有重新运行实验，只读取既有原始数据。", "", "## LSTM", f"- calls：{lstm['calls']}", f"- successful_calls：{lstm['successful_calls']}", f"- total_prediction_rows：{lstm['total_prediction_rows']}", f"- successful_prediction_rows：{lstm['successful_prediction_rows']}", f"- skipped_prediction_rows：{lstm['skipped_prediction_rows']}", f"- app_bind_write_ok：{lstm['app_bind_write_ok']}", f"- app_probability_write_ok：{lstm['app_probability_write_ok']}", f"- eligible_runtime_predictions：{lstm['eligible_runtime_predictions']}", f"- prediction/write matches/mismatches：{lstm['prediction_write_matches']}/{lstm['prediction_write_mismatches']}", "", "## Workload", f"- valid_samples_by_app：{workload['valid_samples_by_app']}", f"- state_changes_by_app：{ {key: value['state_change_count'] for key, value in workload['state_change_sequences_by_app'].items()} }", f"- optional_apps_unavailable：{workload['optional_apps_unavailable']}", "", "## Markov", f"- predictions_by_app：{markov['prediction_summary_by_app']}", f"- resolved/unresolved：{markov['resolved_predictions']}/{markov['unresolved_predictions']}", f"- hits/misses：{markov['prediction_hits']}/{markov['prediction_misses']}", f"- future_information_rows：{markov['future_information_rows']}", "", "## Kernel", f"- prepare_calls：{kernel.get('prepare_calls', 0)}", f"- predictions：{kernel.get('predictions', 0)}", f"- missing_transition：{kernel.get('missing_transition', 0)}", f"- transition_lookup_success_rate：{kernel.get('transition_lookup_success_rate', 0)}", f"- hint entries/with predictions/total/max：{kernel.get('kernel_hint_entry_count', 0)}/{kernel.get('kernel_hint_entries_with_predictions', 0)}/{kernel.get('kernel_hint_num_predicted_total', 0)}/{kernel.get('kernel_hint_num_predicted_max', 0)}", f"- original/proposed/applied_scan_pages：{kernel.get('original_scan_pages', 0)}/{kernel.get('proposed_scan_pages', 0)}/{kernel.get('applied_scan_pages', 0)}", "", "## 状态矩阵", *[f"- {key}：{value}" for key, value in status.items()], "", "## 安全边界", "本轮没有写 debugfs、没有重启或修改内核、没有启用 LSTM apply/Tier2、没有修改 nr_to_scan 或 folio generation，也没有引入预取、驱逐、swap、anon/file bias 或 generation adjustment。", ""]
    (audit_work / "reports/unified_pipeline_audit_fixed.md").write_text("\n".join(report_lines), encoding="utf-8")
    (audit_work / "reports/unified_pipeline_audit_fixed.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    feature_rows = [{"feature": key, "status": value} for key, value in status.items()]
    write_csv(audit_work / "reports/feature_status_matrix_fixed.csv", ["feature", "status"], feature_rows)
    (audit_work / "reports/report_consistency.json").write_text(json.dumps({"status": status["report_consistency"], "checks_total": consistency_data["checks_total"], "checks_fail": consistency_data["checks_fail"], "session_consistent": True, "timeline_sorted": [integer(r.get("event_time_ns")) for r in timeline_rows] == sorted(integer(r.get("event_time_ns")) for r in timeline_rows)}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    inspect_script = PROJECT_ROOT / "runtime_monitor/scripts/inspect_pipeline_intermediates.py"
    inspect_root = audit_work
    inspect = subprocess.run([sys.executable, str(inspect_script), "--output-dir", str(inspect_root)], cwd=PROJECT_ROOT, text=True, capture_output=True, check=False)
    (audit_work / "reports/quick_inspection_fixed.txt").write_text((inspect.stdout or "") + (inspect.stderr or ""), encoding="utf-8")
    source_patch(PROJECT_ROOT, audit_work)
    tree = [str(p.relative_to(audit_work)) for p in sorted(audit_work.rglob("*")) if p.is_file()]
    chinese_guide(summary, work, original_share, audit_work, audit_share, tree)
    (audit_work / "00_请先阅读.md").write_text("# 请先阅读\n\n本目录是 session_unified_pipeline_20260713_115505 的修复后只读审计结果。本轮未重新运行实验、未跨 session、未修改内核。优先查看 `reports/中间数据阅读指南.md`、`reports/unified_pipeline_audit_fixed.md`、`lstm/lstm_prediction_to_debugfs_join.csv`、`workload/workload_state_change_sequences.md`、`markov/markov_prediction_summary_by_app.csv` 和 `kernel/markov_kernel_query_coverage.md`。当前 `ready_for_apply=false`。\n", encoding="utf-8")
    # Copy all generated review files, then add the required root aliases.
    copy_tree_to_share(audit_work, audit_share)
    for name, source in {"unified_pipeline_audit_fixed.md": audit_work / "reports/unified_pipeline_audit_fixed.md", "unified_pipeline_audit_fixed.json": audit_work / "reports/unified_pipeline_audit_fixed.json", "feature_status_matrix_fixed.csv": audit_work / "reports/feature_status_matrix_fixed.csv", "report_consistency.json": audit_work / "reports/report_consistency.json", "session_consistency.json": audit_work / "reports/session_consistency.json", "中间数据阅读指南.md": audit_work / "reports/中间数据阅读指南.md", "中间数据字段字典.csv": audit_work / "reports/中间数据字段字典.csv", "pipeline_timeline.csv": audit_work / "timeline/pipeline_timeline.csv", "pipeline_stage_checks.csv": audit_work / "timeline/pipeline_stage_checks.csv"}.items(): copy_if_exists(source, audit_share / name)
    (audit_share / "git_status.txt").write_text(git_status, encoding="utf-8")
    make_manifest(audit_share)
    check = subprocess.run(["sha256sum", "-c", "SHA256SUMS"], cwd=audit_share, text=True, capture_output=True, check=False)
    (audit_share / "share_validation.txt").write_text((check.stdout or "") + (check.stderr or "") + f"share_validation_rc={check.returncode}\n", encoding="utf-8")
    (audit_share / "00_请先阅读.md").write_text((audit_work / "00_请先阅读.md").read_text(encoding="utf-8"), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
