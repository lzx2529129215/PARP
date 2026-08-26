#!/usr/bin/env python3
"""Verify online prediction timing and the userspace PARP sink audit."""

from __future__ import annotations

import argparse
import csv
import json
import math
from pathlib import Path


def rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8", newline="") as stream:
        return list(csv.DictReader(stream))


def int_value(value: object, default: int = 0) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return default


def float_value(value: object) -> float | None:
    try:
        result = float(str(value))
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session-dir", type=Path, required=True)
    parser.add_argument("--app-scope-config", type=Path, required=True)
    parser.add_argument("--bridge-mode", choices=["off", "dry-run", "shadow-write"], required=True)
    args = parser.parse_args()

    call_rows = rows(args.session_dir / "model/online_lstm_duration_call_trace.csv")
    prediction_rows = rows(args.session_dir / "model/online_lstm_predictions.csv")
    bridge_rows = rows(args.session_dir / "parp/parp_bridge_events.csv")
    success_calls = [row for row in call_rows if row.get("status") == "success"]
    successful_prediction_rows = [row for row in prediction_rows if row.get("status") == "success"]
    bridge_commands = [
        row for row in bridge_rows
        if row.get("serialized_command") and row.get("event_type") in {"app_bind", "app_prior"}
    ]
    prior_rows = [row for row in bridge_commands if row.get("event_type") == "app_prior"]
    bind_rows = [row for row in bridge_commands if row.get("event_type") == "app_bind"]
    shadow_prior_rows = [
        row for row in prior_rows
        if row.get("write_success") == "true"
        and row.get("snapshot_update_seen") == "true"
        and row.get("prediction_id")
    ]
    whitelist = set()
    try:
        scope = json.loads(args.app_scope_config.read_text(encoding="utf-8"))
        whitelist = {str(item.get("vocab_name", "")) for item in scope.get("apps", []) if item.get("vocab_name")}
    except (OSError, ValueError):
        pass

    invalid_probability = 0
    current_in_candidates = 0
    invalid_candidate = 0
    for row in prior_rows:
        probability = float_value(row.get("probability"))
        if probability is None or not 0.0 <= probability <= 1.0:
            invalid_probability += 1
        if row.get("current_app") == row.get("candidate_app"):
            current_in_candidates += 1
        if whitelist and row.get("candidate_app") not in whitelist:
            invalid_candidate += 1

    prediction_ids = {row.get("prediction_id") for row in prior_rows if row.get("prediction_id")}
    derived_success_ids = {
        f"{row.get('session_id', args.session_dir.name)}-p{int_value(row.get('call_id')):05d}"
        for row in success_calls
    }
    joined_prediction_ids = prediction_ids & derived_success_ids
    snapshot_confirmed_prediction_ids = {
        row.get("prediction_id") for row in shadow_prior_rows if row.get("prediction_id")
    }
    preflight_path = args.session_dir / "parp/preflight.json"
    preflight = json.loads(preflight_path.read_text(encoding="utf-8")) if preflight_path.exists() else {}
    summary_path = args.session_dir / "parp/parp_bridge_summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8")) if summary_path.exists() else {}
    funnel = dict(summary.get("prediction_funnel", {}))
    post_filter_candidates = int_value(funnel.get("candidate_row_count_after_filter"))
    duplicate_prediction_batches = int_value(summary.get("duplicate_predictions_suppressed"))
    stale_prediction_batches = int_value(summary.get("stale_predictions_suppressed"))
    suppressed_candidate_rows = max(0, post_filter_candidates - len(prior_rows))
    # Candidate-row de-duplication is distinct from suppressing a whole
    # prediction batch whose normalized scores equal the previous batch.
    # Make that second stage explicit so candidate rows cannot be mistaken for
    # serialized app_prior commands.
    funnel.update({
        "post_filter_candidate_row_count": post_filter_candidates,
        "duplicate_prediction_batch_count": duplicate_prediction_batches,
        "stale_prediction_batch_count": stale_prediction_batches,
        "suppressed_candidate_row_count": suppressed_candidate_rows,
        "serialized_prior_command_row_count": len(prior_rows),
    })

    result = {
        "session_dir": str(args.session_dir),
        "bridge_mode": args.bridge_mode,
        "online_lstm_calls": len(call_rows),
        "online_lstm_success_calls": len(success_calls),
        "online_lstm_success_prediction_rows": len(successful_prediction_rows),
        "online_lstm_triggered": sum(bool(row.get("trigger_type")) for row in success_calls),
        "prediction_batch_count": funnel.get("prediction_batch_count", len(success_calls)),
        "prediction_candidate_row_count": funnel.get("candidate_row_count", len(successful_prediction_rows)),
        "prediction_format": funnel.get("prediction_format", "horizon"),
        "prediction_rows_have_horizon_field": bool(prediction_rows and "horizon" in prediction_rows[0]),
        "prediction_ids_unique": len(prediction_ids) == len([item for item in prediction_ids if item]),
        "prediction_ids_joined_to_calls": len(joined_prediction_ids),
        "prediction_ids_with_snapshot_confirmation": len(snapshot_confirmed_prediction_ids),
        "bridge_command_rows": len(bridge_commands),
        "app_bind_command_rows": len(bind_rows),
        "app_prior_command_rows": len(prior_rows),
        "invalid_probability_rows": invalid_probability,
        "current_app_in_candidates": current_in_candidates,
        "non_whitelist_candidates": invalid_candidate,
        "preflight_status": preflight.get("status", "MISSING"),
        "snapshot_updates_observed": summary.get("snapshot_updates_observed", 0),
        "snapshot_binding_updates_observed": summary.get("snapshot_binding_updates_observed", 0),
        "snapshot_prior_updates_observed": summary.get("snapshot_prior_updates_observed", 0),
        "prediction_to_snapshot_matched": summary.get("prediction_to_snapshot_matched", 0),
        "shadow_write_prior_snapshot_rows": len(shadow_prior_rows),
        "duplicate_prediction_batches_suppressed": duplicate_prediction_batches,
        "stale_prediction_batches_suppressed": stale_prediction_batches,
        "candidate_rows_suppressed_after_filter": suppressed_candidate_rows,
        "prediction_funnel": funnel,
        "bind_stats": {
            "logical_app_count": summary.get("bind_logical_app_count", 0),
            "resolution_attempts": summary.get("bind_resolution_attempts", 0),
            "missing_cgroup": summary.get("bind_missing_cgroup", summary.get("missing_cgroup", 0)),
            "serialized_commands": summary.get("bind_serialized_commands", len(bind_rows)),
            "write_attempts": summary.get("bind_write_attempts", 0),
            "retry_attempts": summary.get("bind_retry_attempts", 0),
            "dry_run_not_attempted": summary.get("bind_dry_run_not_attempted", 0),
            "blocked_missing_interface": summary.get("bind_blocked_missing_interface", 0),
        },
        "prior_stats": {
            "serialized_commands": summary.get("prior_command_row_count", len(prior_rows)),
            "write_attempts": summary.get("prior_write_attempts", 0),
            "retry_attempts": summary.get("prior_retry_attempts", 0),
            "dry_run_not_attempted": summary.get("prior_dry_run_not_attempted", 0),
            "blocked_missing_interface": summary.get("prior_blocked_missing_interface", 0),
        },
        "status": "PASS",
    }
    if not success_calls or not successful_prediction_rows:
        result["status"] = "FAIL"
    if invalid_probability or current_in_candidates or invalid_candidate:
        result["status"] = "FAIL"
    if args.bridge_mode != "off" and not bridge_commands:
        result["status"] = "FAIL"
    if funnel and int_value(funnel.get("prior_command_row_count")) != len(prior_rows):
        result["status"] = "FAIL"
    if funnel and post_filter_candidates != len(prior_rows) + suppressed_candidate_rows:
        result["status"] = "FAIL"
    if result["prediction_format"] == "app_probability" and result["prediction_rows_have_horizon_field"]:
        result["status"] = "FAIL"
    if args.bridge_mode == "shadow-write" and preflight.get("status") != "READY":
        result["status"] = "RUNTIME_BLOCKED"
    elif args.bridge_mode == "shadow-write" and not shadow_prior_rows:
        result["status"] = "FAIL"

    review = args.session_dir / "review"
    review.mkdir(parents=True, exist_ok=True)
    (review / "online_prediction_coverage.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (review / "prediction_sink_coverage.json").write_text(
        json.dumps({
            "bridge_mode": args.bridge_mode,
            "command_rows": len(bridge_commands),
            "app_bind_command_rows": len(bind_rows),
            "app_prior_command_rows": len(prior_rows),
            "write_success_rows": sum(row.get("write_success") == "true" for row in bridge_commands),
            "dry_run_rows": sum(row.get("status") == "DRY_RUN" for row in bridge_commands),
            "snapshot_update_rows": sum(row.get("snapshot_update_seen") == "true" for row in bridge_commands),
            "shadow_write_prior_snapshot_rows": len(shadow_prior_rows),
            "status": result["status"],
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (review / "prediction_funnel.json").write_text(
        json.dumps({
            "prediction_batch_count": result["prediction_batch_count"],
            "prediction_candidate_row_count": result["prediction_candidate_row_count"],
            "funnel": funnel,
            "app_prior_command_rows": len(prior_rows),
            "candidate_rows_suppressed_after_filter": suppressed_candidate_rows,
            "status": result["status"],
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    with (review / "prediction_to_snapshot_join.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(
            stream,
            fieldnames=["prediction_id", "batch_id", "candidate_app", "candidate_app_id", "serialized_command", "write_success", "snapshot_update_seen", "snapshot_generation_before", "snapshot_generation"],
        )
        writer.writeheader()
        writer.writerows({field: row.get(field, "") for field in writer.fieldnames} for row in prior_rows)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] in {"PASS", "RUNTIME_BLOCKED"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
