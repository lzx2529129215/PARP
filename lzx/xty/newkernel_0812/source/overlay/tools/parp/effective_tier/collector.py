#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0
"""Build a future-real-access dataset from already-exported Phase-E events.

The collector is deliberately offline-only.  It cannot read tracefs/debugfs,
write cgroups, set a PARP mode, start pressure, install a kernel, or reboot.
"""

from __future__ import annotations

import argparse
import bisect
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import DefaultDict, Dict, List, Mapping, Optional, Sequence, Tuple

try:
    from .contracts import (
        BASE_FEATURES,
        DEFAULT_RANKING_TIE_MARGIN_NS,
        LABEL_WINDOWS_NS,
        RANKING_HORIZON_NS,
        SCHEMA_VERSION,
        SESSION_SCHEMA_VERSION,
        TELEMETRY_KINDS,
        ContractError,
        assign_session_split,
        folio_key,
        normalized_id,
        quadrant,
        read_json,
        read_jsonl,
        reject_live_path,
        session_key,
        validate_access,
        validate_candidate,
        validate_session,
        validate_telemetry,
        write_json,
        write_jsonl,
    )
except ImportError:  # Direct execution from this directory.
    from contracts import (  # type: ignore
        BASE_FEATURES,
        DEFAULT_RANKING_TIE_MARGIN_NS,
        LABEL_WINDOWS_NS,
        RANKING_HORIZON_NS,
        SCHEMA_VERSION,
        SESSION_SCHEMA_VERSION,
        TELEMETRY_KINDS,
        ContractError,
        assign_session_split,
        folio_key,
        normalized_id,
        quadrant,
        read_json,
        read_jsonl,
        reject_live_path,
        session_key,
        validate_access,
        validate_candidate,
        validate_session,
        validate_telemetry,
        write_json,
        write_jsonl,
    )


AccessPoint = Tuple[int, str]
SessionKey = Tuple[str, str]

TRACE_EVENT_PATTERN = re.compile(
    r"\bparp_effective_tier_(decision|access|outcome|batch|lock):\s+(.*)$")
TRACE_MODES = {
    0: "OFF",
    1: "SHADOW_EFFECTIVE_TIER",
    2: "APPLY_PROTECT_ONLY",
    3: "APPLY_BIDIRECTIONAL",
    4: "APPLY_RANDOM_MATCHED",
    5: "APPLY_RECENCY_BASELINE",
}
TRACE_ACTIONS = {
    0: "KEEP_RECLAIM",
    1: "PREDICTIVE_UPGRADE",
    2: "KEEP_PROTECT",
    3: "PREDICTIVE_DOWNGRADE",
    4: "SPECIAL_NATIVE_PROTECT",
}
TRACE_BYPASS = {
    0: "NONE",
    1: "DISABLED",
    2: "MODEL_INVALID",
    3: "METADATA_MISSING",
    4: "STATE_UNSTABLE",
    5: "NOT_BOUNDARY",
    6: "STRONG_NATIVE",
    7: "UPGRADE_BUDGET",
    8: "DOWNGRADE_BUDGET",
    9: "REPEAT_UPGRADE",
    10: "REPEAT_DOWNGRADE",
    11: "PRESSURE",
    12: "NO_PROGRESS",
    13: "GENERATION_RACE",
    14: "RANDOM_UNSELECTED",
}
TRACE_ACCESS = {
    0: "PTE_YOUNG",
    1: "MARK_ACCESSED",
    2: "FD_REFERENCE",
}
TRACE_OUTCOME = {
    0: "reclaimed",
    1: "putback",
    2: "activated",
    3: "demote_attempt",
}
TRACE_LOCK_SCOPES = {
    0: "scan_folios",
    1: "batch",
}


def _trace_fields(payload: str, context: str) -> Dict[str, str]:
    fields: Dict[str, str] = {}
    for token in payload.split():
        if "=" not in token:
            continue
        name, value = token.split("=", 1)
        if name in fields:
            raise ContractError("duplicate %s field %s" % (context, name))
        fields[name] = value
    return fields


def _trace_int(fields: Mapping[str, str], name: str, context: str) -> int:
    if name not in fields:
        raise ContractError("%s trace is missing %s" % (context, name))
    try:
        return int(fields[name], 0)
    except ValueError as exc:
        raise ContractError("invalid %s.%s" % (context, name)) from exc


def _trace_optional_int(fields: Mapping[str, str], name: str,
                        context: str) -> Optional[int]:
    if name not in fields:
        return None
    return _trace_int(fields, name, context)  #lzx


def _trace_enum(table: Mapping[int, str], value: int, name: str) -> str:
    try:
        return table[value]
    except KeyError as exc:
        raise ContractError("unknown trace %s value %d" % (name, value)) from exc


