#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0
"""Phase-E offline contracts for PARP effective-tier experiments.

This module intentionally has no tracefs, debugfs, cgroup, pressure-generator,
or mode-setter integration.  It validates already-exported records only.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


SCHEMA_VERSION = 2
SESSION_SCHEMA_VERSION = 1
FEATURE_SCHEMA_VERSION = 1

BASE_FEATURES = (
    "time_since_last_real_access_ms",
    "previous_real_access_interval_ms",
    "reuse_interval_ema_ms",
    "consecutive_reclaim_candidate_count",
    "time_in_current_generation_ms",
    "access_ema_q8",
)
NATIVE_TIER_FEATURE = "native_tier"
TIER_IDX_FEATURE = "native_tier_idx"

FEATURE_EDGES = {
    "time_since_last_real_access_ms": (10, 100, 500, 2000, 10000),
    "previous_real_access_interval_ms": (10, 100, 500, 2000, 10000),
    "reuse_interval_ema_ms": (10, 100, 500, 2000, 10000),
    "consecutive_reclaim_candidate_count": (0, 1, 2, 4, 8),
    "time_in_current_generation_ms": (10, 100, 500, 2000, 10000),
    "access_ema_q8": (8, 32, 96, 160, 224),
    NATIVE_TIER_FEATURE: (0, 1, 2),
    TIER_IDX_FEATURE: (0, 1, 2),
}

MODEL_ABLATIONS = (
    ("global_no_native_tier", BASE_FEATURES),
    ("global_plus_native_tier", BASE_FEATURES + (NATIVE_TIER_FEATURE,)),
    ("global_plus_native_tier_and_tier_idx",
     BASE_FEATURES + (NATIVE_TIER_FEATURE, TIER_IDX_FEATURE)),
)

LABEL_WINDOWS_NS = (
    ("reuse_within_100ms", 100_000_000),
    ("reuse_within_500ms", 500_000_000),
    ("reuse_within_1s", 1_000_000_000),
    ("reuse_within_5s", 5_000_000_000),
)
PRIMARY_LABEL = "reuse_within_1s"
RANKING_HORIZON_NS = 5_000_000_000
RANKING_TIE_MARGINS_NS = (0, 10_000_000, 50_000_000)
DEFAULT_RANKING_TIE_MARGIN_NS = 10_000_000

REAL_ACCESS_SOURCES = frozenset((
    "PTE_YOUNG",
    "MARK_ACCESSED",
    "FD_REFERENCE",
))

QUADRANTS = (
    "KEEP_RECLAIM",
    "PREDICTIVE_UPGRADE",
    "KEEP_PROTECT",
    "PREDICTIVE_DOWNGRADE",
)

RECORDED_ACTIONS = frozenset(QUADRANTS + ("SPECIAL_NATIVE_PROTECT",))
DELTA_TIER_Q8_VALUES = frozenset((-256, 0, 256, 512, 768))
MODES = frozenset((
    "OFF",
    "SHADOW_EFFECTIVE_TIER",
    "APPLY_PROTECT_ONLY",
    "APPLY_BIDIRECTIONAL",
    "APPLY_RANDOM_MATCHED",
    "APPLY_RECENCY_BASELINE",
    "PRODUCT_ABLATION",
    "ORACLE_OFFLINE_ONLY",
))
PAGE_TYPES = frozenset(("anon", "file"))
SPLITS = frozenset(("train", "validation", "test"))

TELEMETRY_KINDS = frozenset((
    "score_latency",
    "lock_latency",
    "reclaim_latency",
    "reclaim_efficiency",
    "app_latency",
    "app_session_summary",
    "vm_counter_delta",
    "trace_loss",
))


class ContractError(ValueError):
    """An input cannot safely be used as a Phase-E offline record."""


def _require(record: Mapping[str, object], names: Iterable[str],
             context: str) -> None:
    missing = [name for name in names if name not in record]
    if missing:
        raise ContractError("%s missing fields: %s" %
                            (context, ", ".join(sorted(missing))))


def _integer(value: object, name: str, minimum: Optional[int] = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ContractError("%s must be an integer" % name)
    if minimum is not None and value < minimum:
        raise ContractError("%s must be >= %d" % (name, minimum))
    return value


def _boolean(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise ContractError("%s must be a boolean" % name)
    return value


def _identifier(value: object, name: str) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ContractError("%s must be a string or integer identifier" % name)
    result = str(value)
    if not result:
        raise ContractError("%s cannot be empty" % name)
    return result


def validate_candidate(record: Mapping[str, object]) -> None:
    """Validate one record emitted for a folio reaching the native tier gate."""

    required = (
        "schema_version", "event_kind", "timestamp_ns", "experiment_id",
        "session_id", "folio_cookie", "folio_lifetime_epoch",
        "memcg_anon_id", "nid", "page_type", "source_seq",
        "generation_index", "native_tier", "native_tier_idx",
        "special_native_protect", "native_protect", "mode", "features",
        "model_valid", "features_valid",
        "model_type", "model_version", "expected_model_version",
        "feature_schema_version", "model_checksum", "model_provenance",
        "pairwise_model_checksum", "rank_score_bin", "score_percentile",
        "reuse_score", "cold_threshold", "hot_threshold_1",
        "hot_threshold_2", "hot_threshold_3", "delta_tier_q8",
        "effective_tier_q8",
        "score_threshold_cold", "score_threshold_hot_1",
        "score_threshold_hot_2", "score_threshold_hot_3",
        "predictive_delta_tier_q8",
        "effective_protect", "action", "bypass_reason",
        "folio_nr_pages", "batch_id", "reclaim_epoch", "priority",
        "score_duration_ns", "actual_native_behavior", "isolate_result",
        "reclaimed", "putback", "activated", "gate_reached",
        "candidate_scope",
    )
    _require(record, required, "tier candidate")
    if record["schema_version"] != SCHEMA_VERSION:
        raise ContractError("unsupported candidate schema_version")
    if record["event_kind"] != "tier_gate_candidate":
        raise ContractError("candidate event_kind must be tier_gate_candidate")
    _integer(record["timestamp_ns"], "timestamp_ns", 0)
    for field in ("experiment_id", "session_id", "folio_cookie",
                  "memcg_anon_id", "batch_id", "reclaim_epoch"):
        _identifier(record[field], field)
    _integer(record["folio_lifetime_epoch"], "folio_lifetime_epoch", 0)
    _integer(record["nid"], "nid", 0)
    _integer(record["source_seq"], "source_seq", 0)
    _integer(record["generation_index"], "generation_index", 0)
    native_tier = _integer(record["native_tier"], "native_tier", 0)
    tier_idx = _integer(record["native_tier_idx"], "native_tier_idx", 0)
    if native_tier > 3 or tier_idx > 3:
        raise ContractError("native tier values must be in [0, 3]")
    if record["page_type"] not in PAGE_TYPES:
        raise ContractError("page_type must be anon or file")
    if record["mode"] not in MODES:
        raise ContractError("unknown candidate mode")
    if record["candidate_scope"] != "ALL_NATIVE_TIER_GATE_FOLIOS":
        raise ContractError("candidate scope does not cover the full tier gate")
    if _boolean(record["gate_reached"], "gate_reached") is not True:
        raise ContractError("candidate did not reach the native tier gate")
    for field in ("special_native_protect", "native_protect",
                  "model_valid", "features_valid",
                  "effective_protect"):
        _boolean(record[field], field)
    model_types = {
        "pairwise_linear_ranker",
        "recency_baseline",
        "random_matched_baseline",
    }
    if record["model_type"] not in model_types:
        raise ContractError("unknown candidate scorer type")
    model_version = _integer(record["model_version"], "model_version", 1)
    expected_model_version = _integer(
        record["expected_model_version"], "expected_model_version", 1)
    feature_schema_version = _integer(
        record["feature_schema_version"], "feature_schema_version", 1)
    checksum = record["model_checksum"]
    if record["model_type"] == "pairwise_linear_ranker":
        if not isinstance(checksum, str) or not re.fullmatch(
                r"[0-9a-f]{64}", checksum):
            raise ContractError("model_checksum must be lowercase SHA-256")
        if record["pairwise_model_checksum"] != checksum:
            raise ContractError("pairwise_model_checksum alias disagrees")
        if record["model_provenance"] not in (
                "ENGINEERING_FIXTURE_UNTRAINED",
                "TRAINED_PAIRWISE_OFFLINE"):
            raise ContractError("invalid pairwise model provenance")
    else:
        if checksum is not None or record["pairwise_model_checksum"] is not None:
            raise ContractError("control baselines cannot claim a model checksum")
        if record["model_provenance"] != "CONTROL_BASELINE":
            raise ContractError("invalid control-baseline provenance")
    expected_type = {
        "APPLY_RANDOM_MATCHED": "random_matched_baseline",
        "APPLY_RECENCY_BASELINE": "recency_baseline",
    }.get(str(record["mode"]), "pairwise_linear_ranker")
    if record["model_type"] != expected_type:
        raise ContractError("candidate mode disagrees with scorer identity")
    if record["score_percentile"] is not None:
        raise ContractError("raw score_percentile must be added offline")
    if not record["features_valid"] and record["model_valid"]:
        raise ContractError("model cannot be valid when features are invalid")
    if record["model_valid"] and (
            model_version != expected_model_version or
            feature_schema_version != FEATURE_SCHEMA_VERSION):
        raise ContractError(
            "valid scorer identity must match expected model/schema versions")
    for field in ("reclaimed", "putback", "activated"):
        if record[field] is not None:
            _boolean(record[field], field)
    for field in ("reuse_score", "delta_tier_q8", "effective_tier_q8"):
        _integer(record[field], field)
    _integer(record["priority"], "priority")
    thresholds = (record["cold_threshold"], record["hot_threshold_1"],
                  record["hot_threshold_2"], record["hot_threshold_3"])
    if record["model_valid"]:
        for name, value in zip(("cold_threshold", "hot_threshold_1",
                                "hot_threshold_2", "hot_threshold_3"),
                               thresholds):
            _integer(value, name)
        if not all(left < right for left, right in
                   zip(thresholds, thresholds[1:])):
            raise ContractError("tier thresholds must be strictly increasing")
    elif any(value is not None for value in thresholds):
        raise ContractError("invalid model thresholds must be null")
    rank_score_bin = record["rank_score_bin"]
    expected_bin: Optional[int] = None
    if record["model_valid"]:
        score = _integer(record["reuse_score"], "reuse_score")
        expected_bin = (0 if score <= record["cold_threshold"] else
                        1 if score < record["hot_threshold_1"] else
                        2 if score < record["hot_threshold_2"] else
                        3 if score < record["hot_threshold_3"] else 4)
        if _integer(rank_score_bin, "rank_score_bin", 0) != expected_bin:
            raise ContractError("rank_score_bin disagrees with score thresholds")
    elif rank_score_bin is not None:
        raise ContractError("invalid model must not export a rank_score_bin")
    if not record["model_valid"]:
        native_q8 = int(record["native_tier"]) * 256
        if (record["delta_tier_q8"] != 0 or
                record["predictive_delta_tier_q8"] != 0 or
                record["effective_tier_q8"] != native_q8 or
                record["effective_protect"] != record["native_protect"]):
            raise ContractError("invalid model must fall back exactly to Native")
    aliases = (
        ("cold_threshold", "score_threshold_cold"),
        ("hot_threshold_1", "score_threshold_hot_1"),
        ("hot_threshold_2", "score_threshold_hot_2"),
        ("hot_threshold_3", "score_threshold_hot_3"),
        ("delta_tier_q8", "predictive_delta_tier_q8"),
    )
    for legacy, canonical in aliases:
        if record[legacy] != record[canonical]:
            raise ContractError("deprecated %s disagrees with %s" %
                                (legacy, canonical))
    delta_q8 = int(record["predictive_delta_tier_q8"])
    if delta_q8 not in DELTA_TIER_Q8_VALUES:
        raise ContractError("predictive_delta_tier_q8 is not a legal tier delta")
    if record["model_valid"] and record["bypass_reason"] == "NONE":
        assert expected_bin is not None
        legal_delta_by_bin = {
            0: frozenset((-256,)),
            1: frozenset((0,)),
            2: frozenset((256,)),
            # The trace does not carry max_upgrade_tiers.  Hot-2 and hot-3
            # therefore admit every value the kernel can produce after its
            # configured +1/+2/+3 cap, but no value from another score bin.
            3: frozenset((256, 512)),
            4: frozenset((256, 512, 768)),
        }
        if delta_q8 not in legal_delta_by_bin[expected_bin]:
            raise ContractError(
                "predictive tier delta disagrees with rank score bin")
    elif record["bypass_reason"] != "NONE" and delta_q8 != 0:
        raise ContractError("bypassed decision must use the Native delta")

    native_protect = native_tier > tier_idx
    if record["native_protect"] is not native_protect:
        raise ContractError("native_protect disagrees with native tier")
    expected_effective_q8 = min(max(native_tier * 256 + delta_q8, 0),
                                3 * 256)
    if record["effective_tier_q8"] != expected_effective_q8:
        raise ContractError("effective_tier_q8 disagrees with clamped delta")
    effective_protect = expected_effective_q8 > tier_idx * 256
    if record["effective_protect"] is not effective_protect:
        raise ContractError(
            "effective_protect disagrees with effective tier comparison")
    if record["special_native_protect"]:
        expected_action = "SPECIAL_NATIVE_PROTECT"
    elif not native_protect and effective_protect:
        expected_action = "PREDICTIVE_UPGRADE"
    elif native_protect and not effective_protect:
        expected_action = "PREDICTIVE_DOWNGRADE"
    elif native_protect:
        expected_action = "KEEP_PROTECT"
    else:
        expected_action = "KEEP_RECLAIM"
    if record["action"] != expected_action:
        raise ContractError("action disagrees with effective-tier quadrant")
    _integer(record["folio_nr_pages"], "folio_nr_pages", 1)
    _integer(record["score_duration_ns"], "score_duration_ns", 0)
    if record["action"] not in RECORDED_ACTIONS:
        raise ContractError("unknown effective-tier action")
    if not isinstance(record["bypass_reason"], str):
        raise ContractError("bypass_reason must be a string")
    if record["actual_native_behavior"] not in ("protect", "reclaim",
                                                 "unknown"):
        raise ContractError("invalid actual_native_behavior")
    if record["isolate_result"] not in ("not_attempted", "succeeded",
                                        "failed", "unknown"):
        raise ContractError("invalid isolate_result")

    features = record["features"]
    if not isinstance(features, Mapping):
        raise ContractError("features must be an object")
    _require(features, BASE_FEATURES, "candidate features")
    features_valid = bool(record["features_valid"])
    for name in BASE_FEATURES:
        value = features[name]
        if features_valid:
            if value is None:
                raise ContractError(
                    "features_valid requires all six feature values")
            _integer(value, "features.%s" % name, 0)
        elif value is not None:
            raise ContractError(
                "invalid metadata must export null, not fabricated features")


def validate_access(record: Mapping[str, object]) -> None:
    """Validate a post-candidate real-access event used for future labels."""

    _require(record, (
        "schema_version", "event_kind", "timestamp_ns", "experiment_id",
        "session_id", "folio_cookie", "folio_lifetime_epoch",
        "access_source", "is_real_access", "candidate_count",  #lzx
    ), "real access")
    if record["schema_version"] != SCHEMA_VERSION:
        raise ContractError("unsupported access schema_version")
    if record["event_kind"] != "real_access":
        raise ContractError("access event_kind must be real_access")
    _integer(record["timestamp_ns"], "timestamp_ns", 0)
    for field in ("experiment_id", "session_id", "folio_cookie"):
        _identifier(record[field], field)
    _integer(record["folio_lifetime_epoch"], "folio_lifetime_epoch", 0)
    if _boolean(record["is_real_access"], "is_real_access") is not True:
        raise ContractError("future labels require a real access signal")
    if _integer(record["candidate_count"], "candidate_count", 1) < 1:  #lzx
        raise ContractError("future labels require candidate-correlated access")  #lzx
    if record["access_source"] not in REAL_ACCESS_SOURCES:
        raise ContractError("policy/generation moves cannot label future reuse")


def validate_session(record: Mapping[str, object]) -> None:
    """Validate per-session coverage and measured trace-loss provenance."""

    _require(record, (
        "schema_version", "experiment_id", "session_id", "app",
        "workload", "mode", "pressure_level", "start_ns",
        "observation_end_ns", "tier_gate_counter", "trace_loss",
    ), "session")
    if record["schema_version"] != SESSION_SCHEMA_VERSION:
        raise ContractError("unsupported session schema_version")
    for field in ("experiment_id", "session_id"):
        _identifier(record[field], field)
    if record["app"] not in ("WPS", "FILES", "QQ", "OTHER"):
        raise ContractError("app must be WPS, FILES, QQ, or OTHER")
    if not isinstance(record["workload"], str) or not record["workload"]:
        raise ContractError("workload must be a non-empty string")
    if record["mode"] not in MODES:
        raise ContractError("unknown session mode")
    if record["pressure_level"] not in ("P0", "P1", "P2", "P3", "P4"):
        raise ContractError("unknown pressure level")
    start = _integer(record["start_ns"], "start_ns", 0)
    end = _integer(record["observation_end_ns"], "observation_end_ns", 0)
    if end <= start:
        raise ContractError("observation_end_ns must be after start_ns")

    gate = record["tier_gate_counter"]
    trace = record["trace_loss"]
    if not isinstance(gate, Mapping) or not isinstance(trace, Mapping):
        raise ContractError("coverage measurements must be objects")
    _validate_delta_measurement(gate, "tier_gate_counter", "delta")
    _validate_delta_measurement(trace, "trace_loss", "lost")
    _validate_per_cpu_loss(trace, "trace_loss")
    if "split" in record and record["split"] not in SPLITS:
        raise ContractError("session split must be train, validation, or test")


def _validate_delta_measurement(measurement: Mapping[str, object],
                                name: str, delta_field: str) -> None:
    _require(measurement, ("measured", "source", "before", "after",
                           delta_field), name)
    measured = _boolean(measurement["measured"], "%s.measured" % name)
    if not measured:
        if any(measurement[field] is not None for field in
               ("before", "after", delta_field)):
            raise ContractError("unmeasured %s values must be null" % name)
        return
    if not isinstance(measurement["source"], str) or not measurement["source"]:
        raise ContractError("measured %s requires a source" % name)
    before = _integer(measurement["before"], "%s.before" % name, 0)
    after = _integer(measurement["after"], "%s.after" % name, 0)
    delta = _integer(measurement[delta_field],
                     "%s.%s" % (name, delta_field), 0)
    if after < before or delta != after - before:
        raise ContractError("%s delta does not match before/after" % name)


def _validate_per_cpu_loss(measurement: Mapping[str, object], name: str) -> None:
    per_cpu = measurement.get("per_cpu")
    if per_cpu is None:
        return
    if not isinstance(per_cpu, Mapping):
        raise ContractError("%s.per_cpu must be an object" % name)
    total = 0
    for cpu, value in per_cpu.items():
        if not str(cpu):
            raise ContractError("%s.per_cpu has an empty CPU identifier" % name)
        total += _integer(value, "%s.per_cpu.%s" % (name, cpu), 0)
    if measurement.get("measured") and total != measurement.get("lost"):
        raise ContractError("%s per-CPU total does not match lost" % name)


def validate_telemetry(record: Mapping[str, object]) -> None:
    """Validate one offline latency/counter observation."""

    _require(record, ("schema_version", "event_kind", "timestamp_ns",
                      "experiment_id", "session_id", "mode"), "telemetry")
    if record["schema_version"] != SCHEMA_VERSION:
        raise ContractError("unsupported telemetry schema_version")
    kind = record["event_kind"]
    if kind not in TELEMETRY_KINDS:
        raise ContractError("unknown telemetry event_kind")
    _integer(record["timestamp_ns"], "timestamp_ns", 0)
    _identifier(record["experiment_id"], "experiment_id")
    _identifier(record["session_id"], "session_id")
    if record["mode"] not in MODES:
        raise ContractError("unknown telemetry mode")

    if kind == "score_latency":
        _require(record, ("component", "duration_ns"), kind)
        if record["component"] not in ("score", "effective_tier",
                                       "quadrant_classification",
                                       "batch_model_total"):
            raise ContractError("unknown score latency component")
        _integer(record["duration_ns"], "duration_ns", 0)
    elif kind == "lock_latency":
        _require(record, ("lock_name", "scope", "held_ns", "wait_ns",
                          "irq_disabled_ns", "wait_measured",
                          "irq_disabled_measured"), kind)
        if record["lock_name"] != "lru_lock":
            raise ContractError("only lru_lock is in the Phase-E contract")
        if record["scope"] not in ("scan_folios", "batch"):
            raise ContractError("invalid lock latency scope")
        _integer(record["held_ns"], "held_ns", 0)
        for field, measured_field in (
                ("wait_ns", "wait_measured"),
                ("irq_disabled_ns", "irq_disabled_measured")):
            measured = _boolean(record[measured_field], measured_field)
            if measured:
                _integer(record[field], field, 0)
            elif record[field] is not None:
                raise ContractError("unmeasured %s must be null" % field)
    elif kind == "reclaim_latency":
        _require(record, ("scope", "duration_ns"), kind)
        if record["scope"] not in (
                "direct_reclaim", "memcg_reclaim", "kswapd_round",
                "isolate_batch", "shrink_folio_list", "full_request"):
            raise ContractError("invalid reclaim latency scope")
        _integer(record["duration_ns"], "duration_ns", 0)
    elif kind == "reclaim_efficiency":
        required = (
            "scanned", "isolated", "reclaimed", "native_protected",
            "predictive_upgraded", "predictive_downgraded", "pgscan",
            "pgsteal", "no_progress_rounds", "priority_drops",
            "younger_generation_moves",
        )
        _require(record, required, kind)
        for field in required:
            _integer(record[field], field, 0)
    elif kind == "app_latency":
        _require(record, ("app", "operation", "duration_ns", "success"),
                 kind)
        if record["app"] not in ("WPS", "FILES", "QQ", "OTHER"):
            raise ContractError("invalid app latency app")
        if not isinstance(record["operation"], str) or not record["operation"]:
            raise ContractError("operation must be a non-empty string")
        _integer(record["duration_ns"], "duration_ns", 0)
        _boolean(record["success"], "success")
    elif kind == "app_session_summary":
        _require(record, ("app", "total_duration_ns", "stalls", "timeouts",
                          "failures"), kind)
        if record["app"] not in ("WPS", "FILES", "QQ", "OTHER"):
            raise ContractError("invalid App session summary app")
        for field in ("total_duration_ns", "stalls", "timeouts", "failures"):
            _integer(record[field], field, 0)
    elif kind == "vm_counter_delta":
        _require(record, ("counter", "delta"), kind)
        if record["counter"] not in (
                "workingset_refault_file", "workingset_refault_anon",
                "pgfault", "pgmajfault", "swapin", "swapout",
                "psi_some_us", "psi_full_us", "memory_high",
                "memory_oom", "memory_oom_kill"):
            raise ContractError("invalid VM counter")
        _integer(record["delta"], "delta")
    elif kind == "trace_loss":
        _require(record, ("measured", "source", "before", "after", "lost"),
                 kind)
        _validate_delta_measurement(record, kind, "lost")
        _validate_per_cpu_loss(record, kind)


def normalized_id(record: Mapping[str, object], field: str) -> str:
    """Return an identifier in a precision-preserving comparison form."""

    return _identifier(record[field], field)


def session_key(record: Mapping[str, object]) -> Tuple[str, str]:
    return (normalized_id(record, "experiment_id"),
            normalized_id(record, "session_id"))


def folio_key(record: Mapping[str, object]) -> Tuple[str, str, str, int]:
    return (normalized_id(record, "experiment_id"),
            normalized_id(record, "session_id"),
            normalized_id(record, "folio_cookie"),
            _integer(record["folio_lifetime_epoch"],
                     "folio_lifetime_epoch", 0))


def quadrant(native_protect: bool, effective_protect: bool,
             special_native_protect: bool) -> str:
    """Classify ordinary and special protection into the required quadrants."""

    native_actual = native_protect or special_native_protect
    effective_actual = effective_protect or special_native_protect
    if not native_actual and not effective_actual:
        return "KEEP_RECLAIM"
    if not native_actual and effective_actual:
        return "PREDICTIVE_UPGRADE"
    if native_actual and effective_actual:
        return "KEEP_PROTECT"
    return "PREDICTIVE_DOWNGRADE"


def assign_session_split(experiment_id: str, session_id: str,
                         seed: str = "parp-effective-tier-v1") -> str:
    """Deterministically split whole sessions, never individual folio rows."""

    payload = (seed + "\x00" + experiment_id + "\x00" + session_id).encode(
        "utf-8")
    bucket = int.from_bytes(hashlib.sha256(payload).digest()[:8], "big") % 10000
    if bucket < 7000:
        return "train"
    if bucket < 8500:
        return "validation"
    return "test"


def read_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError("cannot read JSON %s: %s" % (path, exc)) from exc


def read_jsonl(paths: Sequence[Path]) -> List[Dict[str, object]]:
    records: List[Dict[str, object]] = []
    for path in paths:
        try:
            with path.open("r", encoding="utf-8") as stream:
                for line_number, line in enumerate(stream, 1):
                    if not line.strip():
                        continue
                    value = json.loads(line)
                    if not isinstance(value, dict):
                        raise ContractError("%s:%d is not an object" %
                                            (path, line_number))
                    records.append(value)
        except ContractError:
            raise
        except (OSError, json.JSONDecodeError) as exc:
            raise ContractError("cannot read JSONL %s: %s" % (path, exc)) from exc
    return records


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8")


def write_jsonl(path: Path, records: Iterable[Mapping[str, object]]) -> None:
    with path.open("w", encoding="utf-8") as stream:
        for record in records:
            stream.write(json.dumps(record, sort_keys=True) + "\n")


def reject_live_path(path: Path) -> None:
    """Reject interfaces that could turn this offline tool into a live actor."""

    resolved = path.expanduser().resolve()
    forbidden = (Path("/sys"), Path("/proc"), Path("/dev"), Path("/run"))
    for prefix in forbidden:
        try:
            resolved.relative_to(prefix)
        except ValueError:
            continue
        raise ContractError("live kernel/control paths are forbidden: %s" % path)
