#!/usr/bin/env python3
"""Audit positive-label support using only decisions with a full future horizon."""

import argparse
import json
import os
from pathlib import Path
import sys


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from phase210.offline_pipeline import build_qq_decisions  # noqa: E402


def audit(trace_path, session):
    decisions, conversion = build_qq_decisions(trace_path, session)
    if decisions:
        complete_cutoff = max(row["window_start_ns"] for row in decisions) - 60_000_000_000
        complete = [row for row in decisions if row["window_start_ns"] <= complete_cutoff]
    else:
        complete_cutoff = None
        complete = []
    positives = 0
    positive_decisions = 0
    pairwise = 0
    candidates = 0
    for decision in complete:
        count = len(decision["candidates"])
        positive = sum(candidate["future"]["60"] is not None for candidate in decision["candidates"])
        candidates += count
        positives += positive
        positive_decisions += positive > 0
        pairwise += 0 < positive < count
    return {
        "schema_version": 1,
        "session_id": session,
        "trace_path": str(Path(trace_path).resolve()),
        "decoded_file_rows": conversion["decoded_file_rows"],
        "all_decisions": conversion["decision_count"],
        "complete_horizon_decisions": len(complete),
        "complete_horizon_cutoff_ns": complete_cutoff,
        "candidate_count": candidates,
        "positive_candidates_60s": positives,
        "positive_decisions_60s": positive_decisions,
        "pairwise_evaluable_decisions_60s": pairwise,
        "minimum_positive_candidates": 20,
        "minimum_pairwise_evaluable_decisions": 10,
        "passed": positives >= 20 and pairwise >= 10,
        "censored_tail_excluded": True,
        "future_used_for_candidate_set": False,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--trace", type=Path, required=True)
    parser.add_argument("--session", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    payload = audit(args.trace, args.session)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_name(args.output.name + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    os.replace(temporary, args.output)
    print("positive_candidates=%d pairwise_decisions=%d passed=%s" % (
        payload["positive_candidates_60s"],
        payload["pairwise_evaluable_decisions_60s"],
        str(payload["passed"]).lower(),
    ))


if __name__ == "__main__":
    main()