def _trace_decision(fields: Mapping[str, str]) -> Dict[str, object]:
    context = "effective-tier decision"
    thresholds = fields.get("thresholds", "").split("/")
    features = fields.get("features", "").split(",")
    if len(thresholds) != 4 or len(features) != len(BASE_FEATURES):
        raise ContractError("invalid decision threshold/feature vectors")
    try:
        threshold_values = [int(value, 0) for value in thresholds]
        feature_values = [int(value, 0) for value in features]
    except ValueError as exc:
        raise ContractError("invalid numeric decision vector") from exc
    action = _trace_enum(TRACE_ACTIONS, _trace_int(fields, "action", context),
                         "action")
    model_valid = bool(_trace_int(fields, "model_valid", context))
    features_valid = bool(_trace_int(fields, "features_valid", context))
    model_type = fields.get("model_type")
    model_checksum_raw = fields.get("model_checksum")
    model_checksum = (None if model_checksum_raw == "none" else
                      model_checksum_raw)
    score_bin = _trace_int(fields, "rank_score_bin", context)
    if "score_bin" in fields and _trace_int(fields, "score_bin", context) != \
            score_bin:
        raise ContractError("deprecated score_bin disagrees with rank_score_bin")
    native_protect = bool(_trace_int(fields, "native_protect", context))
    special = bool(_trace_int(fields, "special", context))
    isolate_attempted = bool(_trace_int(fields, "isolate_attempted", context))
    isolate_succeeded = bool(_trace_int(fields, "isolate_result", context))
    result: Dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "event_kind": "tier_gate_candidate",
        "timestamp_ns": _trace_int(fields, "time", context),
        "experiment_id": fields.get("experiment"),
        "session_id": fields.get("session"),
        "folio_cookie": fields.get("cookie"),
        "folio_lifetime_epoch": _trace_int(fields, "lifetime", context),
        "memcg_anon_id": fields.get("memcg"),
        "nid": _trace_int(fields, "nid", context),
        "page_type": ("file" if _trace_int(fields, "type", context)
                      else "anon"),
        "source_seq": _trace_int(fields, "source_seq", context),
        "generation_index": _trace_int(fields, "gen", context),
        "native_tier": _trace_int(fields, "native_tier", context),
        "native_tier_idx": _trace_int(fields, "tier_idx", context),
        "special_native_protect": special,
        "native_protect": native_protect,
        "mode": _trace_enum(TRACE_MODES, _trace_int(fields, "mode", context),
                            "mode"),
        "features": dict(zip(
            BASE_FEATURES,
            feature_values if features_valid else [None] * len(BASE_FEATURES))),
        "model_valid": model_valid,
        "features_valid": features_valid,
        "model_type": model_type,
        "model_version": _trace_int(fields, "model_version", context),
        "expected_model_version": _trace_int(
            fields, "expected_model_version", context),
        "feature_schema_version": _trace_int(
            fields, "feature_schema_version", context),
        "model_checksum": model_checksum,
        "pairwise_model_checksum": (model_checksum if
                                     model_type == "pairwise_linear_ranker"
                                     else None),
        "model_provenance": fields.get("model_provenance"),
        "reuse_score": _trace_int(fields, "score", context),
        "rank_score_bin": score_bin if score_bin != 255 else None,
        "score_percentile": None,
        "cold_threshold": threshold_values[0] if model_valid else None,
        "hot_threshold_1": threshold_values[1] if model_valid else None,
        "hot_threshold_2": threshold_values[2] if model_valid else None,
        "hot_threshold_3": threshold_values[3] if model_valid else None,
        "score_threshold_cold": (threshold_values[0]
                                  if model_valid else None),
        "score_threshold_hot_1": (threshold_values[1]
                                   if model_valid else None),
        "score_threshold_hot_2": (threshold_values[2]
                                   if model_valid else None),
        "score_threshold_hot_3": (threshold_values[3]
                                   if model_valid else None),
        "delta_tier_q8": _trace_int(fields, "delta_q8", context),
        "predictive_delta_tier_q8": _trace_int(
            fields, "delta_q8", context),
        "effective_tier_q8": _trace_int(fields, "effective_q8", context),
        "effective_protect": bool(_trace_int(fields, "effective_protect",
                                              context)),
        "action": action,
        "bypass_reason": _trace_enum(
            TRACE_BYPASS, _trace_int(fields, "bypass", context), "bypass"),
        "folio_nr_pages": _trace_int(fields, "pages", context),
        "batch_id": fields.get("batch"),
        "reclaim_epoch": fields.get("epoch"),
        "priority": _trace_int(fields, "priority", context),
        "score_duration_ns": _trace_int(fields, "score_ns", context),
        "decision_duration_ns": _trace_int(fields, "decision_ns", context),
        "actual_native_behavior": ("protect" if special or native_protect
                                   else "reclaim"),
        "isolate_result": ("not_attempted" if not isolate_attempted else
                           "succeeded" if isolate_succeeded else "failed"),
        "reclaimed": None,
        "putback": None,
        "activated": None,
        "gate_reached": True,
        "candidate_scope": "ALL_NATIVE_TIER_GATE_FOLIOS",
        "actual_tier_protect": bool(_trace_int(
            fields, "actual_tier_protect", context)),
        "sort_result": bool(_trace_int(fields, "sort", context)),
        "decision_trace_sequence": _trace_int(fields, "trace_seq", context),
        "raw_trace_lost_flag": False,
        # Phase-F fields are optional for legacy exported traces but required
        # by the live-shadow harness before pressure-aware analysis begins.
        "pressure_level_kernel": _trace_optional_int(
            fields, "pressure_level_kernel", context),
        "reclaim_context": _trace_optional_int(
            fields, "reclaim_context", context),
        "sc_priority": _trace_optional_int(fields, "sc_priority", context),
        "nr_to_reclaim": _trace_optional_int(
            fields, "nr_to_reclaim", context),
        "nr_reclaimed_before": _trace_optional_int(
            fields, "nr_reclaimed_before", context),
        "epoch_reclaimed_pages": _trace_optional_int(
            fields, "epoch_reclaimed_pages", context),
        "batch_scanned_pages": _trace_optional_int(
            fields, "batch_scanned_pages", context),
        "batch_isolated_pages": _trace_optional_int(
            fields, "batch_isolated_pages", context),
        "batch_reclaimed_pages": _trace_optional_int(
            fields, "batch_reclaimed_pages", context),
        "consecutive_no_progress_batches": _trace_optional_int(
            fields, "consecutive_no_progress_batches", context),
        "fixed_delta_q8": _trace_optional_int(
            fields, "fixed_delta_q8", context),
        "binary_bypass_delta_q8": _trace_optional_int(
            fields, "binary_bypass_delta_q8", context),
        "pressure_aware_delta_q8": _trace_optional_int(
            fields, "pressure_aware_delta_q8", context),
        "fixed_effective_protect": (
            None if "fixed_effective_protect" not in fields else bool(
                _trace_int(fields, "fixed_effective_protect", context))),
        "pressure_aware_effective_protect": (
            None if "pressure_aware_effective_protect" not in fields else bool(
                _trace_int(fields, "pressure_aware_effective_protect",
                           context))),
        "pressure_policy_version": _trace_optional_int(
            fields, "pressure_policy_version", context),
        "pressure_policy_provenance": fields.get(
            "pressure_policy_provenance"),
        "pressure_bypass_reason": (
            None if "pressure_bypass_reason" not in fields else _trace_enum(
                TRACE_BYPASS, _trace_int(fields, "pressure_bypass_reason",
                                          context), "pressure bypass")),  #lzx
    }
    if "mode" in fields:
        result["mode"] = _trace_enum(
            TRACE_MODES, _trace_int(fields, "mode", context), "mode")
    return result


