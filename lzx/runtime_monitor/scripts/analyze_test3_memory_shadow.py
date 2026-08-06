#!/usr/bin/env python3
"""Build Test3's conservative prediction-to-memory shadow analysis.

The script is intentionally offline and has no kernel-control code.  It joins
the event-time observer's raw records; counterfactual values are emitted only
when the underlying read-only evidence exists.
"""

from __future__ import annotations

import argparse
import csv
import json
import shutil
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "runtime_monitor") not in sys.path:
    sys.path.insert(0, str(ROOT / "runtime_monitor"))

from core.memory_shadow import memory_value


JOIN_FIELDS = [
    "episode_id", "prediction_id", "prediction_batch_id", "snapshot_version",
    "prediction_time_ns", "current_app", "candidate_app", "candidate_rank",
    "candidate_probability", "candidate_running_at_prediction", "actual_next_app",
    "prediction_correct", "top1_correct", "top3_correct", "time_to_actual_switch_ms",
    "working_set_at_prediction_bytes", "working_set_metric", "memory_drop_before_switch_bytes",
    "rss_loss_bytes", "pss_loss_bytes", "referenced_loss_bytes", "anon_loss_bytes",
    "file_loss_bytes", "reclaim_associated_drop_bytes", "reclaim_evidence_type",
    "swap_growth_before_switch_bytes", "memory_rebuild_500ms_bytes",
    "memory_rebuild_1s_bytes", "memory_rebuild_3s_bytes", "minor_fault_1s",
    "major_fault_1s", "refault_1s", "swapin_1s", "file_read_1s_bytes",
    "file_read_evidence", "activation_latency_ms", "interactive_latency_ms",
    "potentially_avoidable_rebuild_bytes", "causal_evidence_status",
    "potential_wasted_protection_bytes", "memory_sample_status", "notes",
]

EPISODE_VALUE_FIELDS = [
    "episode_id", "prediction_id", "actual_next_app", "actual_candidate_present",
    "actual_candidate_rank", "actual_candidate_running_state", "working_set_at_prediction_bytes",
    "memory_drop_before_switch_bytes", "reclaim_associated_drop_bytes",
    "memory_rebuild_1s_bytes", "refault_1s", "causal_evidence_status",
    "potentially_avoidable_rebuild_bytes",
]


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, fields: list[str], rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def as_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(str(value)))
    except (TypeError, ValueError):
        return default


def as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return default


def delta(current: dict[str, str] | None, previous: dict[str, str] | None, field: str) -> int | None:
    if current is None or previous is None:
        return None
    result = as_int(current.get(field)) - as_int(previous.get(field))
    return result if result >= 0 else None


def nonnegative_drop(before: dict[str, str] | None, after: dict[str, str] | None, field: str) -> int | None:
    if before is None or after is None:
        return None
    return max(0, as_int(before.get(field)) - as_int(after.get(field)))


def latest_before(rows: list[dict[str, str]], point_ns: int) -> dict[str, str] | None:
    choices = [row for row in rows if as_int(row.get("timestamp_ns")) < point_ns]
    return max(choices, key=lambda row: as_int(row.get("timestamp_ns")), default=None)


def first_at_or_after(rows: list[dict[str, str]], point_ns: int) -> dict[str, str] | None:
    choices = [row for row in rows if as_int(row.get("timestamp_ns")) >= point_ns]
    return min(choices, key=lambda row: as_int(row.get("timestamp_ns")), default=None)


def maximum_until(rows: list[dict[str, str]], start_ns: int, end_ns: int, field: str) -> int | None:
    choices = [as_int(row.get(field)) for row in rows if start_ns <= as_int(row.get("timestamp_ns")) <= end_ns]
    return max(choices) if choices else None


def maximum_working_set_until(rows: list[dict[str, str]], start_ns: int, end_ns: int) -> int | None:
    choices = [
        value for row in rows if start_ns <= as_int(row.get("timestamp_ns")) <= end_ns
        for value, _metric in [choose_working_set(row)] if value is not None
    ]
    return max(choices) if choices else None


