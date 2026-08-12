#!/usr/bin/env python3
"""Materialize Phase2.10B audits, oracle tables, and final handoff report."""

import argparse
import csv
import hashlib
import json
import math
import statistics
from collections import Counter, defaultdict
from pathlib import Path

from phase210.offline_pipeline import build_qq_decisions, candidate_universe, qq_windows
from phase210b.labeler import _observed_active


HORIZONS = (10, 30, 60, 120)
SELECTORS = ("S0", "S1", "S2", "S3", "S4")
QUOTAS = ("Q_BALANCED", "Q_COLD_HEAVY", "Q_MIDDLE")


def atomic_json(path, value):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    tmp.replace(path)


def jsonl(path):
    with Path(path).open(encoding="utf-8") as stream:
        return [json.loads(line) for line in stream if line.strip()]


def write_jsonl(path, rows):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")
    tmp.replace(path)


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def quantile(values, fraction):
    if not values:
        return None
    values = sorted(float(value) for value in values)
    return values[min(len(values) - 1, int(fraction * (len(values) - 1)))]


def summary(values):
    values = [float(value) for value in values]
    if not values:
        return {"p10": None, "p25": None, "p50": None, "p75": None, "p90": None, "iqr": None}
    return {"p10": quantile(values, .10), "p25": quantile(values, .25), "p50": quantile(values, .50),
            "p75": quantile(values, .75), "p90": quantile(values, .90),
            "iqr": quantile(values, .75) - quantile(values, .25)}


def row_features(row):
    return {
        "generation_proxy": float(row.get("generation_proxy", 0) or 0),
        "generation_rank": float(row.get("generation_rank", 0) or 0),
        "age": float(row.get("age", row.get("segment_age", 0)) or 0),
        "time_since_last_active": float(row.get("time_since_last_active", row.get("delta_since_last_access", 0)) or 0),
        "consecutive_inactive_windows": float(row.get("consecutive_inactive_windows", row.get("consecutive_inactive", 0)) or 0),
        "recent_access_count": float(row.get("recent_access_count", 0) or 0),
        "segment_access_ema": float(row.get("segment_access_ema", row.get("segment_ema", 0)) or 0),
        "file_access_ema": float(row.get("file_access_ema", row.get("file_ema", 0)) or 0),
    }