def parse_exported_trace(paths: Sequence[Path]) -> Tuple[List[Dict[str, object]],
                                                         Dict[str, int]]:
    """Normalize already-exported Linux trace text; never open tracefs itself."""

    decisions: List[Dict[str, object]] = []
    raw_accesses: List[Dict[str, object]] = []
    outcomes: List[Dict[str, object]] = []
    batches: List[Dict[str, object]] = []
    locks: List[Dict[str, object]] = []
    trace_sequences: List[int] = []
    stats: Counter[str] = Counter()
    for path in paths:
        try:
            with path.open("r", encoding="utf-8", errors="replace") as stream:
                for line in stream:
                    match = TRACE_EVENT_PATTERN.search(line)
                    if not match:
                        continue
                    kind, payload = match.groups()
                    fields = _trace_fields(payload, kind)
                    stats["matched_trace_lines"] += 1
                    trace_sequences.append(_trace_int(
                        fields, "trace_seq", "effective-tier " + kind))
                    if kind == "decision":
                        decisions.append(_trace_decision(fields))
                    elif kind == "access":
                        context = "effective-tier access"
                        raw_accesses.append({
                            "timestamp_ns": _trace_int(fields, "time", context),
                            "experiment_id": fields.get("experiment"),
                            "session_id": fields.get("session"),
                            "folio_cookie": fields.get("cookie"),
                            "folio_lifetime_epoch": _trace_int(
                                fields, "lifetime", context),
                            "event": _trace_int(fields, "event", context),
                            "real": bool(_trace_int(fields, "real", context)),
                            "candidate_count": _trace_optional_int(
                                fields, "candidate_count", context),  #lzx
                        })
                    elif kind == "outcome":
                        context = "effective-tier outcome"
                        outcomes.append({
                            "timestamp_ns": _trace_int(fields, "time", context),
                            "experiment_id": fields.get("experiment"),
                            "session_id": fields.get("session"),
                            "folio_cookie": fields.get("cookie"),
                            "folio_lifetime_epoch": _trace_int(
                                fields, "lifetime", context),
                            "action": _trace_int(fields, "action", context),
                            "actual_action": _trace_int(
                                fields, "actual_action", context),
                            "outcome": _trace_int(fields, "outcome", context),
                        })
                    elif kind == "batch":
                        context = "effective-tier batch"
                        batches.append({
                            "timestamp_ns": _trace_int(fields, "time", context),
                            "experiment_id": fields.get("experiment"),
                            "session_id": fields.get("session"),
                            "batch_id": fields.get("batch"),
                            "mode": _trace_int(fields, "mode", context),
                            "model_time_ns": _trace_int(fields, "model_ns", context),
                            "reclaim_result": bool(_trace_optional_int(
                                fields, "reclaim_result", context) or 0),
                            "batch_scanned_pages": _trace_optional_int(
                                fields, "batch_scanned_pages", context),
                            "batch_isolated_pages": _trace_optional_int(
                                fields, "batch_isolated_pages", context),
                            "batch_reclaimed_pages": _trace_optional_int(
                                fields, "batch_reclaimed_pages", context),  #lzx
                        })
                    elif kind == "lock":
                        context = "effective-tier lock"
                        locks.append({
                            "timestamp_ns": _trace_int(fields, "time", context),
                            "experiment_id": fields.get("experiment"),
                            "session_id": fields.get("session"),
                            "mode": _trace_int(fields, "mode", context),
                            "scope": _trace_int(fields, "scope", context),
                            "wait_ns": _trace_int(fields, "wait_ns", context),
                            "held_ns": _trace_int(fields, "held_ns", context),
                            "irq_disabled_ns": _trace_int(
                                fields, "irq_disabled_ns", context),
                            "irq_disabled_measured": bool(_trace_int(
                                fields, "irq_measured", context)),
                        })
        except OSError as exc:
            raise ContractError("cannot read exported trace %s: %s" %
                                (path, exc)) from exc
    if not decisions:
        raise ContractError("exported trace contains no effective-tier decisions")

    sequences = sorted(set(trace_sequences))
    missing_sequences = sum(max(0, right - left - 1)
                            for left, right in zip(sequences, sequences[1:]))
    if len(sequences) != len(trace_sequences):
        raise ContractError("duplicate effective-tier trace sequence")
    if missing_sequences:
        for decision in decisions:
            decision["raw_trace_lost_flag"] = True
    stats["trace_sequence_missing"] = missing_sequences
    stats["trace_sequence_events"] = len(trace_sequences)
    stats["trace_sequence_measured"] = 1

    owners: DefaultDict[Tuple[str, int], set] = defaultdict(set)
    batch_owners: DefaultDict[str, set] = defaultdict(set)
    for decision in decisions:
        owner = (normalized_id(decision, "experiment_id"),
                 normalized_id(decision, "session_id"))
        owners[(normalized_id(decision, "folio_cookie"),
                int(decision["folio_lifetime_epoch"]))].add(owner)
        batch_owners[normalized_id(decision, "batch_id")].add(owner)

    # Attach outcome truth to the latest preceding matching decision.
    for raw in sorted(outcomes, key=lambda item: int(item["timestamp_ns"])):
        action = _trace_enum(TRACE_ACTIONS, int(raw["action"]), "action")
        candidates = [
            decision for decision in decisions
            if normalized_id(decision, "folio_cookie") == str(raw["folio_cookie"])
            and int(decision["folio_lifetime_epoch"]) ==
            int(raw["folio_lifetime_epoch"])
            and decision["action"] == action
            and int(decision["timestamp_ns"]) <= int(raw["timestamp_ns"])
            and (raw["experiment_id"] is None or
                 normalized_id(decision, "experiment_id") ==
                 str(raw["experiment_id"]))
            and (raw["session_id"] is None or
                 normalized_id(decision, "session_id") ==
                 str(raw["session_id"]))
        ]
        if not candidates:
            stats["unmatched_outcomes"] += 1
            continue
        target = max(candidates, key=lambda item: int(item["timestamp_ns"]))
        outcome = _trace_enum(TRACE_OUTCOME, int(raw["outcome"]), "outcome")
        target["actual_outcome_action"] = _trace_enum(
            TRACE_ACTIONS, int(raw["actual_action"]), "actual outcome action")
        if outcome in ("reclaimed", "putback", "activated"):
            target["reclaimed"] = outcome == "reclaimed"
            target["putback"] = outcome == "putback"
            target["activated"] = outcome == "activated"
        stats["matched_outcomes"] += 1

    records: List[Dict[str, object]] = list(decisions)
    for raw in raw_accesses:
        if not raw["real"]:
            stats["policy_move_access_events_ignored"] += 1
            continue
        if raw["candidate_count"] is None or int(
                raw["candidate_count"]) < 1:  #lzx
            raise ContractError(
                "real access trace lacks positive candidate_count")  #lzx
        source = _trace_enum(TRACE_ACCESS, int(raw["event"]), "real access")
        key = (str(raw["folio_cookie"]), int(raw["folio_lifetime_epoch"]))
        if raw["experiment_id"] is not None and raw["session_id"] is not None:
            experiment_id = str(raw["experiment_id"])
            session_id = str(raw["session_id"])
        else:
            possible = owners.get(key, set())
            if not possible:
                stats["unmatched_real_access_events"] += 1
                continue
            if len(possible) != 1:
                raise ContractError("real-access session owner is ambiguous")
            experiment_id, session_id = next(iter(possible))
        records.append({
            "schema_version": SCHEMA_VERSION,
            "event_kind": "real_access",
            "timestamp_ns": int(raw["timestamp_ns"]),
            "experiment_id": experiment_id,
            "session_id": session_id,
            "folio_cookie": str(raw["folio_cookie"]),
            "folio_lifetime_epoch": int(raw["folio_lifetime_epoch"]),
            "access_source": source,
            "is_real_access": True,
            "candidate_count": int(raw["candidate_count"]),  #lzx
        })
        stats["real_access_events"] += 1

    # The first batch trace is emitted while lru_lock is still held.  A second
    # trace for the same batch is emitted after shrink_folio_list() has the
    # real reclaimed-page count.  Resolve that result only by the exported
    # experiment/session/batch identity; never infer it from future access or
    # candidate outcomes.  #lzx
    batch_results: Dict[Tuple[str, str, str], Mapping[str, object]] = {}
    for raw in batches:
        if not raw["reclaim_result"]:
            continue
        if raw["experiment_id"] is not None and raw["session_id"] is not None:
            owner = (str(raw["experiment_id"]), str(raw["session_id"]))
        else:
            possible = batch_owners.get(str(raw["batch_id"]), set())
            if len(possible) != 1:
                stats["unmatched_or_ambiguous_batch_reclaim_results"] += 1
                continue
            owner = next(iter(possible))
        key = (owner[0], owner[1], str(raw["batch_id"]))
        if key in batch_results:
            raise ContractError("duplicate batch reclaim result for %s" %
                                (key,))
        if raw["batch_reclaimed_pages"] is None:
            raise ContractError("batch reclaim result lacks reclaimed pages")
        batch_results[key] = raw
    for decision in decisions:
        key = (normalized_id(decision, "experiment_id"),
               normalized_id(decision, "session_id"),
               normalized_id(decision, "batch_id"))
        result = batch_results.get(key)
        if result is None:
            decision["batch_reclaim_result_observed"] = False
            continue
        for field in ("batch_scanned_pages", "batch_isolated_pages",
                      "batch_reclaimed_pages"):
            value = result[field]
            if value is not None:
                decision[field] = int(value)
        decision["batch_reclaim_result_observed"] = True
        stats["decisions_with_batch_reclaim_result"] += 1  #lzx

    # Batch model time is directly measured.  Do not invent lock wait/IRQ or
    # whole-reclaim counters that the trace does not contain.
    for raw in batches:
        if raw["reclaim_result"]:
            stats["batch_reclaim_result_events"] += 1
            continue  #lzx
        if raw["experiment_id"] is not None and raw["session_id"] is not None:
            experiment_id = str(raw["experiment_id"])
            session_id = str(raw["session_id"])
        else:
            possible = batch_owners.get(str(raw["batch_id"]), set())
            if len(possible) != 1:
                stats["unmatched_or_ambiguous_batches"] += 1
                continue
            experiment_id, session_id = next(iter(possible))
        records.append({
            "schema_version": SCHEMA_VERSION,
            "event_kind": "score_latency",
            "timestamp_ns": int(raw["timestamp_ns"]),
            "experiment_id": experiment_id,
            "session_id": session_id,
            "mode": _trace_enum(TRACE_MODES, int(raw["mode"]), "mode"),
            "component": "batch_model_total",
            "duration_ns": int(raw["model_time_ns"]),
            "batch_id": str(raw["batch_id"]),
        })
        stats["batch_model_latency_events"] += 1
    for raw in locks:
        records.append({
            "schema_version": SCHEMA_VERSION,
            "event_kind": "lock_latency",
            "timestamp_ns": int(raw["timestamp_ns"]),
            "experiment_id": str(raw["experiment_id"]),
            "session_id": str(raw["session_id"]),
            "mode": _trace_enum(TRACE_MODES, int(raw["mode"]), "mode"),
            "lock_name": "lru_lock",
            "scope": _trace_enum(
                TRACE_LOCK_SCOPES, int(raw["scope"]), "lock scope"),
            "held_ns": int(raw["held_ns"]),
            "wait_ns": int(raw["wait_ns"]),
            "irq_disabled_ns": (int(raw["irq_disabled_ns"])
                                if raw["irq_disabled_measured"] else None),
            "wait_measured": True,
            "irq_disabled_measured": bool(raw["irq_disabled_measured"]),
        })
        stats["lock_latency_events"] += 1
    stats["decision_events"] = len(decisions)
    return records, dict(stats)


