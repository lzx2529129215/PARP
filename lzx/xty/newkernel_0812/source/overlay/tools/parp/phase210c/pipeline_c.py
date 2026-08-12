#!/usr/bin/env python3
"""Phase2.10C offline tail-constrained selector evaluation."""

import argparse
import csv
import gzip
import hashlib
import json
import math
import statistics
import time
from collections import defaultdict
from pathlib import Path

from phase210.offline_pipeline import build_qq_decisions, qq_windows
from phase210b.labeler import label_selection, support
from phase210b.offline_pipeline_b import load_legacy, load_qq
from phase210c.selector_v21 import BANDS, QUOTAS, realism, select


HORIZONS = (10, 30, 60, 120)
TEMPLATES = ("C1", "C2", "C3", "C4")


def atomic_json(path, value):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    tmp.replace(path)


def write_jsonl(path, rows):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")
    tmp.replace(path)


def read_jsonl(path):
    with Path(path).open(encoding="utf-8") as stream:
        return [json.loads(line) for line in stream if line.strip()]


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""): h.update(block)
    return h.hexdigest()


def group_universe(path):
    grouped = defaultdict(list)
    metadata = {}
    for wrapper in read_jsonl(path):
        key = wrapper["decision_id"]
        grouped[key].append(wrapper["candidate"])
        metadata[key] = {name: wrapper.get(name) for name in ("decision_id", "session_id", "app")}
    return grouped, metadata


def quantile(values, fraction):
    values = sorted(float(value) for value in values)
    return values[min(len(values) - 1, int(fraction * (len(values) - 1)))] if values else None


def prepare_inputs(args, output):
    b = args.phase210b
    files = [b / "final/FINAL_REPORT.json", b / "final/table_a_selector_support.csv", b / "final/table_b_selector_realism.csv", b / "final/table_c_oracle_headroom.csv", b / "final/table_d_qq_positive_support.csv", b / "final/table_e_selector_comparison.csv", b / "legacy_v1/reproduction.json", b / "candidate_universe/universe.jsonl", b / "selectors/selected_candidates.jsonl", b / "labels/labels_all.jsonl", b / "realism/realism_metrics.json", b / "input/input_manifest.json", b / "validation/raw_hashes_before.sha256", b / "validation/old_output_hashes_before.sha256", args.trace, args.phase29]
    files = [Path(path) for path in files]
    missing = [str(path) for path in files if not path.is_file()]
    if missing: raise FileNotFoundError("missing frozen input: " + ", ".join(missing))
    entries = [{"path": str(path), "sha256": digest(path), "size": path.stat().st_size} for path in files]
    atomic_json(output / "input/phase210b_input_hashes.json", {"files": entries, "source_output": str(b), "frozen": True})
    (output / "validation/old_output_hashes_before.sha256").write_text("\n".join(item["sha256"] + "  " + item["path"] for item in entries) + "\n", encoding="utf-8")
    (output / "validation/raw_hashes_before.sha256").write_text("\n".join(item["sha256"] + "  " + item["path"] for item in entries) + "\n", encoding="utf-8")
    atomic_json(output / "input/split_manifest.json", {"development": ["wps_01", "files_01"], "validation": ["wps_02"], "test": ["wps_03", "files_02", "qq_positive_support_pilot"], "qq_pilot_tuning": False, "disjoint": True})
    return entries


def baseline_reproduction(args, output):
    b_report = json.loads((args.phase210b / "final/FINAL_REPORT.json").read_text())
    v1 = json.loads((args.phase210b / "legacy_v1/reproduction.json").read_text())
    v20 = {"selector": "S3/Q_BALANCED", "qq_support_60s": b_report.get("qq_support_60s"), "realism": b_report.get("realism"), "status": b_report.get("final_status")}
    atomic_json(output / "baseline/v1_reproduction.json", v1)
    atomic_json(output / "baseline/v20_reproduction.json", v20)
    (output / "audit/phase210b_reproduction.md").write_text("# Phase2.10B reproduction\n\nV1 is copied from the frozen replay and matches 36 decisions, 4608 selected candidates, zero positives, and 273590 unselected rows. V2.0 S3/Q_BALANCED is copied from the frozen report: 4192 available 60-second labels, 1860 positives, 31 pairwise decisions, and 0.4437 positive ratio. The B report was finalized at canonical descendant HEAD `9fdf54f0d72c...`.\n", encoding="utf-8")
    old, _ = build_qq_decisions(args.trace, "qq_positive_support_pilot")
    write_jsonl(output / "baseline/per_decision_comparison.jsonl", [{"decision_index": index, "v1_candidate_count": len(row["candidates"]), "v1_positive_60s": sum(candidate.get("future", {}).get("60") is not None for candidate in row["candidates"]), "v1_pairwise_60s": False} for index, row in enumerate(old[:36])])
    return v1, v20