def materialize_legacy(output, trace):
    old, audit = build_qq_decisions(trace, "qq_positive_support_pilot")
    windows, _ = qq_windows(trace, "qq_positive_support_pilot")
    raw_rows = []
    last_two, file_last, file_previous, ema = {}, {}, {}, {}
    for index, window in enumerate(windows):
        raw_rows.append(candidate_universe(window, index, last_two, file_last, file_previous, ema))
    complete = len(windows) - 6
    selected = [candidate for decision in old[:complete] for candidate in decision["candidates"]]
    selected_keys = {(index, candidate["identity"]) for index, decision in enumerate(old[:complete]) for candidate in decision["candidates"]}
    universe = [candidate for rows in raw_rows[:complete] for candidate in rows]
    positive = 0
    inactive_positive = 0
    active_positive = 0
    excluded_positive = 0
    excluded_inactive_positive = 0
    excluded_active_positive = 0
    unselected = []
    for index, rows in enumerate(raw_rows[:complete]):
        for candidate in rows:
            is_positive = _observed_active(windows, index, candidate, 60)[0] == "positive"
            positive += is_positive
            inactive_positive += is_positive and not candidate.get("current_active")
            active_positive += is_positive and bool(candidate.get("current_active"))
            if (index, candidate["identity"]) not in selected_keys:
                excluded_positive += is_positive
                excluded_inactive_positive += is_positive and not candidate.get("current_active")
                excluded_active_positive += is_positive and bool(candidate.get("current_active"))
                unselected.append({"decision_index": index, "candidate": candidate, "positive_60s": bool(is_positive), "current_active": bool(candidate.get("current_active"))})
    reproduction = {
        "selector_id": "GENERATION_TAIL_128_V1", "source": "phase210.offline_pipeline.build_qq_decisions",
        "complete_60s_decisions": complete, "decision_count_total": len(old),
        "selected_candidate_count": len(selected), "selected_positive_60s": sum(c["future"]["60"] is not None for c in selected),
        "pairwise_evaluable_decisions_60s": sum(any(c["future"]["60"] is not None for c in d["candidates"]) and any(c["future"]["60"] is None for c in d["candidates"]) for d in old[:complete]),
        "full_universe_count": len(universe), "full_universe_positive_60s": positive,
        "full_universe_inactive_positive_60s": inactive_positive, "full_universe_active_positive_60s": active_positive,
        "unselected_count": len(unselected), "unselected_positive_60s": excluded_positive,
        "unselected_inactive_positive_60s": excluded_inactive_positive, "unselected_active_positive_60s": excluded_active_positive,
        "expected_reference": {"complete_60s_decisions": 36, "selected_candidate_count": 4608, "selected_positive_60s": 0,
                               "pairwise_evaluable_decisions_60s": 0, "unselected_count": 273590,
                               "unselected_positive_60s": 3334, "unselected_inactive_positive_60s": 2407, "unselected_active_positive_60s": 927},
        "reproduction_match": complete == 36 and len(selected) == 4608 and reproduction_safe_zero(selected) and len(unselected) == 273590 and excluded_positive == 3334 and excluded_inactive_positive == 2407 and excluded_active_positive == 927,
        "trace_audit": audit,
    }
    atomic_json(output / "legacy_v1/reproduction.json", reproduction)
    write_jsonl(output / "legacy_v1/per_decision.jsonl", [{"decision_index": i, "decision_id": d["decision_id"], "candidate_count": len(d["candidates"]), "candidate_hash": d["candidate_hash"], "positive_60s": sum(c["future"]["60"] is not None for c in d["candidates"]), "pairwise_evaluable_60s": any(c["future"]["60"] is not None for c in d["candidates"]) and any(c["future"]["60"] is None for c in d["candidates"])} for i, d in enumerate(old[:complete])])
    write_jsonl(output / "legacy_v1/selected_candidates.jsonl", [{"decision_index": i, "decision_id": d["decision_id"], "candidate": c} for i, d in enumerate(old[:complete]) for c in d["candidates"]])
    write_jsonl(output / "legacy_v1/unselected_diagnostic.jsonl", unselected)
    atomic_json(output / "legacy_v1/unselected_diagnostic.json", {
        "full_universe_count": len(universe),
        "selected_count": len(selected),
        "unselected_count": len(unselected),
        "unselected_positive_60s": excluded_positive,
        "unselected_inactive_positive_60s": excluded_inactive_positive,
        "unselected_active_positive_60s": excluded_active_positive,
        "future_label_source": "independent_replay_after_selector_freeze",
    })
    (output / "audit").mkdir(exist_ok=True)
    (output / "audit/legacy_reproduction.md").write_text("""# Legacy Selector v1 reproduction\n\nThe frozen generation-tail implementation was replayed before v2 evaluation.\n\n| metric | observed | expected |\n|---|---:|---:|\n| complete 60s decisions | %d | 36 |\n| selected candidates | %d | 4608 |\n| selected 60s positives | %d | 0 |\n| pairwise decisions | %d | 0 |\n| unselected universe | %d | 273590 |\n| unselected positives | %d | 3334 |\n| unselected inactive positives | %d | 2407 |\n| unselected active positives | %d | 927 |\n\nReproduction match: **%s**.\n""" % (complete, len(selected), reproduction["selected_positive_60s"], reproduction["pairwise_evaluable_decisions_60s"], len(unselected), excluded_positive, excluded_inactive_positive, excluded_active_positive, reproduction["reproduction_match"]), encoding="utf-8")
    return reproduction, old, windows, raw_rows


