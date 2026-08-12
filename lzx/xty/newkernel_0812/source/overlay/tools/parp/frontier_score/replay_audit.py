#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0
"""Audit whether frozen data can support genuine frontier SHADOW replay."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Dict, Iterable, Mapping, Set

STATUS = "PARP_FRONTIER_SCORE_SHADOW_NOT_SUPPORTED"
REQUIRED_CANDIDATE_FIELDS = {
    "source_seq",
    "frontier_seq",
    "native_sort_result",
    "folio_nr_pages",
    "page_type",
    "nid",
    "generation_index",
}
REQUIRED_RECLAIM_FIELDS = {
    "nr_to_reclaim",
    "nr_reclaimed",
    "generation_capacities_pages",
    "generation_efficiency_q15",
    "frontier_valid_until_ns",
}
FORBIDDEN_PROXY_FIELDS = {
    "generation_proxy",
    "tier_proxy",
    "refault_proxy",
}


def audit_fields(fields: Iterable[str]) -> Dict[str, object]:
    available: Set[str] = set(fields)
    missing_candidate = sorted(REQUIRED_CANDIDATE_FIELDS - available)
    missing_reclaim = sorted(REQUIRED_RECLAIM_FIELDS - available)
    proxies = sorted(FORBIDDEN_PROXY_FIELDS & available)
    supported = not missing_candidate and not missing_reclaim
    return {
        "supported": supported,
        "missing_candidate_fields": missing_candidate,
        "missing_reclaim_fields": missing_reclaim,
        "proxy_fields_present_but_not_accepted": proxies,
    }


def first_jsonl(path: Path) -> Mapping[str, object]:
    with path.open(encoding="utf-8") as stream:
        return json.loads(stream.readline())


def nested_fields(row: Mapping[str, object]) -> Set[str]:
    fields = set(row)
    candidate = row.get("candidate")
    if isinstance(candidate, Mapping):
        fields.update(str(key) for key in candidate)
    return fields


def session_counts(phase28b: Path, phase210b: Path) -> Dict[str, object]:
    formal = Counter()
    phase28_inventory = json.loads(
        (phase28b / "validation/session_inventory.json").read_text(
            encoding="utf-8"))
    for session in phase28_inventory.get("sessions", []):
        formal[str(session["app"])] += 1
    qq_inventory = json.loads(
        (phase210b / "input/session_inventory.json").read_text(
            encoding="utf-8"))
    pilots = Counter()
    for session in qq_inventory.get("sessions", []):
        if "PILOT" in str(session.get("role", "")):
            pilots[str(session["app"])] += 1
        else:
            formal[str(session["app"])] += 1
    return {
        "formal": {app: formal.get(app, 0)
                   for app in ("WPS", "FILES", "QQ")},
        "pilot_not_formal": {app: pilots.get(app, 0)
                             for app in ("WPS", "FILES", "QQ")},
        "minimum_required_per_app": 4,
    }


def run(phase28b: Path, phase210b: Path) -> Dict[str, object]:
    universe = phase210b / "candidate_universe/universe.jsonl"
    row = first_jsonl(universe)
    field_audit = audit_fields(nested_fields(row))
    counts = session_counts(phase28b, phase210b)
    enough_sessions = all(value >= 4 for value in counts["formal"].values())
    supported = bool(field_audit["supported"] and enough_sessions)
    if supported:
        raise RuntimeError(
            "data unexpectedly meets the schema; a separate replay review is required")
    return {
        "status": STATUS,
        "source": str(phase210b),
        "row_schema_audit": field_audit,
        "sessions": counts,
        "policy": {
            "old_v1_v2_candidate_concatenation_used": False,
            "proxy_scored_as_real_frontier": False,
            "pilot_counted_as_formal": False,
            "future_labels_used_for_selection": False,
        },
        "metrics": {
            "score_count": 0,
            "would_promote_pages": 0,
            "actual_promote_pages": 0,
            "frontier_bypass": 0,
            "model_bypass": 0,
            "budget_bypass": 0,
            "shadow_precision": None,
            "shadow_recall": None,
            "pairwise_auc": None,
            "ndcg": None,
            "trace_lost": 0,
            "trace_lost_measured": False,
        },
        "limitations": [
            "No post-sort native-isolate candidate identity or source_seq.",
            "No live per-lruvec/type generation capacities and reclaim demand.",
            "No measured per-generation reclaim-efficiency EMA or frontier TTL.",
            "Phase2.10B generation/tier fields are proxies and are not accepted.",
            "Formal-session minimum is not met; QQ pilot is test-only.",
            "No live frontier trace was collected, so trace_lost is not measured.",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase28b", type=Path, required=True)
    parser.add_argument("--phase210b", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    payload = run(args.phase28b.resolve(), args.phase210b.resolve())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n",
                           encoding="utf-8")
    print(payload["status"])


if __name__ == "__main__":
    main()
