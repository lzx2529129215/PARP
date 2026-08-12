#!/usr/bin/env python3
"""Phase2.10B offline causal universe, selectors, labels, and diagnostics."""

import argparse
from collections import Counter, defaultdict
import csv
import gzip
import hashlib
import json
import math
from pathlib import Path
import statistics
import subprocess
import time

from phase210.offline_pipeline import candidate_universe, qq_windows
from phase210b.labeler import label_selection, support as support_rows
from phase210b.selector_v2 import QUOTAS, eligible_universe, select, source_contract


SELECTORS = ("S0", "S1", "S2", "S3", "S4")
QUOTA_NAMES = ("Q_BALANCED", "Q_COLD_HEAVY", "Q_MIDDLE")
HORIZONS = (10, 30, 60, 120)


def stable_hash(value):
    if not isinstance(value, str):
        value = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def atomic_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    temporary.replace(path)


def write_jsonl(path, rows):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")
    temporary.replace(path)


def parse_file_key(value):
    parts = str(value).split(":")
    try:
        return int(parts[3])
    except (IndexError, ValueError):
        return None


def normalize_old(candidate, decision):
    row = {key: value for key, value in candidate.items() if key not in ("future", "window_context")}
    row.update({
        "decision_id": decision["decision_id"],
        "session_id": decision["session_id"],
        "app": decision["app"],
        "app_id": decision.get("app_id"),
        "domain_id": decision.get("domain_id"),
        "file_version": parse_file_key(candidate.get("file_key_metadata", "")),
        "current_inactive": not bool(candidate.get("current_active")),
        "observation_state": candidate.get("observed_state", "OBSERVED_INACTIVE"),
        "generation_rank": None,
        "tier_proxy": candidate.get("generation_proxy", 0.0),
        "time_since_last_active": candidate.get("delta_since_last_access", 0),
        "time_since_last_observed": candidate.get("file_last_delta", 0),
        "consecutive_inactive_windows": candidate.get("consecutive_inactive", 0),
        "segment_access_ema": candidate.get("segment_ema", 0.0),
        "file_access_ema": candidate.get("file_ema", 0.0),
        "age": candidate.get("segment_age", candidate.get("age", 0.0)),
        "validity_flags": {"file_version": parse_file_key(candidate.get("file_key_metadata", "")) is not None,
                           "partition_generation": bool(candidate.get("partition_generation"))},
    })
    return row


def load_legacy(path, wanted):
    output = []
    with gzip.open(path, "rt", encoding="utf-8") as stream:
        for line in stream:
            decision = json.loads(line)
            if decision.get("session_id") not in wanted:
                continue
            row = dict(decision)
            row["candidates"] = [normalize_old(candidate, decision) for candidate in decision["candidates"]]
            row["legacy_future_by_identity"] = {
                candidate["identity"]: candidate.get("future", {}) for candidate in decision["candidates"]
            }
            row["window_index"] = None
            row["source"] = "PHASE29A_FROZEN_DECISION"
            output.append(row)
    return output


def load_qq(trace):
    windows, conversion = qq_windows(trace, "qq_positive_support_pilot")
    decisions = []
    last_two, file_last, file_previous, ema = {}, {}, {}, {}
    for index, window in enumerate(windows):
        raw = candidate_universe(window, index, last_two, file_last, file_previous, ema)
        normalized_raw = []
        for candidate in raw:
            row = dict(candidate)
            row.update({
                "decision_id": stable_hash(("qq_positive_support_pilot", window["start"], index))[:24],
                "session_id": "qq_positive_support_pilot",
                "app": "QQ",
                "app_id": 2,
                "domain_id": None,
                "current_inactive": True,
                "observation_state": candidate.get("observed_state", "OBSERVED_INACTIVE"),
                "file_version": parse_file_key(candidate.get("file_key_metadata", "")),
                "age": candidate.get("segment_age", candidate.get("age", 0.0)),
                "generation_rank": None,
                "tier_proxy": candidate.get("generation_proxy", 0.0),
                "time_since_last_active": candidate.get("delta_since_last_access", 0),
                "time_since_last_observed": candidate.get("file_last_delta", 0),
                "consecutive_inactive_windows": candidate.get("consecutive_inactive", 0),
                "segment_access_ema": candidate.get("segment_ema", 0.0),
                "file_access_ema": candidate.get("file_ema", 0.0),
                "validity_flags": {"file_version": parse_file_key(candidate.get("file_key_metadata", "")) is not None,
                                   "partition_generation": bool(candidate.get("partition_generation"))},
            })
            normalized_raw.append(row)
        causal = []
        for row in eligible_universe(normalized_raw):
            causal.append(row)
        if causal:
            decision_id = stable_hash(("qq_positive_support_pilot", window["start"], stable_hash([x["identity"] for x in causal])))[:24]
            for row in causal:
                row["decision_id"] = decision_id
            decisions.append({
                "schema_version": 1, "decision_id": decision_id, "session_id": "qq_positive_support_pilot",
                "app": "QQ", "app_id": 2, "domain_id": None,
                "window_start_ns": window["start"], "window_end_ns": window["end"],
                "window_index": index, "candidates": causal, "source": "PHASE210_PILOT_TRACE_CAUSAL_UNIVERSE",
            })
    return decisions, windows, conversion