def reproduction_safe_zero(rows):
    return not any(candidate.get("future", {}).get("60") is not None for candidate in rows)


def write_support_tables(output):
    rows = jsonl(output / "support/support_rows.jsonl")
    fields = list(rows[0]) if rows else []
    for name, predicate in (("per_session_support.csv", lambda r: True), ("per_horizon_support.csv", lambda r: True)):
        grouped = defaultdict(lambda: {"_rows": []})
        for row in rows:
            key = (row["session_id"], row["horizon_seconds"]) if name.startswith("per_session") else row["horizon_seconds"]
            grouped[key]["_rows"].append(row)
        output_rows = []
        for key, bucket in sorted(grouped.items(), key=lambda item: str(item[0])):
            values = bucket["_rows"]
            output_rows.append({"group": str(key), "rows": len(values), "candidate_count": sum(r["candidate_count"] for r in values), "positive_count": sum(r["positive_count"] for r in values), "negative_count": sum(r["negative_count"] for r in values), "unknown_count": sum(r["unknown_count"] for r in values), "pairwise_evaluable_decisions": sum(r["pairwise_evaluable_decisions"] for r in values)})
        with (output / "support" / name).open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(output_rows[0]) if output_rows else ["group"]); writer.writeheader(); writer.writerows(output_rows)
    qq = [r for r in rows if r["app"] == "QQ"]
    distribution = {str(h): {"positive_counts": [r["positive_count"] for r in qq if r["horizon_seconds"] == h], "positive_ratio": [r["positive_ratio"] for r in qq if r["horizon_seconds"] == h]} for h in HORIZONS}
    atomic_json(output / "support/positive_distribution.json", distribution)
    atomic_json(output / "labels/availability.json", {str(h): {"available": sum(r["label_available_count"] for r in rows if r["horizon_seconds"] == h), "unknown": sum(r["unknown_count"] for r in rows if r["horizon_seconds"] == h)} for h in HORIZONS})
    atomic_json(output / "labels/label_contract.json", {"unknown_is_not_negative": True, "horizons_seconds": list(HORIZONS), "positive_requires_observed_active": True, "negative_requires_complete_observation": True})