def load_sessions(path: Path) -> Dict[SessionKey, Dict[str, object]]:
    document = read_json(path)
    if not isinstance(document, Mapping):
        raise ContractError("session metadata must be an object")
    if document.get("schema_version") != SESSION_SCHEMA_VERSION:
        raise ContractError("unsupported session metadata document")
    raw_sessions = document.get("sessions")
    if not isinstance(raw_sessions, list):
        raise ContractError("session metadata requires a sessions array")

    result: Dict[SessionKey, Dict[str, object]] = {}
    for raw in raw_sessions:
        if not isinstance(raw, dict):
            raise ContractError("session metadata entries must be objects")
        validate_session(raw)
        key = session_key(raw)
        if key in result:
            raise ContractError("duplicate session metadata for %s/%s" % key)
        result[key] = dict(raw)
    if not result:
        raise ContractError("session metadata cannot be empty")
    return result


def _session_split(meta: Mapping[str, object], seed: str) -> str:
    explicit = meta.get("split")
    if explicit is not None:
        return str(explicit)
    return assign_session_split(normalized_id(meta, "experiment_id"),
                                normalized_id(meta, "session_id"), seed)


def _measurement_value(meta: Mapping[str, object], measurement: str,
                       value: str) -> Optional[int]:
    block = meta[measurement]
    assert isinstance(block, Mapping)
    raw = block[value]
    return int(raw) if raw is not None else None