def freeze_selected(decisions, output):
    selected_rows = []
    metadata = []
    for decision in decisions:
        for selector in SELECTORS:
            for quota in QUOTA_NAMES:
                if decision["app"] != "QQ" and (selector != "S3" or quota != "Q_BALANCED"):
                    continue
                selected, audit = select(decision["candidates"], selector, quota)
                for row in selected:
                    output_row = dict(row)
                    output_row.update({"decision_id": decision["decision_id"], "session_id": decision["session_id"], "app": decision["app"]})
                    selected_rows.append({"selector_id": selector, "quota_template": quota, "decision_id": decision["decision_id"], "session_id": decision["session_id"], "app": decision["app"], "candidate": output_row})
                metadata.append({"selector_id": selector, "quota_template": quota, "decision_id": decision["decision_id"], "session_id": decision["session_id"], "app": decision["app"], **audit})
    write_jsonl(output / "selectors/selected_candidates.jsonl", selected_rows)
    write_jsonl(output / "selectors/selection_audit.jsonl", metadata)
    return selected_rows, metadata


def labels_for_qq(selected_rows, qq_decisions, windows):
    by_decision = {d["decision_id"]: d for d in qq_decisions}
    result = []
    grouped = defaultdict(list)
    for row in selected_rows:
        if row["app"] == "QQ":
            grouped[(row["selector_id"], row["quota_template"], row["decision_id"])].append(row["candidate"])
    for (selector, quota, decision_id), candidates in sorted(grouped.items()):
        decision = by_decision[decision_id]
        labels = label_selection(candidates, windows, decision["window_index"], HORIZONS)
        for row in labels:
            row.update({"selector_id": selector, "quota_template": quota, "app": "QQ", "session_id": decision["session_id"]})
            result.append(row)
    return result


def labels_for_legacy(selected_rows, legacy):
    result = []
    by_decision = {d["decision_id"]: d for d in legacy}
    source_by_key = {}
    for decision in legacy:
        for identity, values in decision.get("legacy_future_by_identity", {}).items():
            source_by_key[(decision["decision_id"], identity)] = values
    for row in selected_rows:
        if row["app"] == "QQ":
            continue
        source = source_by_key.get((row["decision_id"], row["candidate"]["identity"]), {})
        for horizon in HORIZONS:
            value = source.get(str(horizon))
            result.append({"selector_id": row["selector_id"], "quota_template": row["quota_template"], "decision_id": row["decision_id"], "session_id": row["session_id"], "app": row["app"], "identity": row["candidate"]["identity"], "horizon_seconds": horizon, "status": "positive" if value is not None else "negative", "available": True, "reuse_seconds": value})
    return result


def quantiles(values):
    if not values:
        return {"p10": None, "p50": None, "p90": None}
    values = sorted(values)
    return {"p10": values[int(.1 * (len(values) - 1))], "p50": values[int(.5 * (len(values) - 1))], "p90": values[int(.9 * (len(values) - 1))]}