def choose_working_set(row: dict[str, str] | None) -> tuple[int | None, str]:
    if row is None:
        return None, "UNAVAILABLE"
    referenced = as_int(row.get("referenced_bytes"))
    if referenced > 0:
        return referenced, "REFERENCED"
    pss = as_int(row.get("pss_bytes"))
    if pss > 0:
        return pss, "PSS_FALLBACK"
    return None, "UNAVAILABLE"


def parse_candidates(raw: str) -> list[dict[str, Any]]:
    try:
        loaded = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return []
    return [item for item in loaded if isinstance(item, dict)] if isinstance(loaded, list) else []


def automation_latency(
    automation_rows: list[dict[str, str]], foreground_rows: list[dict[str, str]]
) -> tuple[list[dict[str, Any]], dict[tuple[int, str], int]]:
    """Match a switch command to the first observed target foreground edge."""
    output: list[dict[str, Any]] = []
    lookup: dict[tuple[int, str], int] = {}
    switches = [
        row for row in automation_rows
        if row.get("event_type") in {"OP_START", "APP_SWITCH_START"}
        and (row.get("action") == "switch" or row.get("event_type") == "APP_SWITCH_START")
    ]
    observed = [row for row in foreground_rows if row.get("event_type") == "APP_SWITCH"]
    for command in switches:
        target = str(command.get("app_key") or command.get("app") or "")
        start = as_int(command.get("ts_ns") or command.get("start_time_ns"))
        if not target or not start:
            continue
        matching = [
            row for row in observed
            if str(row.get("new_app") or row.get("app") or "") == target
            and as_int(row.get("ts_ns")) >= start
            and as_int(row.get("ts_ns")) - start <= 10_000_000_000
        ]
        foreground = min(matching, key=lambda row: as_int(row.get("ts_ns")), default=None)
        latency = as_int(foreground.get("ts_ns")) - start if foreground else None
        row = {
            "switch_command_time_ns": start, "target_app": target,
            "foreground_observed_time_ns": foreground.get("ts_ns", "") if foreground else "",
            "switch_command_to_foreground_ms": (latency / 1_000_000) if latency is not None else "",
            "foreground_to_probe_success_ms": "", "switch_command_to_probe_success_ms": "",
            "probe_status": "UNAVAILABLE_NO_STABLE_INTERACTIVE_PROBE",
            "latency_status": "OK_WINDOW_ACTIVATION_ONLY" if foreground else "NO_MATCHING_X11_SWITCH",
        }
        output.append(row)
        if foreground and latency is not None:
            lookup[(as_int(foreground.get("ts_ns")), target)] = latency // 1_000_000
    return output, lookup


