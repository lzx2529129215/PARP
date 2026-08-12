#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0
"""Render the Phase-E controlled experiment matrix as a non-executable plan."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Sequence

try:
    from .contracts import ContractError, read_json, reject_live_path, write_json
except ImportError:  # Direct execution from this directory.
    from contracts import ContractError, read_json, reject_live_path, write_json  # type: ignore


FORBIDDEN_EXECUTION_KEYS = frozenset((
    "command", "commands", "argv", "shell", "subprocess", "execute",
    "apply_now", "pressure_command", "cgroup_write",
))


def _walk_keys(value: object) -> Iterable[str]:
    if isinstance(value, Mapping):
        for key, child in value.items():
            yield str(key)
            yield from _walk_keys(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_keys(child)


def validate_manifest(manifest: Mapping[str, object]) -> None:
    required = (
        "schema_version", "experiment_id", "phase", "status", "safety",
        "collection", "session_split", "global_model_ablations",
        "ranking", "probability_classifier_ablation",
        "policy_ablations",
        "experiment_groups", "pressure_levels", "workloads", "repetitions",
        "gates", "artifacts",
    )
    missing = [name for name in required if name not in manifest]
    if missing:
        raise ContractError("manifest missing: %s" % ", ".join(missing))
    if manifest["schema_version"] != 1 or manifest["phase"] != "E":
        raise ContractError("only the Phase-E v1 manifest is supported")
    if manifest["status"] != "PARP_EFFECTIVE_TIER_LIVE_AUTH_REQUIRED":
        raise ContractError("Phase E must stop at the live authorization gate")

    safety = manifest["safety"]
    if not isinstance(safety, Mapping):
        raise ContractError("safety must be an object")
    expected = {
        "harness_capability": "PLAN_ONLY",
        "dry_run_default": True,
        "live_reads_executed": False,
        "cgroup_writes_authorized": False,
        "pressure_authorized": False,
        "apply_authorized": False,
        "kernel_install_authorized": False,
        "reboot_authorized": False,
    }
    for name, value in expected.items():
        if safety.get(name) != value:
            raise ContractError("unsafe manifest safety.%s" % name)

    collection = manifest["collection"]
    split = manifest["session_split"]
    if not isinstance(collection, Mapping) or not isinstance(split, Mapping):
        raise ContractError("collection and session_split must be objects")
    if collection.get("candidate_scope") != "ALL_NATIVE_TIER_GATE_FOLIOS":
        raise ContractError("experiment must collect the full tier-gate set")
    if collection.get("shadow_behavior") != "NATIVE_ONLY":
        raise ContractError("SHADOW must preserve Native behavior")
    if collection.get("label_semantics") != "FUTURE_REAL_ACCESS_NOT_REFAULT":
        raise ContractError("future access must not be represented as refault")
    if collection.get("label_windows") != ["100ms", "500ms", "1s", "5s"]:
        raise ContractError("all four future reuse windows are required")
    if not collection.get("trace_lost_required"):
        raise ContractError("trace_lost must be measured")
    if split.get("unit") != "session" or not split.get(
            "random_page_row_split_forbidden"):
        raise ContractError("training splits must be session-only")

    ranking = manifest["ranking"]
    if not isinstance(ranking, Mapping):
        raise ContractError("ranking must be an object")
    expected_ranking = {
        "primary_task": "pairwise_next-reuse_ranking",
        "score_semantics": "higher_score_means_earlier_next_real_access",
        "ranking_horizon_ns": 5_000_000_000,
        "default_tie_margin_ns": 10_000_000,
        "tie_margin_ablations_ns": [0, 10_000_000, 50_000_000],
        "pair_cap_per_group": 64,
        "pair_cap_ablations_per_group": [32, 64, 128],
        "pair_sampling_seed": "parp-rank-pairs-v1",
        "fallback_time_window_ns": 100_000_000,
        "primary_group": ["session_id", "batch_id", "page_type"],
        "fallback_group": ["session_id", "reclaim_epoch", "page_type"],
        "cross_session_pairs_forbidden": True,
        "runtime_pairwise_comparison": False,
        "runtime_candidate_sorting": False,
        "runtime_sigmoid": False,
    }
    for name, value in expected_ranking.items():
        if ranking.get(name) != value:
            raise ContractError("invalid ranking.%s" % name)

    ablations = manifest["global_model_ablations"]
    if not isinstance(ablations, list):
        raise ContractError("global_model_ablations must be an array")
    expected_ids = {
        "rank_base",
        "rank_plus_native_tier",
        "rank_plus_native_tier_and_tier_idx",
        "recency_only_rank",
        "recent_frequency_rank",
    }
    actual_ids = set()
    for item in ablations:
        if not isinstance(item, Mapping):
            raise ContractError("ablation entries must be objects")
        if item.get("model_name") != "GLOBAL_REUSE_MODEL":
            raise ContractError("App/workload model routing is forbidden")
        actual_ids.add(item.get("id"))
        features = item.get("features")
        if not isinstance(features, list):
            raise ContractError("ablation features must be an array")
        prohibited = {"app", "app_id", "session_id", "workload",
                      "future_access", "future_label"}
        if prohibited.intersection(features):
            raise ContractError("ablation contains routed or future features")
    if actual_ids != expected_ids:
        raise ContractError("the five required GLOBAL rank ablations are mandatory")

    probability = manifest["probability_classifier_ablation"]
    if not isinstance(probability, Mapping) or \
            probability.get("status") != "NOT_IMPLEMENTED" or \
            probability.get("legacy_available") != "LEGACY_1S_LOG_ODDS" or \
            probability.get("probability_model_mainline") is not False or \
            probability.get("product_q15_calibrated") is not False:
        raise ContractError("probability classifier must remain an optional ablation")

    policy = manifest["policy_ablations"]
    if not isinstance(policy, Mapping):
        raise ContractError("policy_ablations must be an object")
    if policy.get("max_upgrade_tiers_apply") != [1, 2]:
        raise ContractError("APPLY max_upgrade_tiers must be limited to +1/+2")
    plus3 = policy.get("experimental_plus3")
    if not isinstance(plus3, Mapping) or \
            plus3.get("max_upgrade_tiers") != [3] or \
            plus3.get("allowed_modes") != [
                "SHADOW_EFFECTIVE_TIER", "ORACLE_OFFLINE_ONLY"] or \
            plus3.get("apply_supported") is not False:
        raise ContractError(
            "experimental +3 must remain SHADOW/ORACLE offline-only")

    groups = manifest["experiment_groups"]
    if not isinstance(groups, list) or len(groups) < 8:
        raise ContractError("all eight controlled groups are required")
    for group in groups:
        if not isinstance(group, Mapping) or group.get("execution") != "PLAN_ONLY":
            raise ContractError("experiment groups may only be planned")

    dangerous_keys = FORBIDDEN_EXECUTION_KEYS.intersection(_walk_keys(manifest))
    if dangerous_keys:
        raise ContractError("manifest contains executable keys: %s" %
                            ", ".join(sorted(dangerous_keys)))


def build_plan(manifest: Mapping[str, object]) -> Dict[str, object]:
    """Expand cells without executing or constructing shell commands."""

    validate_manifest(manifest)
    groups = manifest["experiment_groups"]
    pressures = manifest["pressure_levels"]
    workloads = manifest["workloads"]
    repetitions = manifest["repetitions"]
    assert isinstance(groups, list) and isinstance(pressures, list)
    assert isinstance(workloads, list) and isinstance(repetitions, Mapping)
    minimum = int(repetitions["minimum"])
    cells: List[Dict[str, object]] = []
    for group in groups:
        assert isinstance(group, Mapping)
        if group["mode"] == "ORACLE_OFFLINE_ONLY":
            cells.append({
                "group": group["id"],
                "mode": group["mode"],
                "workload": "MERGED_OFFLINE_DATA",
                "pressure_level": "OFFLINE_ONLY",
                "repetition": 1,
                "execution_status": "NOT_EXECUTED_PLAN_ONLY",
                "authorization_required": False,
            })
            continue
        for pressure in pressures:
            assert isinstance(pressure, Mapping)
            for workload in workloads:
                for repetition in range(1, minimum + 1):
                    cells.append({
                        "group": group["id"],
                        "mode": group["mode"],
                        "workload": workload,
                        "pressure_level": pressure["id"],
                        "repetition": repetition,
                        "execution_status": "NOT_EXECUTED_PLAN_ONLY",
                        "authorization_required": True,
                    })
    return {
        "schema_version": 1,
        "experiment_id": manifest["experiment_id"],
        "phase": "E",
        "status": "PARP_EFFECTIVE_TIER_LIVE_AUTH_REQUIRED",
        "generated_plan_only": True,
        "runtime_actions_executed": 0,
        "cgroup_writes_executed": 0,
        "pressure_actions_executed": 0,
        "apply_actions_executed": 0,
        "kernel_install_actions_executed": 0,
        "reboot_actions_executed": 0,
        "planned_cells": len(cells),
        "cells": cells,
        "authorization_boundaries": [
            {
                "next_phase": "F",
                "purpose": "live SHADOW and GLOBAL model training",
                "state": "EXPLICIT_AUTHORIZATION_REQUIRED",
            },
            {
                "next_phase": "G",
                "purpose": "protect-only live APPLY",
                "state": "SEPARATE_EXPLICIT_AUTHORIZATION_REQUIRED",
            },
            {
                "next_phase": "H",
                "purpose": "bidirectional live APPLY after all gates",
                "state": "THIRD_EXPLICIT_AUTHORIZATION_REQUIRED",
            },
        ],
    }


def checklist_markdown(manifest: Mapping[str, object],
                       plan: Mapping[str, object]) -> str:
    """Return a human checklist containing descriptions, never commands."""

    lines = [
        "# PARP effective-tier controlled experiment checklist",
        "",
        "Status: `PARP_EFFECTIVE_TIER_LIVE_AUTH_REQUIRED`",
        "",
        "This file is a plan, not an execution script. No live interface was "
        "read or written while generating it.",
        "",
        "## Phase-E offline prerequisites",
        "",
        "- [ ] Every exported candidate reached the native tier gate.",
        "- [ ] Candidate count matches the independently measured tier-gate counter.",
        "- [ ] Trace loss has before/after provenance for every session.",
        "- [ ] Future labels use only real access and cover 100ms/500ms/1s/5s.",
        "- [ ] Whole sessions, rather than page rows, are assigned to splits.",
        "- [ ] Five GLOBAL rank ablations are reported per App and page type.",
        "- [ ] Session splits precede deterministic bounded pair construction.",
        "- [ ] The 5 s ranking tail is observed after the last candidate.",
        "- [ ] Four quadrants and page-weighted upgrade/downgrade metrics are present.",
        "- [ ] Score, lru_lock, reclaim, efficiency, true refault, and App latency schemas are populated.",
        "",
        "## Runtime authorization boundaries",
        "",
        "- [ ] Obtain explicit Phase-F authorization before any live SHADOW activity.",
        "- [ ] Obtain separate Phase-G authorization before protect-only APPLY.",
        "- [ ] Pass downgrade gates and obtain third authorization before bidirectional APPLY.",
        "- [ ] Treat cgroup changes, pressure generation, install, and reboot as unauthorized.",
        "",
        "## Planned matrix",
        "",
        "- Planned cells: %s" % plan["planned_cells"],
        "- Minimum repetitions per live cell: %s" %
        manifest["repetitions"]["minimum"],  # type: ignore[index]
        "- Workloads: %s" % ", ".join(manifest["workloads"]),  # type: ignore[arg-type]
        "- Every live cell remains `NOT_EXECUTED_PLAN_ONLY`.",
        "",
        "## Stop conditions",
        "",
    ]
    for condition in manifest.get("stop_conditions", []):
        lines.append("- [ ] Stop on %s." % condition)
    lines.extend((
        "",
        "## Required statistical report",
        "",
        "- [ ] Median, mean, P95, P99, paired difference, and 95% bootstrap CI.",
        "- [ ] Upgrade hit/waste and downgrade mistake/cold precision by window.",
        "- [ ] OFF/SHADOW/PROTECT_ONLY/BIDIRECTIONAL/RANDOM/RECENCY lock and reclaim tails.",
        "- [ ] True workingset refault counters kept distinct from future access labels.",
        "- [ ] App P50/P95/P99, duration, stalls, timeouts, and failures.",
        "",
    ))
    return "\n".join(lines)


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Render a non-executable effective-tier experiment plan")
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    try:
        reject_live_path(args.manifest)
        reject_live_path(args.output_dir)
        raw = read_json(args.manifest)
        if not isinstance(raw, Mapping):
            raise ContractError("manifest must be an object")
        plan = build_plan(raw)
        args.output_dir.mkdir(parents=True, exist_ok=True)
        write_json(args.output_dir / "experiment_manifest.json", raw)
        write_json(args.output_dir / "experiment_plan.json", plan)
        (args.output_dir / "EXPERIMENT_CHECKLIST.md").write_text(
            checklist_markdown(raw, plan), encoding="utf-8")
    except ContractError as exc:
        print("experiment_plan: %s" % exc, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