def _measurement_delta(meta: Mapping[str, object], measurement: str,
                       delta_field: str) -> Tuple[Optional[int],
                                                 Optional[int],
                                                 Optional[int]]:
    block = meta[measurement]
    assert isinstance(block, Mapping)
    before = _measurement_value(meta, measurement, "before")
    after = _measurement_value(meta, measurement, "after")
    delta = _measurement_value(meta, measurement, delta_field)
    if not bool(block["measured"]):
        if before is not None or after is not None or delta is not None:
            raise ContractError(
                "unmeasured %s values must be null" % measurement)
        return None, None, None
    if before is None or after is None or delta is None:
        raise ContractError(
            "measured %s requires before/after/%s for %s/%s" %
            ((measurement, delta_field) + session_key(meta)))
    computed = after - before
    if computed < 0 or computed != delta:
        raise ContractError(
            "%s delta does not match before/after for %s/%s" %
            ((measurement,) + session_key(meta)))
    return before, after, computed


def _coverage(meta: Mapping[str, object], collected: int,
              trace_sequence_gap: bool = False) -> Dict[str, object]:
    gate = meta["tier_gate_counter"]
    trace = meta["trace_loss"]
    assert isinstance(gate, Mapping) and isinstance(trace, Mapping)
    gate_measured = bool(gate["measured"])
    loss_measured = bool(trace["measured"])
    gate_delta = _measurement_value(meta, "tier_gate_counter", "delta")
    trace_lost = _measurement_value(meta, "trace_loss", "lost")
    trace_before, trace_after, trace_delta = _measurement_delta(
        meta, "trace_loss", "lost")
    if gate_measured and gate_delta is not None and collected > gate_delta:
        raise ContractError(
            "collected candidates exceed measured gate count for %s/%s" %
            session_key(meta))
    trace_delta_zero = bool(loss_measured and trace_delta == 0)
    complete = bool(gate_measured and loss_measured and trace_delta_zero and
                    trace_lost == 0 and
                    gate_delta == collected and not trace_sequence_gap)
    reasons: List[str] = []
    if not gate_measured:
        reasons.append("tier_gate_counter_not_measured")
    elif gate_delta != collected:
        reasons.append("candidate_count_does_not_match_tier_gate_counter")
    if not loss_measured:
        reasons.append("trace_lost_not_measured")
    elif trace_lost:
        reasons.append("trace_lost_nonzero")
    if loss_measured and not trace_delta_zero:
        reasons.append("trace_loss_delta_nonzero")
    if trace_sequence_gap:
        reasons.append("trace_sequence_gap_detected")
    return {
        "candidate_records": collected,
        "tier_gate_counter_measured": gate_measured,
        "tier_gate_counter_delta": gate_delta,
        "trace_lost_measured": loss_measured,
        "trace_lost": trace_lost,
        "trace_loss_source": trace.get("source"),
        "trace_loss_before": trace_before,
        "trace_loss_after": trace_after,
        "trace_loss_delta": trace_delta,
        "trace_loss_delta_zero": trace_delta_zero,
        "trace_sequence_gap_detected": trace_sequence_gap,
        "complete": complete,
        "incomplete_reasons": reasons,
    }