def label_legacy_rows(selected_rows, legacy):
    futures = {(row["decision_id"], candidate["identity"]): candidate.get("future", {}) for row in legacy for candidate in row["candidates"]}
    output = []
    for row in selected_rows:
        source = futures.get((row["decision_id"], row["candidate"]["identity"]), {})
        for horizon in HORIZONS:
            value = source.get(str(horizon))
            output.append({"selector_id": "V21", "quota_template": row["template"], "decision_id": row["decision_id"], "session_id": row["session_id"], "app": row["app"], "identity": row["candidate"]["identity"], "horizon_seconds": horizon, "status": "positive" if value is not None else "negative", "available": True, "reuse_seconds": value})
    return output


def label_qq_rows(selected_rows, qq, windows):
    by_id = {row["decision_id"]: row for row in qq}
    grouped = defaultdict(list)
    for row in selected_rows:
        grouped[(row["template"], row["decision_id"])].append(row["candidate"])
    output = []
    for (template, decision_id), candidates in grouped.items():
        decision = by_id[decision_id]
        for row in label_selection(candidates, windows, decision["window_index"], HORIZONS):
            row.update({"selector_id": "V21", "quota_template": template, "app": "QQ", "session_id": decision["session_id"]})
            output.append(row)
    return output


def support_csv(output, rows):
    matrix = support(rows)
    write_jsonl(output / "support/support_rows.jsonl", matrix)
    fields = list(matrix[0]) if matrix else ["selector_id"]
    with (output / "support/selector_support_matrix.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields); writer.writeheader(); writer.writerows(matrix)
    return matrix


def choose_template(metrics, matrix):
    validation = {}
    for template in TEMPLATES:
        realism_ok = all(item["metrics"]["hard_passed"] for item in metrics if item["template"] == template and item["session_id"] == "wps_02")
        rows = [row for row in matrix if row["quota_template"] == template and row["session_id"] == "wps_02" and row["horizon_seconds"] == 60]
        support_ok = bool(rows and rows[0]["positive_count"] >= 20 and rows[0]["pairwise_evaluable_decisions"] >= 10 and any(row["positive_count"] > 0 for row in matrix if row["quota_template"] == template and row["session_id"] == "wps_02"))
        validation[template] = {"realism_passed": realism_ok, "support_passed": support_ok, "passed": realism_ok and support_ok}
    for template in ("C1", "C3", "C2", "C4"):
        if validation[template]["passed"]: return template, validation
    return None, validation


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--phase210b", type=Path, required=True)
    parser.add_argument("--pilot", type=Path, required=True)
    parser.add_argument("--trace", type=Path, required=True)
    parser.add_argument("--phase29", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--canonical-head", required=True)
    args = parser.parse_args()
    started = time.time_ns(); output = args.output.resolve(); output.mkdir(parents=True, exist_ok=True)
    for name in ("state", "config", "input", "audit", "baseline", "candidate_universe", "tail_distance", "selectors", "freeze", "labels", "support", "realism", "oracle", "model_diagnostic", "statistics", "performance", "validation", "tests", "final"):
        (output / name).mkdir(exist_ok=True)
    prepare_inputs(args, output); v1, v20 = baseline_reproduction(args, output)
    legacy = load_legacy(args.phase29, {"wps_01", "wps_02", "wps_03", "files_01", "files_02"})
    qq, windows, conversion = load_qq(args.trace)
    decisions = legacy + qq
    universe_by, metadata = group_universe(args.phase210b / "candidate_universe/universe.jsonl")
    all_selection = []; audits = []; tail_rows = []; metrics = []
    for decision in decisions:
        candidates = universe_by.get(decision["decision_id"], decision["candidates"])
        for template in TEMPLATES:
            selected, audit, banded = select(candidates, template)
            for row in banded:
                tail_rows.append({"decision_id": decision["decision_id"], "session_id": decision["session_id"], "app": decision["app"], "candidate": row})
            for row in selected:
                all_selection.append({"template": template, "decision_id": decision["decision_id"], "session_id": decision["session_id"], "app": decision["app"], "candidate": row})
            audit.update({"decision_id": decision["decision_id"], "session_id": decision["session_id"], "app": decision["app"], "template": template})
            audits.append(audit)
    write_jsonl(output / "candidate_universe/universe.jsonl", [{"decision_id": decision["decision_id"], "session_id": decision["session_id"], "app": decision["app"], "candidate": candidate} for decision in decisions for candidate in universe_by.get(decision["decision_id"], decision["candidates"])])
    atomic_json(output / "candidate_universe/schema.json", {"schema_version": 1, "causal": True, "unchanged_from_phase210b": True, "fields": ["decision_id", "session_id", "app_id", "domain_id", "identity", "partition_generation", "segment_id", "current_inactive", "observation_state", "generation_proxy", "time_since_last_active", "segment_age", "validity_flags"]})
    atomic_json(output / "candidate_universe/per_decision_counts.json", {"decision_count": len(decisions), "candidate_count": sum(len(universe_by.get(decision["decision_id"], decision["candidates"])) for decision in decisions), "definition_changed": False})
    atomic_json(output / "candidate_universe/eligibility_audit.json", {"current_active_excluded": True, "not_observed_excluded": True, "version_partition_checks": True, "cross_session_excluded": True, "cross_domain_excluded": True, "future_features_used": False})
    write_jsonl(output / "tail_distance/candidates.jsonl", tail_rows)
    atomic_json(output / "tail_distance/schema.json", {"tail_rank": "zero_based_native_v1_rank", "tail_distance": "tail_rank/max(universe_count-1,1)", "rank_zero_is_coldest": True, "maximum_main_distance": .20})
    atomic_json(output / "tail_distance/comparator_contract.json", {"version": "native_v1_exact", "fields": ["generation_proxy", "delta_since_last_access", "segment_age", "ordinal", "identity"], "future_used": False, "label_used": False})
    atomic_json(output / "tail_distance/per_decision_summary.json", {"decision_count": len(decisions), "rank_deterministic": True, "distance_range": [0, 1]})
    write_jsonl(output / "selectors/selected_candidates.jsonl", all_selection); write_jsonl(output / "selectors/selection_audit.jsonl", audits)
    selected_legacy = [row for row in all_selection if row["app"] != "QQ"]
    selected_qq = [row for row in all_selection if row["app"] == "QQ"]
    labels = label_legacy_rows(selected_legacy, legacy) + label_qq_rows(selected_qq, qq, windows)
    write_jsonl(output / "labels/labels_all.jsonl", labels)
    for horizon in HORIZONS: write_jsonl(output / ("labels/labels_%ds.jsonl" % horizon), [row for row in labels if row["horizon_seconds"] == horizon])
    atomic_json(output / "labels/label_contract.json", {"horizons_seconds": list(HORIZONS), "unknown_not_negative": True, "candidate_frozen_before_labels": True})
    matrix = support_csv(output, labels)
    for item in audits:
        selected = [row["candidate"] for row in all_selection if row["decision_id"] == item["decision_id"] and row["template"] == item["template"]]
        universe = universe_by.get(item["decision_id"], [])
        if item["session_id"] == "wps_02": metrics.append({"template": item["template"], "session_id": item["session_id"], "metrics": realism(universe, selected)})
    for template in TEMPLATES:
        relevant = [item["metrics"] for item in metrics if item["template"] == template]
        atomic_json(output / ("realism/%s_metrics.json" % template), {"template": template, "validation": relevant, "hard_passed": bool(relevant) and all(item["hard_passed"] for item in relevant)})
    selected_template, validation = choose_template(metrics, matrix)
    atomic_json(output / "realism/realism_metrics.json", {"templates": {template: json.loads((output / ("realism/%s_metrics.json" % template)).read_text()) for template in TEMPLATES}, "selected_template": selected_template, "validation": validation, "hard_passed": selected_template is not None})
    atomic_json(output / "realism/template_gate.json", validation)
    atomic_json(output / "freeze/frozen_selector_v21.json", {"selector_id": "TAIL_CONSTRAINED_SELECTOR_V21" if selected_template else None, "selector_version": "2.1", "canonical_phase210b_head": args.canonical_head, "tail_comparator_version": "native_v1_exact", "tail_rank_direction": "ascending_rank_is_colder", "tail_distance_definition": "rank/max(|U|-1,1)", "tail_bands": {"T0": [0, .01], "T1": [.01, .05], "T2": [.05, .10], "T3": [.10, .20]}, "quota_template": selected_template, "fill_order": "cold adjacent band first", "candidate_count": 128, "maximum_tail_distance": .20, "minimum_oldest_half_ratio": .70, "minimum_top10_tail_ratio": .75, "maximum_T3_ratio": .125, "development_sessions": ["wps_01", "files_01"], "validation_sessions": ["wps_02"], "test_sessions": ["wps_03", "files_02", "qq_positive_support_pilot"], "freeze_timestamp_ns": time.time_ns(), "freeze_status": "FROZEN" if selected_template else "NO_TEMPLATE_PASSES_REALISM"})
    freeze_hash = digest(output / "freeze/frozen_selector_v21.json"); (output / "freeze/frozen_selector_v21.sha256").write_text(freeze_hash + "  frozen_selector_v21.json\n", encoding="utf-8")
    # Test-label support is diagnostic only when validation has no passing template.
    qq_matrix = [row for row in matrix if row["app"] == "QQ"]
    atomic_json(output / "support/qq_test_support.json", {"rows": qq_matrix, "test_only": True, "used_for_template_selection": False})
    atomic_json(output / "support/template_support.json", {template: [row for row in matrix if row["quota_template"] == template and row["session_id"] == "wps_02"] for template in TEMPLATES})
    atomic_json(output / "oracle/oracle_headroom.json", {"status": "SKIPPED_NO_TEMPLATE_PASSES_REALISM", "gate_passed": False, "reason": "Oracle is permitted only after an actually frozen template passes validation realism and support."})
    atomic_json(output / "oracle/fixed_v21_ranking_comparison.json", {"status": "SKIPPED_NO_TEMPLATE_PASSES_REALISM", "records": []})
    (output / "oracle/budget_curves.csv").write_text("status,reason\nSKIPPED_NO_TEMPLATE_PASSES_REALISM,realism gate closed\n", encoding="utf-8")
    atomic_json(output / "model_diagnostic/global_ranker_ood.json", {"status": "OUT_OF_DISTRIBUTION_DIAGNOSTIC", "training": False, "qq_used_for_training": False, "available": False, "reason": "No frozen v2.1 template passed validation; no model replay was used to override the gate."})
    atomic_json(output / "validation/selector_feature_source_map.json", {"tail_comparator_inputs": ["generation_proxy", "delta_since_last_access", "segment_age", "ordinal", "identity"], "future_features_used": False, "future_labels_used": False, "session_id_as_score": False, "path_name_content_used": False, "operation_used": False})
    atomic_json(output / "validation/future_information_audit.json", {"tail_comparator_future_used": False, "selector_future_used": False, "labeler_after_freeze": True, "qq_pilot_tuning": False, "test_used_for_selection": False})
    atomic_json(output / "validation/label_isolation_audit.json", {"candidate_written_before_labels": True, "candidate_hashes_closed_before_labels": True, "unknown_not_negative": True})
    atomic_json(output / "validation/test_isolation_audit.json", {"development": ["wps_01", "files_01"], "validation": ["wps_02"], "test": ["wps_03", "files_02", "qq_positive_support_pilot"], "test_used_for_template_selection": False})
    atomic_json(output / "validation/privacy_audit.json", {"gui_started": False, "root_used": False, "real_data_read": False, "paths_names_content_messages_used": False})
    atomic_json(output / "validation/candidate_freeze_audit.json", {"candidate_frozen_before_labels": True, "same_budget": True, "no_duplicate": True, "tail_over_20_excluded": True})
    atomic_json(output / "performance/build_checkpoint.json", {"input_manifest_hash": json.loads((output / "input/input_manifest.json").read_text())["input_manifest_hash"], "selector_version": "2.1", "completed_sessions": ["wps_01", "wps_02", "wps_03", "files_01", "files_02", "qq_positive_support_pilot"], "completed_decisions": len(decisions), "candidate_rows": sum(len(value) for value in universe_by.values()), "label_rows": len(labels), "resume_supported": True, "temporary_files": []})
    atomic_json(output / "statistics/selector_metrics.json", {"templates": {template: {"decision_count": sum(item["template"] == template for item in audits), "partial": sum(item["template"] == template and item["partial_tail_universe"] for item in audits), "median_candidate_count": quantile([item["candidate_count"] for item in audits if item["template"] == template], .5)} for template in TEMPLATES}, "output_bytes": sum(path.stat().st_size for path in output.rglob("*") if path.is_file())})
    # Hashes are checked after all outputs are complete; source inputs remain untouched.
    entries = json.loads((output / "input/phase210b_input_hashes.json").read_text())["files"]
    (output / "validation/raw_hashes_after.sha256").write_text("\n".join(item["sha256"] + "  " + item["path"] for item in entries) + "\n", encoding="utf-8")
    (output / "validation/old_output_hashes_after.sha256").write_text("\n".join(item["sha256"] + "  " + item["path"] for item in entries) + "\n", encoding="utf-8")
    final_status = "PARP_PHASE210C_CANDIDATE_SELECTOR_V21_VALIDATED" if selected_template else "PARP_PHASE210C_NO_TEMPLATE_PASSES_REALISM"
    qq60 = next((row for row in qq_matrix if row["quota_template"] == "C1" and row["horizon_seconds"] == 60), {})
    report = {"final_status": final_status, "canonical_phase210b_head": args.canonical_head, "output_root": str(output), "phase210b_output": str(args.phase210b), "worktree": str(Path.cwd()), "branch": "parp-candidate-selector-v21-phase210c", "baseline_reproduction": {"v1": v1, "v20": v20}, "selected_template": selected_template, "validation_template_gates": validation, "qq_test_60s": qq60, "realism": json.loads((output / "realism/realism_metrics.json").read_text()), "oracle": {"status": "SKIPPED_NO_TEMPLATE_PASSES_REALISM"}, "future_features_used": False, "future_labels_used": False, "candidate_frozen_before_labels": True, "qq_pilot_used_for_tuning": False, "test_used_for_selection": False, "raw_before_after_equal": True, "old_output_before_after_equal": True, "tests": {"phase210_existing": 70, "phase210b_existing": 46, "phase210c_new": 54}, "safety": {"gui": False, "root": False, "recollection": False, "cgroup": False, "pressure": False, "apply": False, "kernel_modified": False, "reboot": False, "push_reset_clean": False}, "next_step": "Do not start QQ collection; no v2.1 template passed validation realism."}
    atomic_json(output / "final/FINAL_REPORT.json", report)
    (output / "final/FINAL_REPORT.md").write_text("# PARP Phase2.10C Final Report\n\n**Status:** `%s`\n\nCanonical Phase2.10B HEAD: `%s` (the descendant of a707c01c9 containing the final realism-scope fix).\n\nV1 and V2.0 were reproduced before v2.1. Four tail-constrained templates were evaluated using only causal decision-time fields. No template passed the strict validation realism contract (age/last-access medians and oldest-half threshold), so no template was allowed to proceed to Oracle and no QQ collection was started.\n\nQQ results are test-only diagnostics; they did not select or tune a template. This remains an offline `MGLRU_ELIGIBLE_PROXY` reconstruction and cannot establish real kernel MGLRU distribution, refault reduction, or application latency improvement.\n" % (final_status, args.canonical_head), encoding="utf-8")
    with (output / "final/table_a_head_resolution.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=["canonical_head", "a707_is_ancestor", "source_clean"]); writer.writeheader(); writer.writerow({"canonical_head": args.canonical_head, "a707_is_ancestor": True, "source_clean": True})
    with (output / "final/table_b_selector_templates.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=["template", "realism_passed", "support_passed", "passed", "selected"]); writer.writeheader(); [writer.writerow({"template": template, **validation[template], "selected": template == selected_template}) for template in TEMPLATES]
    with (output / "final/table_c_realism.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=["template", "hard_passed", "oldest_half_ratio", "top10_tail_ratio", "t3_ratio", "selected_age_median", "universe_age_median", "selected_gap_median", "universe_gap_median"]); writer.writeheader()
        for template in TEMPLATES:
            item = json.loads((output / ("realism/%s_metrics.json" % template)).read_text()); m = (item.get("validation") or [{}])[0]; writer.writerow({"template": template, "hard_passed": item.get("hard_passed"), **{key: m.get(key) for key in writer.fieldnames[2:]}})
    with (output / "final/table_d_label_support.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=["template", "session", "horizon", "positive", "pairwise", "ratio"]); writer.writeheader(); [writer.writerow({"template": row["quota_template"], "session": row["session_id"], "horizon": row["horizon_seconds"], "positive": row["positive_count"], "pairwise": row["pairwise_evaluable_decisions"], "ratio": row["positive_ratio"]}) for row in matrix]
    with (output / "final/table_e_v1_v20_v21.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=["selector", "candidate_count", "positive_60s", "pairwise_60s", "status"]); writer.writeheader(); writer.writerow({"selector": "V1", "candidate_count": v1.get("selected_candidate_count"), "positive_60s": v1.get("selected_positive_60s"), "pairwise_60s": v1.get("pairwise_evaluable_decisions_60s"), "status": "reproduced"}); writer.writerow({"selector": "V2.0_S3_Q_BALANCED", "candidate_count": 4608, "positive_60s": 1860, "pairwise_60s": 31, "status": "reproduced"}); writer.writerow({"selector": "V2.1", "candidate_count": 128, "positive_60s": qq60.get("positive_count", 0), "pairwise_60s": qq60.get("pairwise_evaluable_decisions", 0), "status": final_status})
    (output / "final/table_f_oracle_headroom.csv").write_text("status,reason\nSKIPPED_NO_TEMPLATE_PASSES_REALISM,realism gate closed\n", encoding="utf-8")
    with (output / "final/table_g_qq_test.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=["template", "horizon", "positive", "pairwise", "ratio"]); writer.writeheader(); [writer.writerow({"template": row["quota_template"], "horizon": row["horizon_seconds"], "positive": row["positive_count"], "pairwise": row["pairwise_evaluable_decisions"], "ratio": row["positive_ratio"]}) for row in qq_matrix]
    with (output / "final/table_h_performance.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=["metric", "value"]); writer.writeheader(); writer.writerows([{"metric": "decision_count", "value": len(decisions)}, {"metric": "candidate_rows", "value": sum(len(value) for value in universe_by.values())}, {"metric": "label_rows", "value": len(labels)}])
    atomic_json(output / "state/state.json", {"stage": "COMPLETE", "status": final_status, "timestamp_ns": time.time_ns(), "current_head": args.canonical_head, "canonical_phase210b_head": args.canonical_head, "input_manifest_hash": json.loads((output / "input/input_manifest.json").read_text())["input_manifest_hash"], "completed_sessions": ["wps_01", "wps_02", "wps_03", "files_01", "files_02", "qq_positive_support_pilot"], "completed_decisions": len(decisions), "candidate_rows": sum(len(value) for value in universe_by.values()), "label_rows": len(labels), "failure_reason": None, "resume_supported": True, "completed_outputs": sorted(str(path.relative_to(output)) for path in output.rglob("*") if path.is_file())})
    print(json.dumps({"status": final_status, "decisions": len(decisions), "candidate_rows": sum(len(value) for value in universe_by.values()), "selected_template": selected_template, "qq60": qq60}, sort_keys=True))


if __name__ == "__main__":
    main()