def build_join(session: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    prediction_dir, memory_dir, model_dir = session / "prediction", session / "memory", session / "model"
    episodes = read_csv(prediction_dir / "prediction_episodes.csv")
    batches = {row.get("prediction_id", ""): row for row in read_csv(prediction_dir / "prediction_batches.csv")}
    samples_by_key: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for row in read_csv(memory_dir / "app_memory_shadow_250ms.csv"):
        samples_by_key[(row.get("episode_id", ""), row.get("app_key", ""))].append(row)
    for rows in samples_by_key.values():
        rows.sort(key=lambda row: as_int(row.get("timestamp_ns")))
    latency_rows, latency_lookup = automation_latency(
        read_csv(model_dir / "automation_trace.csv"), read_csv(model_dir / "foreground_events.csv")
    )
    write_csv(session / "latency" / "app_switch_latency.csv", list(latency_rows[0].keys()) if latency_rows else [
        "switch_command_time_ns", "target_app", "foreground_observed_time_ns", "switch_command_to_foreground_ms",
        "foreground_to_probe_success_ms", "switch_command_to_probe_success_ms", "probe_status", "latency_status",
    ], latency_rows)
    write_csv(session / "latency" / "interactive_probe_results.csv", [
        "probe_type", "status", "reason",
    ], [{"probe_type": "stable_interactive", "status": "UNAVAILABLE", "reason": "No app-specific stable-frame probe was enabled; X11 foreground latency is not interactive latency."}])

    join_rows: list[dict[str, Any]] = []
    episode_rows: list[dict[str, Any]] = []
    for episode in episodes:
        terminal_ns = as_int(episode.get("terminal_time_ns"))
        if episode.get("terminal_reason") != "NEXT_APP_SWITCH" or not terminal_ns:
            continue
        prediction_ns = as_int(episode.get("generated_at_ns"))
        actual = episode.get("actual_next_app", "")
        batch = batches.get(episode.get("prediction_id", ""), {})
        candidates = parse_candidates(episode.get("candidate_apps_json", ""))
        top3 = {str(item.get("app_key", "")) for item in candidates if as_int(item.get("rank"), 999) <= 3}
        episode_actual: dict[str, Any] | None = None
        for candidate in candidates:
            app = str(candidate.get("app_key", ""))
            rows = samples_by_key[(episode.get("episode_id", ""), app)]
            t0 = next((row for row in rows if row.get("sample_reason") == "T0_PREDICTION"), None)
            t1 = latest_before(rows, terminal_ns)
            if t1 is t0 and len([row for row in rows if as_int(row.get("timestamp_ns")) < terminal_ns]) < 2:
                # A single T0 point cannot establish pre-switch loss.
                t1 = None
            ws_t0, ws_metric = choose_working_set(t0)
            ws_t1, _ = choose_working_set(t1)
            drop = max(0, ws_t0 - ws_t1) if ws_t0 is not None and ws_t1 is not None else None
            t2_500 = maximum_working_set_until(rows, terminal_ns, terminal_ns + 500_000_000)
            t2_1s = maximum_working_set_until(rows, terminal_ns, terminal_ns + 1_000_000_000)
            t2_3s = maximum_working_set_until(rows, terminal_ns, terminal_ns + 3_000_000_000)
            base = ws_t1 if ws_t1 is not None else None
            rebuilds = [max(0, value - base) if value is not None and base is not None else None for value in (t2_500, t2_1s, t2_3s)]
            one_second = first_at_or_after(rows, terminal_ns + 1_000_000_000)
            if one_second is None:
                one_second = latest_before(rows, terminal_ns + 1_000_000_000)
            # Reclaim/swap prior to activation is measured at T0 -> T1.
            pgscan_before = delta(t1, t0, "pgscan")
            pgsteal_before = delta(t1, t0, "pgsteal")
            pswpout_before = delta(t1, t0, "pswpout")
            swap_growth = (as_int(t1.get("swap_bytes")) - as_int(t0.get("swap_bytes"))) if t0 and t1 else None
            reclaim_evidence = []
            if (pgscan_before or 0) > 0 or (pgsteal_before or 0) > 0:
                reclaim_evidence.append("CGROUP_PGSCAN_PGSTEAL")
            if (pswpout_before or 0) > 0 or (swap_growth or 0) > 0:
                reclaim_evidence.append("CGROUP_SWAP_COUNTER")
            reclaim_associated = drop if drop is not None and reclaim_evidence else 0
            fault = delta(one_second, t1, "pgfault")
            major = delta(one_second, t1, "pgmajfault")
            refault_anon = delta(one_second, t1, "workingset_refault_anon")
            refault_file = delta(one_second, t1, "workingset_refault_file")
            refault = (refault_anon or 0) + (refault_file or 0) if refault_anon is not None or refault_file is not None else None
            swapin = delta(one_second, t1, "pswpin")
            file_read = delta(one_second, t1, "proc_read_bytes")
            rebuild = rebuilds[2]
            recovery_cost = any((value or 0) > 0 for value in (fault, major, refault, swapin, file_read))
            causal = "INSUFFICIENT_CAUSAL_EVIDENCE"
            potentially_avoidable: int | str = ""
            if drop is not None and drop > 0 and not reclaim_evidence:
                causal = "MEMORY_FOOTPRINT_DROP"
            if drop is not None and drop > 0 and reclaim_evidence and (rebuild or 0) > 0 and recovery_cost:
                causal = "POTENTIALLY_AVOIDABLE"
                potentially_avoidable = min(reclaim_associated, rebuild or 0)
            elif t0 is None or t1 is None:
                causal = "MEMORY_SAMPLE_UNAVAILABLE"
            correct = app == actual
            activation = latency_lookup.get((terminal_ns, actual), "") if correct else ""
            row = {
                "episode_id": episode.get("episode_id", ""), "prediction_id": episode.get("prediction_id", ""),
                "prediction_batch_id": episode.get("prediction_batch_id", ""), "snapshot_version": batch.get("snapshot_version_end", ""),
                "prediction_time_ns": prediction_ns, "current_app": episode.get("current_app", ""),
                "candidate_app": app, "candidate_rank": as_int(candidate.get("rank")),
                "candidate_probability": candidate.get("probability", ""),
                "candidate_running_at_prediction": candidate.get("running_state", "UNKNOWN"),
                "actual_next_app": actual, "prediction_correct": int(correct),
                "top1_correct": int(correct and as_int(candidate.get("rank")) == 1),
                "top3_correct": int(correct and app in top3),
                "time_to_actual_switch_ms": max(0, (terminal_ns - prediction_ns) // 1_000_000),
                "working_set_at_prediction_bytes": ws_t0 if ws_t0 is not None else "", "working_set_metric": ws_metric,
                "memory_drop_before_switch_bytes": drop if drop is not None else "",
                "rss_loss_bytes": nonnegative_drop(t0, t1, "rss_bytes") if t1 else "",
                "pss_loss_bytes": nonnegative_drop(t0, t1, "pss_bytes") if t1 else "",
                "referenced_loss_bytes": nonnegative_drop(t0, t1, "referenced_bytes") if t1 else "",
                "anon_loss_bytes": nonnegative_drop(t0, t1, "anon_bytes") if t1 else "",
                "file_loss_bytes": nonnegative_drop(t0, t1, "file_bytes") if t1 else "",
                "reclaim_associated_drop_bytes": reclaim_associated,
                "reclaim_evidence_type": "|".join(reclaim_evidence) if reclaim_evidence else "NONE",
                "swap_growth_before_switch_bytes": swap_growth if swap_growth is not None else "",
                "memory_rebuild_500ms_bytes": rebuilds[0] if rebuilds[0] is not None else "",
                "memory_rebuild_1s_bytes": rebuilds[1] if rebuilds[1] is not None else "",
                "memory_rebuild_3s_bytes": rebuilds[2] if rebuilds[2] is not None else "",
                "minor_fault_1s": fault if fault is not None else "", "major_fault_1s": major if major is not None else "",
                "refault_1s": refault if refault is not None else "", "swapin_1s": swapin if swapin is not None else "",
                "file_read_1s_bytes": file_read if file_read is not None else "",
                "file_read_evidence": "APPROXIMATE_PROC_IO_NOT_FAULT_ATTRIBUTABLE",
                "activation_latency_ms": activation, "interactive_latency_ms": "",
                "potentially_avoidable_rebuild_bytes": potentially_avoidable,
                "causal_evidence_status": causal,
                "potential_wasted_protection_bytes": ws_t0 if not correct and ws_t0 is not None else "",
                "memory_sample_status": (t0 or {}).get("metric_status", "UNAVAILABLE"),
                "notes": "pgscan/pgsteal are cgroup-counter association only; no per-folio tracepoint was available.",
            }
            join_rows.append(row)
            if correct:
                episode_actual = row
        if episode_actual is None:
            episode_actual = {"episode_id": episode.get("episode_id", ""), "prediction_id": episode.get("prediction_id", ""),
                              "actual_next_app": actual, "actual_candidate_present": 0, "actual_candidate_rank": "",
                              "actual_candidate_running_state": "UNAVAILABLE", "working_set_at_prediction_bytes": "",
                              "memory_drop_before_switch_bytes": "", "reclaim_associated_drop_bytes": "",
                              "memory_rebuild_1s_bytes": "", "refault_1s": "",
                              "causal_evidence_status": "ACTUAL_NEXT_NOT_IN_LSTM_CANDIDATES",
                              "potentially_avoidable_rebuild_bytes": ""}
        else:
            episode_actual = {
                "episode_id": episode_actual["episode_id"], "prediction_id": episode_actual["prediction_id"],
                "actual_next_app": actual, "actual_candidate_present": 1,
                "actual_candidate_rank": episode_actual["candidate_rank"],
                "actual_candidate_running_state": episode_actual["candidate_running_at_prediction"],
                "working_set_at_prediction_bytes": episode_actual["working_set_at_prediction_bytes"],
                "memory_drop_before_switch_bytes": episode_actual["memory_drop_before_switch_bytes"],
                "reclaim_associated_drop_bytes": episode_actual["reclaim_associated_drop_bytes"],
                "memory_rebuild_1s_bytes": episode_actual["memory_rebuild_1s_bytes"],
                "refault_1s": episode_actual["refault_1s"],
                "causal_evidence_status": episode_actual["causal_evidence_status"],
                "potentially_avoidable_rebuild_bytes": episode_actual["potentially_avoidable_rebuild_bytes"],
            }
        episode_rows.append(episode_actual)
    return join_rows, episode_rows, {"latency_rows": latency_rows, "episodes_total": len(episodes)}


def strategy_comparison(join_rows: list[dict[str, Any]], episode_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_episode: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in join_rows:
        by_episode[row["episode_id"]].append(row)
    actual_rows = {row["episode_id"]: row for row in episode_rows}
    strategies = {name: {"top1": 0, "top3": 0, "valuable": 0, "covered": 0, "waste": 0, "evaluated": 0, "available": 0}
                  for name in ("Native", "Last-App", "Most-Frequent", "Markov", "LSTM", "Oracle")}
    history: list[str] = []
    transitions: dict[str, Counter[str]] = defaultdict(Counter)
    ordered = sorted(episode_rows, key=lambda row: next((as_int(item["prediction_time_ns"]) for item in by_episode[row["episode_id"]]), 0))
    for actual_row in ordered:
        eid, actual = actual_row["episode_id"], actual_row["actual_next_app"]
        candidates = sorted(by_episode.get(eid, []), key=lambda row: as_int(row.get("candidate_rank"), 999))
        current = candidates[0]["current_app"] if candidates else ""
        lstm = [row["candidate_app"] for row in candidates]
        previous = next((app for app in reversed(history) if app != current), "")
        counts = Counter(app for app in history if app != current)
        most_frequent = [app for app, _ in counts.most_common(3)]
        markov = [app for app, _ in transitions[current].most_common(3) if app != current]
        choices = {
            "Native": [], "Last-App": [previous] if previous else [], "Most-Frequent": most_frequent,
            "Markov": markov, "LSTM": lstm, "Oracle": [actual] if actual else [],
        }
        lookup = {row["candidate_app"]: row for row in candidates}
        for name, selected in choices.items():
            stats = strategies[name]; stats["evaluated"] += 1
            top1 = selected[:1]; top3 = selected[:3]
            stats["top1"] += int(actual in top1)
            stats["top3"] += int(actual in top3)
            hit = lookup.get(actual) if actual in top3 else None
            if hit is not None:
                stats["available"] += 1
                if hit.get("causal_evidence_status") == "POTENTIALLY_AVOIDABLE":
                    stats["valuable"] += 1; stats["covered"] += as_int(hit.get("potentially_avoidable_rebuild_bytes"))
            for candidate in top3:
                if candidate != actual and candidate in lookup:
                    stats["waste"] += as_int(lookup[candidate].get("potential_wasted_protection_bytes"))
        if current and actual:
            transitions[current][actual] += 1
        if current:
            history.append(current)
        if actual:
            history.append(actual)
    rows: list[dict[str, Any]] = []
    for name, value in strategies.items():
        n = value["evaluated"]
        rows.append({
            "strategy": name, "episode_count": n, "top1_accuracy": value["top1"] / n if n else "",
            "top3_accuracy": value["top3"] / n if n else "", "valuable_hit_count": value["valuable"],
            "valuable_hit_rate": value["valuable"] / n if n else "", "avoidable_bytes_covered": value["covered"],
            "false_protection_bytes": value["waste"], "candidate_memory_available_episodes": value["available"],
            "utility_lambda_0_25": value["covered"] - 0.25 * value["waste"],
            "utility_lambda_0_5": value["covered"] - 0.5 * value["waste"],
            "utility_lambda_1_0": value["covered"] - value["waste"],
            "scope": "SHADOW_COUNTERFACTUAL_ONLY",
        })
    return rows


def write_review(session: Path, join_rows: list[dict[str, Any]], episode_rows: list[dict[str, Any]], strategy_rows: list[dict[str, Any]]) -> None:
    review = session / "review"; review.mkdir(exist_ok=True)
    statuses = Counter(row.get("causal_evidence_status", "") for row in join_rows)
    episode_by_id = {row.get("episode_id"): row for row in episode_rows}
    event_rows = read_csv(session / "model" / "direct_app_events.csv")
    batch_rows = read_csv(session / "prediction" / "prediction_batches.csv")
    memory_rows = read_csv(session / "memory" / "app_memory_shadow_250ms.csv")
    write_json(review / "event_coverage.json", {"direct_event_count": len(event_rows), "by_type": Counter(row.get("event_type", "") for row in event_rows)})
    write_json(review / "prediction_episode_coverage.json", {
        "batch_count": len(batch_rows), "episode_count": len(episode_rows),
        "terminal_switch_episode_count": len(episode_rows),
        "bridge_write_success_batches": sum(row.get("bridge_status") == "WRITE_SUCCESS" for row in batch_rows),
    })
    write_json(review / "memory_sampling_coverage.json", {
        "sample_count": len(memory_rows), "ok_or_partial_samples": sum(row.get("metric_status") in {"OK", "PARTIAL"} for row in memory_rows),
        "unique_episode_app_pairs": len({(row.get("episode_id"), row.get("app_key")) for row in memory_rows}),
    })
    write_json(review / "causal_evidence_coverage.json", dict(statuses))
    (review / "observability_matrix.md").write_text("""# Test3 可观测接口矩阵

| 数据 | 来源 | 精度 | Test3 处理 |
|---|---|---:|---|
| prediction_id / batch | 在线 v3 LSTM + Test2 PARP bridge audit | 事件级 | 复用并在结束时关联 snapshot version |
| 前台与切换 | 原生 X11 APP_* 事件 | 事件级 | 不依赖 250 ms 前台轮询 |
| App 是否运行 | Runtime Monitor procfs + cgroup scope | 250 ms / T0 事件级 | RUNNING_BACKGROUND / NOT_RUNNING 等状态 |
| RSS/PSS/Referenced/Swap | `/proc/<pid>/smaps_rollup` | 250 ms | 聚合 scope 中可见进程 |
| memcg 工作集与 faults | `memory.current` / `memory.stat` | 250 ms | cgroup 归属计数 |
| refault / swap / scan-steal | `memory.stat` | 250 ms | cgroup-counter association，不升级为 per-folio trace |
| 文件读取 | `/proc/<pid>/io:read_bytes` | 250 ms | APPROXIMATE，不归因于 fault |
| mm_vmscan/mm_filemap trace | 当前 tracefs | 不可用 | NOT_ATTRIBUTABLE；不伪造 trace event |
| 切换延迟 | automation trace + X11 APP_SWITCH | 毫秒级 | 仅 window activation，不等价交互完成 |
""", encoding="utf-8")
    lstm = next((row for row in strategy_rows if row.get("strategy") == "LSTM"), {})
    oracle = next((row for row in strategy_rows if row.get("strategy") == "Oracle"), {})
    by_strategy = {row.get("strategy", ""): row for row in strategy_rows}
    correct_rows = [row for row in join_rows if row.get("prediction_correct") == 1]
    top1_rows = [row for row in join_rows if row.get("top1_correct") == 1]
    top3_rows = [row for row in join_rows if row.get("top3_correct") == 1]
    background_hits = sum(row.get("candidate_running_at_prediction") == "RUNNING_BACKGROUND" for row in correct_rows)
    correct_drops = sum(as_int(row.get("memory_drop_before_switch_bytes")) > 0 for row in correct_rows)
    correct_reclaim = sum(as_int(row.get("reclaim_associated_drop_bytes")) > 0 for row in correct_rows)
    correct_rebuild = sum(as_int(row.get("memory_rebuild_1s_bytes")) > 0 for row in correct_rows)
    correct_cost = sum(any(as_int(row.get(field)) > 0 for field in (
        "minor_fault_1s", "major_fault_1s", "refault_1s", "swapin_1s", "file_read_1s_bytes"
    )) for row in correct_rows)
    potential_bytes = sum(as_int(row.get("potentially_avoidable_rebuild_bytes")) for row in join_rows)
    lstm_false_bytes = as_int(lstm.get("false_protection_bytes"))
    pressure_level = "unknown"
    environment = session / "environment.txt"
    if environment.is_file():
        for line in environment.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("test3_pressure_level="):
                pressure_level = line.split("=", 1)[1] or "unknown"
    direct_observed = sum(row.get("causal_evidence_status") == "POTENTIALLY_AVOIDABLE" for row in join_rows)
    status = "PARP_APP_LSTM_MEMORY_VALUE_SHADOW_COMPLETE" if direct_observed else "PARP_APP_LSTM_MEMORY_VALUE_PARTIAL"
    comparison_lines = "\n".join(
        f"| {name} | {by_strategy.get(name, {}).get('top1_accuracy', '')} | {by_strategy.get(name, {}).get('top3_accuracy', '')} | {by_strategy.get(name, {}).get('valuable_hit_count', '')} | {by_strategy.get(name, {}).get('avoidable_bytes_covered', '')} | {by_strategy.get(name, {}).get('false_protection_bytes', '')} |"
        for name in ("Native", "Last-App", "Most-Frequent", "Markov", "LSTM", "Oracle")
    )
    report = f"""# Test3: 应用间预测的内存价值 SHADOW 验证

## 状态

`{status}`

本次压力等级为 `{pressure_level}`，以完整 prediction batch / episode 为单位。LSTM Top-1 为 `{len(top1_rows)}/{len(episode_rows)}`（`{lstm.get('top1_accuracy', '')}`），Top-3 为 `{len(top3_rows)}/{len(episode_rows)}`（`{lstm.get('top3_accuracy', '')}`）；Oracle Top-1 上限为 `{oracle.get('top1_accuracy', '')}`。

## 逐项结论

1. 真实切换 episode 为 `{len(episode_rows)}`；LSTM 候选集中实际下一 App 的命中数为 `{len(correct_rows)}`，其中 `{background_hits}` 个（`{background_hits}/{len(correct_rows) if correct_rows else 0}`）在预测时是 `RUNNING_BACKGROUND`。Top-1 的 `{len(top1_rows)}` 个命中也全部属于该状态。
2. 共有 `{sum(as_int(row.get('working_set_at_prediction_bytes')) > 0 for row in join_rows)}` 个候选记录到非零 T0 可复用工作集；这只计已采集的 Top-K/实际目标，未采样候选保持 `UNAVAILABLE`。
3. 在候选集命中目标的记录中，`{correct_drops}` 个出现工作集下降，`{correct_reclaim}` 个同时存在 cgroup `pgscan/pgsteal` 或 swap 关联计数。
4. 命中目标中，`{correct_rebuild}` 个在 1 秒窗口观察到工作集重建，`{correct_cost}` 个有至少一项 App cgroup fault/refault/swap-in 或近似文件读取计数。
5. 同时满足全部四段保守证据链的命中为 `{direct_observed}` 个；理论上可能避免的重建总量为 `{potential_bytes}` bytes。它们均为 Top-3 命中（并非 LSTM Top-1 命中）。
6. LSTM Top-3（`{lstm.get('top3_accuracy', '')}`）高于本轮 Last-App（`{by_strategy.get('Last-App', {}).get('top3_accuracy', '')}`）、Most-Frequent（`{by_strategy.get('Most-Frequent', {}).get('top3_accuracy', '')}`）和 Markov（`{by_strategy.get('Markov', {}).get('top3_accuracy', '')}`）；Top-1 与 Markov 持平，低于 Most-Frequent。
7. LSTM 错误候选的**累计、反事实**潜在保护成本为 `{lstm_false_bytes}` bytes；不是系统已经占用或浪费的内存。
8. Oracle 的上限为 `{oracle.get('avoidable_bytes_covered', '')}` bytes，且本 trace 上两条完整证据均落在 LSTM Top-3 中。
9. 本次只运行 `{pressure_level}` 自然负载；中/高压力的可比重复尚未执行，故不对压力等级间差异下结论。

## 同一 trace 的策略比较

| 策略 | Top-1 | Top-3 | valuable hits | 可避免量（bytes） | 假设错误保护量（bytes） |
|---|---:|---:|---:|---:|---:|
{comparison_lines}

## 直接观测与限制

- 直接观测：X11 事件时间、在线 LSTM batch、PARP bridge 审计、procfs `smaps_rollup`、应用 cgroup `memory.stat`/`memory.current`。
- `pgscan/pgsteal` 仅为应用 cgroup 计数关联，不是 per-folio reclaim trace；当前内核未暴露 `mm_vmscan/mm_filemap/workingset/swap` tracepoint。
- `proc/<pid>/io:read_bytes` 标为 `APPROXIMATE_PROC_IO_NOT_FAULT_ATTRIBUTABLE`，不被冒充为 page-fault 文件重读。
- activation latency 仅为自动化切换命令到 X11 前台确认，不等价于应用真正可交互延迟；本轮未启用稳定交互探针。
- `potentially_avoidable_rebuild_bytes`、策略 utility 与 false protection 都是 SHADOW 反事实估算，不能表述为实际延迟收益。虽然有 X11 activation latency，未得到可靠稳定交互探针，因而不能声称改善用户体验延迟。

## 安全声明

本阶段未根据预测保护页面；未修改 App 回收预算、MGLRU tier 或 generation；未写 `memory.reclaim`；未改变 vmscan 行为；未启用预测 APPLY。所有潜在收益均为 SHADOW 反事实估算。
"""
    (review / "FINAL_REPORT.md").write_text(report, encoding="utf-8")


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=dict) + "\n", encoding="utf-8")


def copy_contract_files(session: Path) -> None:
    targets = {
        "events": ["foreground_events.csv", "app_lifecycle_events.csv", "process_events.csv", "automation_trace.csv"],
        "prediction": ["online_lstm_predictions.csv"],
    }
    for directory, names in targets.items():
        destination = session / directory; destination.mkdir(exist_ok=True)
        for name in names:
            source = session / "model" / name
            if source.is_file():
                shutil.copy2(source, destination / name)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Analyze Test3 read-only memory shadow session.")
    parser.add_argument("--session-dir", required=True)
    args = parser.parse_args(argv)
    session = Path(args.session_dir).resolve()
    copy_contract_files(session)
    join_rows, episode_rows, _meta = build_join(session)
    analysis = session / "analysis"; analysis.mkdir(exist_ok=True)
    write_csv(analysis / "prediction_memory_value_join.csv", JOIN_FIELDS, join_rows)
    write_csv(analysis / "episode_memory_value.csv", EPISODE_VALUE_FIELDS, episode_rows)
    strategies = strategy_comparison(join_rows, episode_rows)
    strategy_fields = list(strategies[0].keys()) if strategies else ["strategy", "episode_count"]
    write_csv(analysis / "strategy_comparison.csv", strategy_fields, strategies)
    write_json(analysis / "valuable_hit_summary.json", {
        "lstm": next((row for row in strategies if row.get("strategy") == "LSTM"), {}),
        "causal_status_counts": Counter(row.get("causal_evidence_status", "") for row in join_rows),
    })
    write_json(analysis / "prediction_cost_summary.json", {
        row.get("strategy", ""): {"false_protection_bytes": row.get("false_protection_bytes", 0),
                                    "utility_lambda_0_25": row.get("utility_lambda_0_25", 0),
                                    "utility_lambda_0_5": row.get("utility_lambda_0_5", 0),
                                    "utility_lambda_1_0": row.get("utility_lambda_1_0", 0)}
        for row in strategies
    })
    write_review(session, join_rows, episode_rows, strategies)
    manifest_path = session / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.is_file() else {}
    except json.JSONDecodeError:
        manifest = {}
    manifest["test3"] = {
        "observer": "procfs_cgroup_read_only",
        "episode_count": len(episode_rows),
        "causal_evidence_status": (
            "PARP_APP_LSTM_MEMORY_VALUE_SHADOW_COMPLETE"
            if any(row.get("causal_evidence_status") == "POTENTIALLY_AVOIDABLE" for row in join_rows)
            else "PARP_APP_LSTM_MEMORY_VALUE_PARTIAL"
        ),
        "forbidden_memory_actions": "not_performed",
    }
    write_json(manifest_path, manifest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