def _next_access(accesses: Mapping[Tuple[str, str, str, int],
                                   Sequence[AccessPoint]],
                 candidate: Mapping[str, object],
                 observation_end_ns: int) -> Optional[AccessPoint]:
    points = accesses.get(folio_key(candidate), ())
    timestamps = [point[0] for point in points]
    index = bisect.bisect_right(timestamps, int(candidate["timestamp_ns"]))
    if index >= len(points) or points[index][0] > observation_end_ns:
        return None
    return points[index]


def _future_labels(candidate_ns: int, observation_end_ns: int,
                   access: Optional[AccessPoint]) -> Tuple[Dict[str, object],
                                                           Optional[int],
                                                           Optional[str]]:
    delay = access[0] - candidate_ns if access is not None else None
    source = access[1] if access is not None else None
    if delay is not None and delay <= 0:
        raise ContractError("future access must be strictly after its candidate")
    labels: Dict[str, object] = {}
    for name, window_ns in LABEL_WINDOWS_NS:
        if delay is not None and delay <= window_ns:
            labels[name] = True
        elif candidate_ns + window_ns <= observation_end_ns:
            labels[name] = False
        else:
            labels[name] = None
    return labels, delay, source


def _ranking_target(candidate_ns: int, observation_end_ns: int,
                    access: Optional[AccessPoint]) -> Dict[str, object]:
    delay = access[0] - candidate_ns if access is not None else None
    observed = delay is not None and delay <= RANKING_HORIZON_NS
    full_horizon = candidate_ns + RANKING_HORIZON_NS <= observation_end_ns
    return {
        "next_reuse_delay_ns": delay if observed else None,
        "observed_within_horizon": observed,
        "censored_by_session_end": not observed and not full_horizon,
        "horizon_ns": RANKING_HORIZON_NS,
        "tie_margin_ns": DEFAULT_RANKING_TIE_MARGIN_NS,
        "ranking_target_semantics":
            "NEXT_REAL_ACCESS_DELAY_RIGHT_CENSORED_AT_HORIZON",
    }