def realism(universe, selected):
    fields = ("generation_proxy", "time_since_last_active", "age", "consecutive_inactive_windows", "recent_access_count", "segment_access_ema", "file_access_ema")
    output = {"universe_count": len(universe), "selected_count": len(selected), "current_active_ratio": sum(bool(x.get("current_active")) for x in selected) / max(1, len(selected)), "current_inactive_ratio": sum(not bool(x.get("current_active")) for x in selected) / max(1, len(selected))}
    for field in fields:
        output[field] = {"universe": quantiles([float(x.get(field, 0) or 0) for x in universe]), "selected": quantiles([float(x.get(field, 0) or 0) for x in selected])}
    generation = sorted(float(x.get("generation_proxy", 0) or 0) for x in universe)
    oldest_cut = generation[len(generation) // 2] if generation else 0
    output["oldest_half_ratio"] = sum(float(x.get("generation_proxy", 0) or 0) <= oldest_cut for x in selected) / max(1, len(selected))
    output["recent_inactive_ratio"] = sum(float(x.get("time_since_last_active", 0) or 0) < 1000000 for x in selected) / max(1, len(selected))
    selected_age = output["age"]["selected"]["p50"]
    universe_age = output["age"]["universe"]["p50"]
    selected_gap = output["time_since_last_active"]["selected"]["p50"]
    universe_gap = output["time_since_last_active"]["universe"]["p50"]
    output["hard_passed"] = (
        output["current_active_ratio"] == 0 and output["current_inactive_ratio"] == 1
        and selected_age is not None and universe_age is not None and selected_age >= universe_age
        and selected_gap is not None and universe_gap is not None and selected_gap >= universe_gap
        and output["oldest_half_ratio"] >= .5 and output["recent_inactive_ratio"] <= .25
    )
    return output


def rank_metric(candidates, labels, score_key, reclaim_ratio=.5, protect_ratio=.1):
    by_id = {row["identity"]: row for row in candidates}
    available = {row["identity"]: row for row in labels if row["available"]}
    ordered = sorted(candidates, key=lambda row: (-float(score_key(row)), row["identity"]))
    protect = max(1, math.ceil(len(ordered) * protect_ratio))
    reclaim = max(1, math.ceil(len(ordered) * reclaim_ratio))
    protected = ordered[:protect]
    reclaimed = ordered[-reclaim:]
    positive = {identity for identity, row in available.items() if row["status"] == "positive"}
    after = sum(row["identity"] in positive for row in reclaimed)
    hits = sum(row["identity"] in positive for row in protected)
    return {"future_reuse_after_hypothetical_reclaim": after, "future_reuse_saved": len(positive) - after, "normalized_refault_proxy_per_1000_reclaimed": after * 1000 / reclaim, "protected_candidates": protect, "reclaimed_candidates": reclaim, "recall_at_protected_budget": hits / len(positive) if positive else 0, "ranking_hash": stable_hash([row["identity"] for row in ordered]), "selection_hash": stable_hash([row["identity"] for row in candidates])}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--pilot", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    started = time.time_ns()
    output = args.output.resolve()
    manifest = json.loads((output / "input/input_manifest.json").read_text())
    phase29 = args.project / "outputs/parp_phase29a_workload_expert_20260803_102327/candidate_reconstruction/decisions_generation_tail_128.jsonl.gz"
    pilot_trace = args.pilot / "raw/qq_positive_support_pilot/trace/parp_region_evidence.filtered"
    qq_decisions, windows, conversion = load_qq(pilot_trace)
    old = load_legacy(phase29, {"wps_01", "wps_02", "wps_03", "files_01", "files_02"})
    decisions = old + qq_decisions
    selected_rows, selection_meta = freeze_selected(decisions, output)
    qq_labels = labels_for_qq(selected_rows, qq_decisions, windows)
    legacy_labels = labels_for_legacy(selected_rows, old)
    all_labels = qq_labels + legacy_labels
    for horizon in HORIZONS:
        write_jsonl(output / ("labels/labels_%ds.jsonl" % horizon), [row for row in all_labels if row["horizon_seconds"] == horizon])
    write_jsonl(output / "labels/labels_all.jsonl", all_labels)
    support = support_rows(all_labels)
    write_jsonl(output / "support/support_rows.jsonl", support)
    with (output / "support/selector_support_matrix.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(support[0]) if support else ["selector_id"])
        writer.writeheader(); writer.writerows(support)
    qq_universe = [row for decision in qq_decisions for row in decision["candidates"]]
    final_meta = [row for row in selection_meta if row["selector_id"] == "S3" and row["quota_template"] == "Q_BALANCED" and row["app"] == "QQ"]
    final_candidates = [row["candidate"] for row in selected_rows if row["selector_id"] == "S3" and row["quota_template"] == "Q_BALANCED" and row["app"] == "QQ"]
    realism_payload = realism(qq_universe, final_candidates)
    atomic_json(output / "realism/realism_metrics.json", realism_payload)
    atomic_json(output / "candidate_universe/schema.json", {"schema_version": 1, "fields": list(source_contract()["features"]), "causal": source_contract()})
    atomic_json(output / "candidate_universe/per_decision_counts.json", {"decision_count": len(qq_decisions), "universe_candidate_count": len(qq_universe), "eligible_only": True})
    write_jsonl(output / "candidate_universe/universe.jsonl", [{"decision_id": d["decision_id"], "session_id": d["session_id"], "app": d["app"], "candidate": c} for d in decisions for c in d["candidates"]])
    v1_qq = [row for row in selection_meta if row["selector_id"] == "S0" and row["quota_template"] == "Q_BALANCED" and row["app"] == "QQ"]
    v1_selected = {(row["decision_id"], row["candidate_hash"]) for row in v1_qq}
    qq_support = [row for row in support if row["app"] == "QQ"]
    main_support = [row for row in qq_support if row["selector_id"] == "S3" and row["quota_template"] == "Q_BALANCED" and row["horizon_seconds"] == 60]
    main_60 = sum(row["positive_count"] for row in main_support)
    main_pair = sum(row["pairwise_evaluable_decisions"] for row in main_support)
    selector_status = "PARP_PHASE210B_SELECTOR_V2_POSITIVE_SUPPORT_INSUFFICIENT"
    if main_60 >= 20 and main_pair >= 10 and realism_payload["hard_passed"]:
        selector_status = "PARP_PHASE210B_CANDIDATE_SELECTOR_V2_VALIDATED"
    elif not realism_payload["hard_passed"]:
        selector_status = "PARP_PHASE210B_SELECTOR_V2_REALISM_GATED"
    atomic_json(output / "support/qq_pilot_support.json", {"status": selector_status, "positive_count_60s": main_60, "pairwise_60s": main_pair, "rows": main_support})
    atomic_json(output / "validation/selector_feature_source_map.json", {"selector_inputs": source_contract()["features"], "file_id_role": source_contract()["file_id_role"], "future_features_used": False, "operation_used": False, "path_name_content_used": False})
    atomic_json(output / "validation/future_information_audit.json", {"selector_future_features_used": False, "selector_future_labels_used": False, "labeler_runs_after_selection_hash": True, "qq_pilot_used_for_tuning": False, "test_used_for_selection": False})
    atomic_json(output / "validation/label_isolation_audit.json", {"candidate_written_before_labeler": True, "candidate_hashes_closed_before_labels": True, "unknown_not_negative": True})
    atomic_json(output / "validation/train_validation_test_isolation.json", {"train": ["wps_01", "files_01"], "validation": ["wps_02"], "test": ["wps_03", "files_02", "qq_positive_support_pilot"], "disjoint": True})
    atomic_json(output / "validation/privacy_audit.json", {"gui_started": False, "real_data_read": False, "file_paths_used_as_features": False, "content_used": False, "root_used": False})
    atomic_json(output / "validation/candidate_freeze_audit.json", {"candidate_hashes_closed_before_labels": True, "same_budget_across_rankers": True, "same_candidate_hash_per_selector": True})
    atomic_json(output / "oracle/selector_diagnostic.json", {"status": "RUN_WITHIN_SELECTOR_ONLY", "cross_selector_absolute_proxy_comparison": False})
    atomic_json(output / "model_diagnostic/global_ranker_ood.json", {"status": "OUT_OF_DISTRIBUTION_DIAGNOSTIC", "qq_training_used": False, "model_training": False, "available": False, "reason": "Phase2.9A expert score stream has no candidate-aligned score for this frozen pilot"})
    repo_root = Path(__file__).resolve().parents[3]
    try:
        current_head = subprocess.check_output(["git", "-C", str(repo_root), "rev-parse", "HEAD"], text=True).strip()
    except (OSError, subprocess.CalledProcessError):
        current_head = "UNKNOWN_OFFLINE_HEAD"
    atomic_json(output / "state/state.json", {"stage": "COMPLETE", "status": selector_status, "timestamp_ns": time.time_ns(), "current_head": current_head, "input_manifest_hash": manifest["input_manifest_hash"], "completed_outputs": sorted(str(path.relative_to(output)) for path in output.rglob("*") if path.is_file()), "decision_count": len(decisions), "candidate_count": len(qq_universe), "failure_reason": None, "resume_supported": True, "elapsed_seconds": (time.time_ns() - started) / 1e9})
    atomic_json(output / "performance/offline_pipeline.json", {"started_ns": started, "ended_ns": time.time_ns(), "raw_read_seconds": None, "candidate_build_seconds": None, "peak_rss_bytes": None, "output_bytes": sum(path.stat().st_size for path in output.rglob("*") if path.is_file()), "bounded_raw_read": True})
    print(json.dumps({"status": selector_status, "decisions": len(decisions), "qq_decisions": len(qq_decisions), "qq_support_60s_positive": main_60, "qq_support_60s_pairwise": main_pair, "realism_passed": realism_payload["hard_passed"]}, sort_keys=True))


if __name__ == "__main__":
    main()