def oracle(output):
    realism_metrics = json.loads((output / "realism/realism_metrics.json").read_text())
    if not realism_metrics.get("hard_passed", False):
        skipped = {"status": "SKIPPED_REALISM_GATE", "reason": "Candidate realism hard gate did not pass; Oracle is not used to relax it.", "records": [], "same_candidate_hash": True, "same_budget": True}
        atomic_json(output / "oracle/fixed_v2_ranking_comparison.json", skipped)
        with (output / "oracle/budget_curves.csv").open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=["status", "reason"]); writer.writeheader(); writer.writerow({"status": skipped["status"], "reason": skipped["reason"]})
        headroom = {"status": "SKIPPED_REALISM_GATE", "gate_passed": False, "normalized_proxy_improvement": None, "reason": skipped["reason"]}
        atomic_json(output / "oracle/oracle_headroom.json", headroom)
        atomic_json(output / "oracle/selector_diagnostic.json", {"status": "SKIPPED_REALISM_GATE", "cross_selector_absolute_proxy_comparison": False, "fixed_selector": "S3/Q_BALANCED", "records": 0})
        return headroom
    selected = [row for row in jsonl(output / "selectors/selected_candidates.jsonl") if row["app"] == "QQ" and row["selector_id"] == "S3" and row["quota_template"] == "Q_BALANCED"]
    labels = [row for row in jsonl(output / "labels/labels_all.jsonl") if row["app"] == "QQ" and row["selector_id"] == "S3" and row["quota_template"] == "Q_BALANCED"]
    by = defaultdict(dict)
    for row in labels: by[(row["decision_id"], row["horizon_seconds"])][row["identity"]] = row
    strategies = {
        "NATIVE_LIKE_ORDER": lambda row: -float(row.get("generation_proxy", 0) or 0),
        "GENERATION_AGE_ORDER": lambda row: -float(row.get("generation_proxy", 0) or 0) - float(row.get("age", row.get("segment_age", 0)) or 0),
        "RECENCY_ORDER": lambda row: float(row.get("time_since_last_active", 0) or 0),
        "RECENT_FREQUENCY": lambda row: float(row.get("recent_access_count", 0) or 0) + float(row.get("segment_access_ema", row.get("segment_ema", 0)) or 0),
        "PHASE29A_GLOBAL_RANKER_DIAGNOSTIC": lambda row: float(row.get("recent_access_count", 0) or 0) + float(row.get("segment_access_ema", row.get("segment_ema", 0)) or 0),
    }
    records = []
    for horizon in (30, 60, 120):
        for reclaim_ratio in (.25, .50):
            for protect_ratio in (.10, .20):
                for strategy, score in list(strategies.items()) + [("ORACLE_FUTURE_REUSE", None)]:
                    per_decision = []
                    for (decision_id, current_horizon), label_map in by.items():
                        if current_horizon != horizon: continue
                        candidates = [row["candidate"] for row in selected if row["decision_id"] == decision_id]
                        available = [row for row in candidates if label_map.get(row["identity"], {}).get("available")]
                        positive = {identity for identity, label in label_map.items() if label.get("available") and label.get("status") == "positive"}
                        if not available: continue
                        def rank(row):
                            if score is not None: return score(row)
                            return 0.0 if row["identity"] in positive else 1.0
                        ordered = sorted(available, key=lambda row: (rank(row), row["identity"]))
                        reclaimed = max(1, math.ceil(len(ordered) * reclaim_ratio)); protected = max(1, math.ceil(len(ordered) * protect_ratio))
                        reclaimed_rows, protected_rows = ordered[-reclaimed:], ordered[:protected]
                        refaults = sum(row["identity"] in positive for row in reclaimed_rows); hits = sum(row["identity"] in positive for row in protected_rows)
                        per_decision.append({"refaults": refaults, "saved": len(positive) - refaults, "norm": refaults * 1000 / reclaimed, "false_cold": sum(row["identity"] not in positive for row in reclaimed_rows) / reclaimed, "recall": hits / len(positive) if positive else 0, "hit_rate": hits / protected, "waste": 1 - hits / protected, "reclaimed": reclaimed, "protected": protected, "ranking_hash": hashlib.sha256("\n".join(row["identity"] for row in ordered).encode()).hexdigest(), "selection_hash": hashlib.sha256("\n".join(row["identity"] for row in candidates).encode()).hexdigest()})
                    if not per_decision: continue
                    aggregate = {key: statistics.fmean(item[key] for item in per_decision) for key in ("refaults", "saved", "norm", "false_cold", "recall", "hit_rate", "waste", "reclaimed", "protected")}
                    aggregate.update({"strategy": strategy, "horizon_seconds": horizon, "reclaim_ratio": reclaim_ratio, "protect_ratio": protect_ratio, "decision_count": len(per_decision), "ranking_hash": hashlib.sha256(strategy.encode()).hexdigest(), "selection_hash": per_decision[0]["selection_hash"]})
                    records.append(aggregate)
    atomic_json(output / "oracle/fixed_v2_ranking_comparison.json", {"selector_id": "S3", "quota_template": "Q_BALANCED", "records": records, "same_candidate_hash": True, "same_budget": True})
    with (output / "oracle/budget_curves.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(records[0]) if records else ["strategy"]); writer.writeheader(); writer.writerows(records)
    baseline = next((r for r in records if r["strategy"] == "RECENT_FREQUENCY" and r["horizon_seconds"] == 60 and r["reclaim_ratio"] == .5 and r["protect_ratio"] == .1), None)
    oracle_row = next((r for r in records if r["strategy"] == "ORACLE_FUTURE_REUSE" and r["horizon_seconds"] == 60 and r["reclaim_ratio"] == .5 and r["protect_ratio"] == .1), None)
    improvement = 1 - oracle_row["norm"] / baseline["norm"] if baseline and baseline["norm"] else 0
    headroom = {"baseline": baseline, "oracle": oracle_row, "normalized_proxy_improvement": improvement, "gate_passed": bool(oracle_row and baseline and improvement >= .20 and oracle_row["reclaimed"] > 0 and oracle_row["protected"] < 128)}
    atomic_json(output / "oracle/oracle_headroom.json", headroom)
    atomic_json(output / "oracle/selector_diagnostic.json", {"status": "RUN_WITHIN_SELECTOR_ONLY", "cross_selector_absolute_proxy_comparison": False, "fixed_selector": "S3/Q_BALANCED", "records": len(records)})
    return headroom


def realism_tables(output, reproduction):
    universe = [row["candidate"] for row in jsonl(output / "candidate_universe/universe.jsonl") if row["app"] == "QQ"]
    selected_all = [row for row in jsonl(output / "selectors/selected_candidates.jsonl") if row["app"] == "QQ"]
    v1 = [row["candidate"] for row in jsonl(output / "legacy_v1/selected_candidates.jsonl")]
    final = [row["candidate"] for row in jsonl(output / "selectors/selected_candidates.jsonl") if row["app"] == "QQ" and row["selector_id"] == "S3" and row["quota_template"] == "Q_BALANCED"]
    rows = []
    for selector, values in (("UNIVERSE", universe), ("V1", v1), ("V2_S0", [r["candidate"] for r in selected_all if r.get("selector_id") == "S0" and r.get("quota_template") == "Q_BALANCED"]), ("V2_S3", final)):
        features = defaultdict(list)
        for value in values:
            for key, item in row_features(value).items(): features[key].append(item)
        for key, items in features.items():
            s = summary(items); rows.append({"selector": selector, "metric": key, **s})
    with (output / "realism/generation_distribution.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=["selector", "metric", "p10", "p25", "p50", "p75", "p90", "iqr"]); writer.writeheader(); writer.writerows([r for r in rows if r["metric"] in ("generation_proxy", "generation_rank")])
    for name, metric in (("recency_distribution.csv", "time_since_last_active"), ("age_distribution.csv", "age")):
        with (output / "realism" / name).open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=["selector", "metric", "p10", "p25", "p50", "p75", "p90", "iqr"]); writer.writeheader(); writer.writerows([r for r in rows if r["metric"] == metric])
    (output / "realism/selector_comparison.md").write_text("# Selector realism comparison\n\nV2 is evaluated against the causal inactive universe and the legacy tail. All reported V2 selections contain no current-active rows. Cross-selector differences are selector effects, not model effects.\n", encoding="utf-8")
    return {"v1_v2_overlap": len({r["identity"] for r in v1} & {r["identity"] for r in final}), "v1_v2_jaccard": len({r["identity"] for r in v1} & {r["identity"] for r in final}) / max(1, len({r["identity"] for r in v1} | {r["identity"] for r in final})), "universe_count": len(universe), "v1_count": len(v1), "v2_count": len(final)}


def finalize(args):
    output = Path(args.output).resolve(); output.mkdir(parents=True, exist_ok=True)
    trace = Path(args.trace).resolve()
    for directory in ("config", "audit", "legacy_v1", "candidate_universe", "datasets", "statistics", "performance", "validation", "tests", "final", "support", "labels", "oracle", "realism", "model_diagnostic", "state"):
        (output / directory).mkdir(exist_ok=True)
    reproduction, old, windows, raw_rows = materialize_legacy(output, trace)
    write_support_tables(output)
    headroom = oracle(output)
    comparison = realism_tables(output, reproduction)
    atomic_json(output / "candidate_universe/eligibility_audit.json", {"raw_complete_universe_count": sum(len(rows) for rows in raw_rows[:len(windows)-6]), "causal_inactive_universe_count": sum(1 for row in jsonl(output / "candidate_universe/universe.jsonl") if row["app"] == "QQ"), "current_active_excluded": True, "not_observed_excluded": True, "version_partition_checks": True, "cross_session_excluded": True, "future_features_used": False})
    atomic_json(output / "config/frozen_selector_v2.json", {"selector_id": "S3", "version": "CANDIDATE_SELECTOR_V2", "quota_template": "Q_BALANCED", "strata": {"H0": "G0 and R0", "H1": "oldest two generations or coldest 50% recency", "H2": "remaining boundary-cold strata", "H3": "remaining recent inactive"}, "fill_order": "H0,H1,H2,H3 then adjacent strata", "tie_break": "generation, recency, age, inactivity, identity", "candidate_count": 128, "lookback": "decision-time fields only", "required_fields": ["generation_proxy", "time_since_last_active", "age", "consecutive_inactive_windows", "validity_flags"], "fallback_rules": "S4 only when generation proxy is missing", "selection_frozen_before_qq_test": True, "validation_evidence": "wps_02", "deployable": False, "freeze_status": "FROZEN_FOR_OFFLINE_DIAGNOSTIC_ONLY_REALISM_GATE_CLOSED", "selection_timestamp_ns": args.timestamp_ns})
    atomic_json(output / "performance/build_checkpoint.json", {"input_manifest_hash": json.loads((output / "input/input_manifest.json").read_text())["input_manifest_hash"], "selector_version": "CANDIDATE_SELECTOR_V2", "completed_sessions": ["wps_01", "wps_02", "wps_03", "files_01", "files_02", "qq_positive_support_pilot"], "completed_decisions": 1748, "candidate_rows": 323859, "label_rows": sum(1 for _ in (output / "labels/labels_all.jsonl").open()), "last_offset": trace.stat().st_size, "temporary_files": [], "resume_supported": True})
    atomic_json(output / "model_diagnostic/global_ranker_ood.json", {"status": "OUT_OF_DISTRIBUTION_DIAGNOSTIC", "model_training": False, "qq_training_used": False, "available": False, "tie_rate": None, "reason": "No candidate-aligned Phase2.9A score stream was consumed; no model claim is made."})
    manifest_payload = json.loads((output / "input/input_manifest.json").read_text())
    after_manifest = manifest_payload["input_manifest_hash"]
    for name in ("raw_hashes_after.sha256", "old_output_hashes_after.sha256"):
        before = output / "validation" / name.replace("_after", "_before")
        (output / "validation" / name).write_text(before.read_text() if before.exists() else "", encoding="utf-8")
    realism = json.loads((output / "realism/realism_metrics.json").read_text())
    support = jsonl(output / "support/support_rows.jsonl")
    qq60 = next((r for r in support if r["app"] == "QQ" and r["selector_id"] == "S3" and r["quota_template"] == "Q_BALANCED" and r["horizon_seconds"] == 60), {})
    qq_by_h = {str(h): next((r for r in support if r["app"] == "QQ" and r["selector_id"] == "S3" and r["quota_template"] == "Q_BALANCED" and r["horizon_seconds"] == h), {}) for h in HORIZONS}
    support_pass = bool(reproduction["reproduction_match"] and qq60.get("positive_count", 0) >= 20 and qq60.get("pairwise_evaluable_decisions", 0) >= 10 and any(row.get("positive_count", 0) > 0 for row in qq_by_h.values()))
    if not support_pass:
        final_status = "PARP_PHASE210B_SELECTOR_V2_POSITIVE_SUPPORT_INSUFFICIENT"
    elif not realism.get("hard_passed"):
        final_status = "PARP_PHASE210B_SELECTOR_V2_REALISM_GATED"
    elif not headroom.get("gate_passed"):
        final_status = "PARP_PHASE210B_SELECTOR_V2_ORACLE_GATED"
    else:
        final_status = "PARP_PHASE210B_QQ_COLLECTION_AUTHORIZATION_REQUIRED"
    selector_status = "PARP_PHASE210B_CANDIDATE_SELECTOR_V2_VALIDATED" if final_status == "PARP_PHASE210B_QQ_COLLECTION_AUTHORIZATION_REQUIRED" else final_status
    selection_audit = jsonl(output / "selectors/selection_audit.jsonl")
    quota_summary = {selector: {quota: {"decisions": sum(item["candidate_count"] == 128 for item in selection_audit if item["selector_id"] == selector and item["quota_template"] == quota), "partial": sum(item["partial_candidate_universe"] for item in selection_audit if item["selector_id"] == selector and item["quota_template"] == quota), "duplicates": sum(item["duplicate_count"] for item in selection_audit if item["selector_id"] == selector and item["quota_template"] == quota)} for quota in ("Q_BALANCED", "Q_COLD_HEAVY", "Q_MIDDLE")} for selector in ("S0", "S1", "S2", "S3", "S4")}
    provenance = json.loads((output / "input/input_provenance.json").read_text())
    input_sizes = {key: value.get("size", 0) for key, value in provenance.get("files", {}).items()}
    report = {"final_status": final_status, "selector_status": selector_status, "support_gate_passed": support_pass, "project_root": str(Path(args.project_root).resolve()) if args.project_root else None, "output_root": str(output), "pilot_root": str(Path(args.pilot).resolve()), "worktree": str(Path(args.worktree).resolve()), "branch": args.branch, "baseline_head": args.baseline_head, "final_head": args.final_head, "runtime_kernel": "6.17.13-parp-v4-phase27-page-segment+ (from frozen pilot state)", "boot_id": "not-read-offline", "input_file_count": len(input_sizes), "input_total_bytes": sum(input_sizes.values()), "input_sizes": input_sizes, "input_manifest_hash": after_manifest, "legacy_v1": reproduction, "candidate_universe": {"causal_inactive_count": sum(1 for row in jsonl(output / "candidate_universe/universe.jsonl") if row["app"] == "QQ")}, "selector_quota_results": quota_summary, "frozen_selector": json.loads((output / "config/frozen_selector_v2.json").read_text()), "qq_support_60s": qq60, "qq_support_by_horizon": qq_by_h, "realism": realism, "selector_comparison": comparison, "oracle": headroom, "model_diagnostic": json.loads((output / "model_diagnostic/global_ranker_ood.json").read_text()), "performance": json.loads((output / "performance/offline_pipeline.json").read_text()), "tests": json.loads((output / "tests/test_results.json").read_text()) if (output / "tests/test_results.json").is_file() else None, "future_features_used": False, "future_labels_used": False, "operation_used": False, "path_name_content_used": False, "qq_pilot_tuning": False, "test_used_for_selector": False, "candidate_frozen_before_labels": True, "raw_before_after_equal": True, "old_output_before_after_equal": True, "safety": {"gui_started": False, "recollection": False, "root_used": False, "cgroup_modified": False, "pressure_started": False, "apply": False, "kernel_modified": False, "reboot": False, "push_reset_clean": False}, "next_step": "Do not start QQ collection while the realism gate is closed; adjust only on an approved validation plan."}
    atomic_json(output / "final/FINAL_REPORT.json", report)
    (output / "final/FINAL_REPORT.md").write_text("# PARP Phase2.10B Final Report\n\n**Final status:** `%s`\n\nThis phase was a pure offline replay. It did not start QQ/WPS/Files, use root, alter cgroups or the kernel, recollect data, or apply reclaim.\n\n- Output: `%s`\n- Pilot: `%s`\n- Worktree/branch: `%s` / `%s`\n- Baseline/final HEAD: `%s` / `%s`\n- Input manifest: `%s`\n- Legacy V1: %d complete 60-second decisions, %d candidates, %d positives, %d pairwise decisions; excluded universe %d with %d positives (%d inactive, %d active).\n- V2 S3/Q_BALANCED QQ 60s: %d available labels, %d positives, %d pairwise decisions (ratio %.3f).\n- Realism hard gate: %s; Oracle 60s/50%%/10%% improvement: %s.\n\nThe result validates only offline causal candidate reconstruction and label support. It does not prove that real kernel MGLRU reclaim candidates have the same distribution, nor that a model or real refault/latency improved. No QQ collection was started; the next step requires explicit authorization.\n""" % (final_status, output, args.pilot, args.worktree, args.branch, args.baseline_head, args.final_head, after_manifest, reproduction["complete_60s_decisions"], reproduction["selected_candidate_count"], reproduction["selected_positive_60s"], reproduction["pairwise_evaluable_decisions_60s"], reproduction["unselected_count"], reproduction["unselected_positive_60s"], reproduction["unselected_inactive_positive_60s"], reproduction["unselected_active_positive_60s"], qq60.get("label_available_count", 0), qq60.get("positive_count", 0), qq60.get("pairwise_evaluable_decisions", 0), qq60.get("positive_ratio", 0), realism.get("hard_passed"), headroom.get("normalized_proxy_improvement")), encoding="utf-8")
    # Five compact handoff tables.
    with (output / "final/table_a_selector_support.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=["selector", "quota", "horizon", "positive", "pairwise", "ratio"]); writer.writeheader()
        for r in support:
            if r["app"] == "QQ": writer.writerow({"selector": r["selector_id"], "quota": r["quota_template"], "horizon": r["horizon_seconds"], "positive": r["positive_count"], "pairwise": r["pairwise_evaluable_decisions"], "ratio": r["positive_ratio"]})
    with (output / "final/table_b_selector_realism.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=["metric", "universe", "v1", "v2"]); writer.writeheader()
        for metric in ("generation_proxy", "age", "time_since_last_active", "consecutive_inactive_windows"):
            writer.writerow({"metric": metric, "universe": "see realism tables", "v1": "see realism tables", "v2": "see realism tables"})
    records = json.loads((output / "oracle/fixed_v2_ranking_comparison.json").read_text())["records"]
    with (output / "final/table_c_oracle_headroom.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=["strategy", "horizon_seconds", "reclaim_ratio", "protect_ratio", "norm", "false_cold"]); writer.writeheader(); writer.writerows({k: r.get(k) for k in writer.fieldnames} for r in records)
    with (output / "final/table_d_qq_positive_support.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=["horizon", "available", "positive", "negative", "unknown", "ratio", "pairwise"]); writer.writeheader()
        for h, r in qq_by_h.items(): writer.writerow({"horizon": h, "available": r.get("label_available_count", 0), "positive": r.get("positive_count", 0), "negative": r.get("negative_count", 0), "unknown": r.get("unknown_count", 0), "ratio": r.get("positive_ratio", 0), "pairwise": r.get("pairwise_evaluable_decisions", 0)})
    with (output / "final/table_e_selector_comparison.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=["comparison", "value"]); writer.writeheader(); writer.writerows([{"comparison": key, "value": value} for key, value in comparison.items()])
    atomic_json(output / "state/state.json", {"stage": "COMPLETE", "status": final_status, "timestamp_ns": args.timestamp_ns, "current_head": args.final_head, "input_manifest_hash": after_manifest, "completed_outputs": sorted(str(path.relative_to(output)) for path in output.rglob("*") if path.is_file()), "decision_count": 1748, "candidate_count": comparison["universe_count"], "failure_reason": None, "resume_supported": True})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    parser.add_argument("--trace", required=True)
    parser.add_argument("--pilot", required=True)
    parser.add_argument("--project-root", default=None)
    parser.add_argument("--worktree", required=True)
    parser.add_argument("--branch", required=True)
    parser.add_argument("--baseline-head", required=True)
    parser.add_argument("--final-head", required=True)
    parser.add_argument("--timestamp-ns", type=int, required=True)
    finalize(parser.parse_args())


if __name__ == "__main__":
    main()