def build_dataset(
        records: Sequence[Mapping[str, object]],
        sessions: Mapping[SessionKey, Mapping[str, object]],
        split_seed: str = "parp-effective-tier-v1",
) -> Tuple[List[Dict[str, object]], List[Dict[str, object]], Dict[str, object]]:
    """Validate, join, label, and session-split exported observations."""

    candidates: List[Dict[str, object]] = []
    telemetry: List[Dict[str, object]] = []
    accesses: DefaultDict[Tuple[str, str, str, int], List[AccessPoint]] = (
        defaultdict(list))
    access_count = 0

    for record in records:
        kind = record.get("event_kind")
        if kind == "tier_gate_candidate":
            validate_candidate(record)
            candidates.append(dict(record))
        elif kind == "real_access":
            validate_access(record)
            accesses[folio_key(record)].append(
                (int(record["timestamp_ns"]), str(record["access_source"])))
            access_count += 1
        elif kind in TELEMETRY_KINDS:
            validate_telemetry(record)
            telemetry.append(dict(record))
        else:
            raise ContractError("unsupported event_kind %r" % kind)

    if not candidates:
        raise ContractError("no tier-gate candidates were supplied")
    candidate_identities = set()
    for candidate in candidates:
        identity = (session_key(candidate), folio_key(candidate),
                    int(candidate["timestamp_ns"]),
                    normalized_id(candidate, "batch_id"))
        if identity in candidate_identities:
            raise ContractError("duplicate tier-gate candidate record")
        candidate_identities.add(identity)
    for points in accesses.values():
        points.sort(key=lambda item: item[0])

    per_session_candidates: Counter[SessionKey] = Counter(
        session_key(candidate) for candidate in candidates)
    for key in per_session_candidates:
        if key not in sessions:
            raise ContractError("candidate has no session metadata: %s/%s" % key)
    for observation in telemetry:
        key = session_key(observation)
        if key not in sessions:
            raise ContractError("telemetry has no session metadata: %s/%s" % key)
        if observation["mode"] != sessions[key]["mode"]:
            raise ContractError("telemetry mode disagrees with session metadata")
    trace_gap_sessions = {
        session_key(candidate) for candidate in candidates
        if bool(candidate.get("raw_trace_lost_flag", False))
    }
    coverage = {
        key: _coverage(meta, per_session_candidates.get(key, 0),
                       key in trace_gap_sessions)
        for key, meta in sessions.items()
    }

    labeled: List[Dict[str, object]] = []
    for candidate in sorted(
            candidates,
            key=lambda item: (session_key(item), int(item["timestamp_ns"]),
                              int(item["source_seq"]))):
        key = session_key(candidate)
        meta = sessions[key]
        candidate_ns = int(candidate["timestamp_ns"])
        start_ns = int(meta["start_ns"])
        end_ns = int(meta["observation_end_ns"])
        if not start_ns <= candidate_ns <= end_ns:
            raise ContractError("candidate timestamp outside session observation")
        if "mode" in candidate and candidate["mode"] != meta["mode"]:
            raise ContractError("candidate mode disagrees with session metadata")

        classified = quadrant(bool(candidate["native_protect"]),
                              bool(candidate["effective_protect"]),
                              bool(candidate["special_native_protect"]))
        recorded = str(candidate["action"])
        if recorded != classified and not (
                recorded == "SPECIAL_NATIVE_PROTECT" and
                bool(candidate["special_native_protect"]) and
                classified == "KEEP_PROTECT"):
            raise ContractError(
                "recorded action %s disagrees with decision quadrant %s" %
                (recorded, classified))
        if classified == "PREDICTIVE_DOWNGRADE" and (
                bool(candidate["special_native_protect"]) or
                int(candidate["native_tier"]) !=
                int(candidate["native_tier_idx"]) + 1):
            raise ContractError("unsafe predictive downgrade in exported data")

        next_access = _next_access(accesses, candidate, end_ns)
        labels, delay, source = _future_labels(candidate_ns, end_ns,
                                                next_access)
        ranking_target = _ranking_target(candidate_ns, end_ns, next_access)
        session_coverage = coverage[key]
        trace_lost = session_coverage["trace_lost"]

        row = dict(candidate)
        row.update({
            "quadrant": classified,
            "split": _session_split(meta, split_seed),
            "app": meta["app"],
            "workload": meta["workload"],
            "mode": meta["mode"],
            "pressure_level": meta["pressure_level"],
            "labels": labels,
            "next_real_access_delay_ns": delay,
            "next_real_access_source": source,
            "label_semantics": "FUTURE_REAL_ACCESS_NOT_REFAULT",
            "trace_lost_measured": session_coverage["trace_lost_measured"],
            "trace_lost": trace_lost,
            "trace_sequence_gap_detected":
                session_coverage["trace_sequence_gap_detected"],
            "trace_loss_delta":
                session_coverage["trace_loss_delta"],
            "trace_loss_delta_zero":
                session_coverage["trace_loss_delta_zero"],
            "tier_gate_coverage_measured":
                session_coverage["tier_gate_counter_measured"],
            "tier_gate_coverage_complete": session_coverage["complete"],
        })
        row.update(ranking_target)
        labeled.append(row)

    session_rows = []
    for key, meta in sorted(sessions.items()):
        session_rows.append({
            "experiment_id": key[0],
            "session_id": key[1],
            "app": meta["app"],
            "workload": meta["workload"],
            "mode": meta["mode"],
            "pressure_level": meta["pressure_level"],
            "split": _session_split(meta, split_seed),
            "coverage": coverage[key],
        })

    label_counts: Dict[str, Dict[str, int]] = {}
    for name, _window_ns in LABEL_WINDOWS_NS:
        known_pages = 0
        positive_pages = 0
        censored_pages = 0
        for row in labeled:
            pages = int(row["folio_nr_pages"])
            value = row["labels"][name]  # type: ignore[index]
            if value is None:
                censored_pages += pages
            else:
                known_pages += pages
                if value:
                    positive_pages += pages
        label_counts[name] = {
            "known_base_pages": known_pages,
            "positive_base_pages": positive_pages,
            "censored_base_pages": censored_pages,
        }

    trace_measured = all(bool(item["coverage"]["trace_lost_measured"])
                         for item in session_rows)
    trace_lost_total = (sum(int(item["coverage"]["trace_lost"] or 0)
                            for item in session_rows)
                        if trace_measured else None)
    trace_loss_delta_measured = all(
        item["coverage"]["trace_loss_delta"] is not None
        for item in session_rows)
    trace_loss_delta_total = (
        sum(int(item["coverage"]["trace_loss_delta"] or 0)
            for item in session_rows)
        if trace_loss_delta_measured else None)
    all_complete = all(bool(item["coverage"]["complete"])
                       for item in session_rows)
    warnings: List[str] = []
    if not trace_measured:
        warnings.append("trace_lost was not measured for every session")
    if trace_lost_total:
        warnings.append("trace_lost is nonzero; dataset completeness is not proven")
    if not trace_loss_delta_measured:
        warnings.append("trace_loss before/after delta was not measured for every session")
    elif trace_loss_delta_total:
        warnings.append("trace_loss before/after delta is nonzero; dataset completeness is not proven")
    if trace_gap_sessions:
        warnings.append(
            "the monotonic effective-tier trace sequence has a gap")
    if not all_complete:
        warnings.append("full native tier-gate candidate coverage is not proven")

    summary: Dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "status": ("PARP_EFFECTIVE_TIER_OFFLINE_DATASET_READY"
                   if all_complete else
                   "PARP_EFFECTIVE_TIER_OFFLINE_DATASET_INCOMPLETE"),
        "offline_only": True,
        "candidate_scope": "ALL_NATIVE_TIER_GATE_FOLIOS",
        "label_semantics": "FUTURE_REAL_ACCESS_NOT_REFAULT",
        "candidate_records": len(labeled),
        "candidate_base_pages": sum(int(row["folio_nr_pages"])
                                    for row in labeled),
        "real_access_events": access_count,
        "telemetry_records": len(telemetry),
        "session_count": len(session_rows),
        "session_splits": dict(Counter(str(item["split"])
                                       for item in session_rows)),
        "trace_lost_measured": trace_measured,
        "trace_lost": trace_lost_total,
        "trace_sequence_gap_detected": bool(trace_gap_sessions),
        "trace_loss_delta_measured": trace_loss_delta_measured,
        "trace_loss_delta": trace_loss_delta_total,
        "trace_loss_delta_zero": (trace_loss_delta_total == 0
                                  if trace_loss_delta_total is not None else False),
        "tier_gate_coverage_complete": all_complete,
        "labels": label_counts,
        "sessions": session_rows,
        "warnings": warnings,
        "safety": {
            "live_interfaces_read": False,
            "cgroup_writes": False,
            "mode_writes": False,
            "pressure_started": False,
            "apply_enabled": False,
        },
    }
    return labeled, telemetry, summary


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Label exported effective-tier events; offline files only")
    parser.add_argument("--events", action="append", type=Path,
                        help="exported candidate/access/telemetry JSONL")
    parser.add_argument("--trace-text", action="append", type=Path,
                        help="already-exported Linux trace text (never tracefs)")
    parser.add_argument("--sessions", required=True, type=Path,
                        help="exported session metadata JSON")
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--split-seed", default="parp-effective-tier-v1")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    try:
        if not args.events and not args.trace_text:
            raise ContractError("at least one --events or --trace-text is required")
        input_paths = list(args.events or []) + list(args.trace_text or [])
        for path in input_paths + [args.sessions, args.output_dir]:
            reject_live_path(path)
        sessions = load_sessions(args.sessions)
        records = read_jsonl(args.events or [])
        trace_normalization = None
        if args.trace_text:
            normalized, trace_normalization = parse_exported_trace(args.trace_text)
            records.extend(normalized)
        labeled, telemetry, summary = build_dataset(
            records, sessions, split_seed=args.split_seed)
        if trace_normalization is not None:
            summary["exported_trace_normalization"] = trace_normalization
        args.output_dir.mkdir(parents=True, exist_ok=True)
        write_jsonl(args.output_dir / "labeled_candidates.jsonl", labeled)
        write_jsonl(args.output_dir / "observability.jsonl", telemetry)
        write_json(args.output_dir / "collection_summary.json", summary)
        write_json(args.output_dir / "session_splits.json", {
            "schema_version": SCHEMA_VERSION,
            "split_unit": "session",
            "split_seed": args.split_seed,
            "sessions": [
                {
                    "experiment_id": item["experiment_id"],
                    "session_id": item["session_id"],
                    "split": item["split"],
                }
                for item in summary["sessions"]  # type: ignore[union-attr]
            ],
        })
    except ContractError as exc:
        print("collector: %s" % exc, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
