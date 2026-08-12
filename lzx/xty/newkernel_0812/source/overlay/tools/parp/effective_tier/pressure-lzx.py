#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0  #lzx
"""Pure Phase-F pressure-aware SHADOW counterfactuals.

The module intentionally accepts already-exported candidate rows only.  It
does not inspect PSI, cgroups, tracefs, or a running kernel.  Kernel pressure
levels come from low-cost reclaim-local signals; user-space PSI is joined only
after collection for calibration and evaluation.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from dataclasses import dataclass
from enum import IntEnum
from typing import Dict, Iterable, List, Mapping, Optional, Tuple

try:
    from .reference import MAX_TIER, TIER_SCALE, effective_tier_q8
except ImportError:  # Direct execution through a -lzx tool entry point.
    from reference import MAX_TIER, TIER_SCALE, effective_tier_q8  # type: ignore


PRESSURE_POLICY_VERSION = 1
PRESSURE_POLICY_PROVENANCE = "ENGINEERING_PRESSURE_POLICY_UNVALIDATED"
PRESSURE_POLICY_IDS = (
    "FIXED_PROTECT_ONLY",
    "FIXED_BIDIRECTIONAL",
    "PRESSURE_AWARE_PROTECT_ONLY",
    "PRESSURE_AWARE_BIDIRECTIONAL",
    "BINARY_BYPASS",
    "RECENCY_ONLY_SHADOW",
    "RANDOM_RATE_MATCHED_SHADOW",
)
PRIMARY_FUTURE_ACCESS_LABEL = "reuse_within_1s"
RANDOM_RATE_MATCHED_SEED = "parp-effective-tier-pressure-v1"


class PressureLevel(IntEnum):
    LOW = 0
    MEDIUM = 1
    HIGH = 2
    CRITICAL = 3


class ReclaimContext(IntEnum):
    UNKNOWN = 0
    KSWAPD = 1
    DIRECT = 2
    MEMCG = 3
    PROACTIVE_MEMCG = 4


@dataclass(frozen=True)
class PressureCounterfactual:
    """All Phase-F decisions are observations; none changes Native state."""

    fixed_delta_q8: int
    binary_bypass_delta_q8: int
    pressure_aware_delta_q8: int
    fixed_effective_protect: bool
    pressure_aware_effective_protect: bool
    pressure_level_kernel: PressureLevel
    pressure_bypass_reason: str


def _scales(level: PressureLevel) -> Tuple[int, int]:
    """Return Q8 (upgrade, downgrade) scales matching the kernel matrix."""

    return {
        PressureLevel.LOW: (256, 128),
        PressureLevel.MEDIUM: (192, 256),
        PressureLevel.HIGH: (64, 256),
        PressureLevel.CRITICAL: (0, 0),
    }[level]


def _scale_signed_q8(delta_q8: int, scale_q8: int) -> int:
    """C ``div_s64`` semantics: divide signed values toward zero."""

    magnitude = abs(delta_q8) * scale_q8 // TIER_SCALE
    return -magnitude if delta_q8 < 0 else magnitude


def pressure_level_from_local_signals(reclaim_priority: int,
                                      no_progress: bool,
                                      nr_to_reclaim: int,
                                      nr_reclaimed: int) -> PressureLevel:
    """Mirror the C local-only classifier without user-space observations."""

    if no_progress or reclaim_priority <= 2:
        return PressureLevel.CRITICAL
    if reclaim_priority <= 4:
        return PressureLevel.HIGH
    if reclaim_priority <= 8 or nr_to_reclaim > nr_reclaimed:
        return PressureLevel.MEDIUM
    return PressureLevel.LOW


def counterfactual_deltas(raw_delta_q8: int, native_tier: int,
                          native_tier_idx: int,
                          special_native_protect: bool,
                          model_valid: bool,
                          level: PressureLevel) -> PressureCounterfactual:
    """Evaluate fixed, binary-bypass, and graded SHADOW-only policies."""

    if not 0 <= native_tier <= MAX_TIER or not 0 <= native_tier_idx <= MAX_TIER:
        raise ValueError("native tier is outside the Q8 effective-tier domain")
    if raw_delta_q8 not in (-TIER_SCALE, 0, TIER_SCALE,
                            2 * TIER_SCALE, 3 * TIER_SCALE):
        raise ValueError("raw delta is not a declared Q8 tier mapping")

    fixed = raw_delta_q8 if model_valid and not special_native_protect else 0
    if fixed < 0 and native_tier != native_tier_idx + 1:
        fixed = 0
    binary = 0 if level is PressureLevel.CRITICAL else fixed
    up_scale, down_scale = _scales(level)
    graded = _scale_signed_q8(fixed, down_scale if fixed < 0 else up_scale)
    fixed_protect = effective_tier_q8(native_tier, fixed) > (
        native_tier_idx * TIER_SCALE)
    graded_protect = effective_tier_q8(native_tier, graded) > (
        native_tier_idx * TIER_SCALE)
    bypass = ("NO_PROGRESS_OR_CRITICAL" if level is PressureLevel.CRITICAL
              else "NONE")
    return PressureCounterfactual(
        fixed_delta_q8=fixed,
        binary_bypass_delta_q8=binary,
        pressure_aware_delta_q8=graded,
        fixed_effective_protect=fixed_protect,
        pressure_aware_effective_protect=graded_protect,
        pressure_level_kernel=level,
        pressure_bypass_reason=bypass,
    )


def validate_engineering_policy() -> None:
    """Reject accidental Phase-F policy broadening before offline replay."""

    up = [_scales(PressureLevel(value))[0] for value in range(4)]
    down = [_scales(PressureLevel(value))[1] for value in range(4)]
    if any(left < right for left, right in zip(up, up[1:])):
        raise ValueError("upgrade scales must be nonincreasing with pressure")
    if any(value < 0 or value > TIER_SCALE for value in down):
        raise ValueError("downgrade scales must stay in the Q8 safety range")
    if _scales(PressureLevel.CRITICAL) != (0, 0):
        raise ValueError("critical pressure must be exactly Native")


def _native_protect(row: Mapping[str, object]) -> bool:
    return bool(row["native_protect"]) or bool(row["special_native_protect"])


def _quadrant(native_protect: bool, effective_protect: bool) -> str:
    if not native_protect and not effective_protect:
        return "KEEP_RECLAIM"
    if not native_protect and effective_protect:
        return "PREDICTIVE_UPGRADE"
    if native_protect and effective_protect:
        return "KEEP_PROTECT"
    return "PREDICTIVE_DOWNGRADE"


def _field_int(row: Mapping[str, object], name: str) -> int:
    value = row.get(name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("Phase-F row is missing integer %s" % name)
    return value


def _field_bool(row: Mapping[str, object], name: str) -> bool:
    value = row.get(name)
    if not isinstance(value, bool):
        raise ValueError("Phase-F row is missing boolean %s" % name)
    return value


def _pressure_level(row: Mapping[str, object]) -> PressureLevel:
    try:
        return PressureLevel(_field_int(row, "pressure_level_kernel"))
    except ValueError as exc:
        raise ValueError("invalid pressure_level_kernel") from exc


def _effective_protect(row: Mapping[str, object], delta_q8: int) -> bool:
    return bool(row["special_native_protect"]) or (
        effective_tier_q8(_field_int(row, "native_tier"), delta_q8) >
        _field_int(row, "native_tier_idx") * TIER_SCALE)


def _policy_protect(row: Mapping[str, object], delta_q8: int,
                    protect_only: bool) -> bool:
    effective = _effective_protect(row, delta_q8)
    return (_native_protect(row) or effective) if protect_only else effective


def _fixed_delta(row: Mapping[str, object]) -> int:
    return _field_int(row, "fixed_delta_q8")


def _recency_delta(row: Mapping[str, object]) -> int:
    """Recreate the documented recency baseline from candidate-time state."""

    if not bool(row.get("features_valid", False)):
        return 0
    features = row.get("features")
    if not isinstance(features, Mapping):
        return 0
    age = features.get("time_since_last_real_access_ms")
    cold = row.get("score_threshold_cold", row.get("cold_threshold"))
    hot_1 = row.get("score_threshold_hot_1", row.get("hot_threshold_1"))
    hot_2 = row.get("score_threshold_hot_2", row.get("hot_threshold_2"))
    values = (age, cold, hot_1, hot_2)
    if any(isinstance(value, bool) or not isinstance(value, int)
           for value in values):
        return 0
    if age <= 100:
        score = hot_2
    elif age <= 500:
        score = hot_1
    elif age >= 10_000:
        score = cold
    else:
        score = 0
    if score <= cold:
        raw_delta = -TIER_SCALE
    elif score >= hot_2:
        raw_delta = 2 * TIER_SCALE
    elif score >= hot_1:
        raw_delta = TIER_SCALE
    else:
        raw_delta = 0
    return counterfactual_deltas(
        raw_delta, _field_int(row, "native_tier"),
        _field_int(row, "native_tier_idx"),
        bool(row["special_native_protect"]), True,
        _pressure_level(row)).fixed_delta_q8


def _random_bucket(row: Mapping[str, object]) -> int:
    identity = "\x1f".join((
        RANDOM_RATE_MATCHED_SEED,
        str(row.get("experiment_id", "")),
        str(row.get("session_id", "")),
        str(row.get("folio_cookie", "")),
        str(row.get("folio_lifetime_epoch", "")),
        str(row.get("batch_id", "")),
    ))
    return int.from_bytes(hashlib.sha256(identity.encode("utf-8")).digest()[:8],
                          "big") % 10_000


def _random_delta(row: Mapping[str, object], upgrade_rate: int,
                  downgrade_rate: int) -> int:
    if bool(row["special_native_protect"]):
        return 0
    bucket = _random_bucket(row)
    native = _native_protect(row)
    boundary = (_field_int(row, "native_tier") ==
                _field_int(row, "native_tier_idx") + 1)
    if not native and bucket < upgrade_rate:
        return TIER_SCALE
    if native and boundary and bucket < downgrade_rate:
        return -TIER_SCALE
    return 0


def _rate_permyriad(rows: Iterable[Mapping[str, object]], action: str,
                    eligible) -> int:
    numerator_pages = 0
    denominator_pages = 0
    for row in rows:
        pages = _field_int(row, "folio_nr_pages")
        if eligible(row):
            denominator_pages += pages
            if action == _quadrant(_native_protect(row),
                                   _effective_protect(row, _fixed_delta(row))):
                numerator_pages += pages
    if not denominator_pages:
        return 0
    return min(10_000, numerator_pages * 10_000 // denominator_pages)


def _future_access_rate(rows: Iterable[Mapping[str, object]]) -> Dict[str, object]:
    known_pages = 0
    positive_pages = 0
    for row in rows:
        labels = row.get("labels")
        if not isinstance(labels, Mapping):
            continue
        value = labels.get(PRIMARY_FUTURE_ACCESS_LABEL)
        if value is None:
            continue
        if not isinstance(value, bool):
            raise ValueError("future-access label must be boolean or null")
        pages = _field_int(row, "folio_nr_pages")
        known_pages += pages
        if value:
            positive_pages += pages
    rate = positive_pages / known_pages if known_pages else None
    return {
        "known_base_pages": known_pages,
        "positive_base_pages": positive_pages,
        "future_access_rate": rate,
    }


def _policy_summary(name: str, rows: List[Mapping[str, object]],
                    deltas: List[int], protect_only: bool,
                    random_rates: Optional[Mapping[str, int]] = None
                    ) -> Dict[str, object]:
    quadrant_rows: Dict[str, List[Mapping[str, object]]] = defaultdict(list)
    slices: Dict[str, Dict[str, Dict[str, int]]] = {
        "pressure_level_kernel": defaultdict(lambda: defaultdict(int)),
        "app": defaultdict(lambda: defaultdict(int)),
        "page_type": defaultdict(lambda: defaultdict(int)),
        "native_tier": defaultdict(lambda: defaultdict(int)),
    }
    critical_pages = critical_native_pages = 0
    high_critical_pages = high_critical_native_pages = 0
    for row, delta in zip(rows, deltas):
        native = _native_protect(row)
        action = _quadrant(native, _policy_protect(row, delta, protect_only))
        pages = _field_int(row, "folio_nr_pages")
        quadrant_rows[action].append(row)
        dimensions = {
            "pressure_level_kernel": str(int(_pressure_level(row))),
            "app": str(row.get("app", "UNKNOWN")),
            "page_type": str(row.get("page_type", "UNKNOWN")),
            "native_tier": str(row.get("native_tier", "UNKNOWN")),
        }
        for dimension, key in dimensions.items():
            slices[dimension][key][action] += pages
        level = _pressure_level(row)
        native_action = _quadrant(native, native)
        if level is PressureLevel.CRITICAL:
            critical_pages += pages
            if action == native_action:
                critical_native_pages += pages
        if level >= PressureLevel.HIGH:
            high_critical_pages += pages
            if action == native_action:
                high_critical_native_pages += pages

    pages_by_action = {
        action: sum(_field_int(row, "folio_nr_pages") for row in members)
        for action, members in quadrant_rows.items()
    }
    for action in ("KEEP_RECLAIM", "PREDICTIVE_UPGRADE", "KEEP_PROTECT",
                   "PREDICTIVE_DOWNGRADE"):
        pages_by_action.setdefault(action, 0)
    native_reclaim_pages = sum(_field_int(row, "folio_nr_pages") for row in rows
                               if not _native_protect(row))
    boundary_protect_pages = sum(
        _field_int(row, "folio_nr_pages") for row in rows
        if _native_protect(row) and not bool(row["special_native_protect"])
        and _field_int(row, "native_tier") ==
        _field_int(row, "native_tier_idx") + 1)
    upgrade = _future_access_rate(quadrant_rows["PREDICTIVE_UPGRADE"])
    downgrade = _future_access_rate(quadrant_rows["PREDICTIVE_DOWNGRADE"])
    result: Dict[str, object] = {
        "policy": name,
        "counterfactual_only": True,
        "label_semantics": "FUTURE_REAL_ACCESS_NOT_REFAULT",
        "action_base_pages": pages_by_action,
        "upgrade_coverage": (pages_by_action["PREDICTIVE_UPGRADE"] /
                             native_reclaim_pages if native_reclaim_pages else None),
        "downgrade_coverage": (pages_by_action["PREDICTIVE_DOWNGRADE"] /
                               boundary_protect_pages
                               if boundary_protect_pages else None),
        "upgrade_future_access": upgrade,
        "upgrade_waste_rate": (1.0 - upgrade["future_access_rate"]
                               if upgrade["future_access_rate"] is not None else None),
        "downgrade_future_access": downgrade,
        "downgrade_mistake_rate": downgrade["future_access_rate"],
        "downgrade_cold_precision": (1.0 - downgrade["future_access_rate"]
                                      if downgrade["future_access_rate"] is not None
                                      else None),
        "slices_base_pages": {
            dimension: {key: dict(values) for key, values in groups.items()}
            for dimension, groups in slices.items()
        },
        "simulated_budget": {
            "predictive_upgrade_base_pages": pages_by_action["PREDICTIVE_UPGRADE"],
            "predictive_downgrade_base_pages": pages_by_action["PREDICTIVE_DOWNGRADE"],
            "budget_enforced": False,
            "reason": "counterfactual replay has no independent budget state",
        },
        "estimated_scan_amplification": None,
        "estimated_reclaimable_candidate_increase": None,
        "high_or_critical_native_fallback_rate": (
            high_critical_native_pages / high_critical_pages
            if high_critical_pages else None),
        "critical_native_fallback_rate": (
            critical_native_pages / critical_pages if critical_pages else None),
    }
    if random_rates is not None:
        result["rate_matched"] = {
            "basis": "fixed-policy base-page rate by eligible native state",
            "seed": RANDOM_RATE_MATCHED_SEED,
            "upgrade_target_permyriad": random_rates["upgrade"],
            "downgrade_target_permyriad": random_rates["downgrade"],
            "exact_count_matched": False,
        }
    return result


def pressure_policy_ablation(rows: Iterable[Mapping[str, object]]) -> Dict[str, object]:
    """Replay Phase-F policies offline without altering Native outcomes."""

    selected = list(rows)
    if not selected:
        raise ValueError("pressure ablation requires at least one candidate")
    validate_engineering_policy()
    for row in selected:
        level = _pressure_level(row)
        if row.get("pressure_policy_provenance") not in (
                PRESSURE_POLICY_PROVENANCE, None):
            raise ValueError("unexpected pressure policy provenance")
        if row.get("pressure_policy_version") not in (
                PRESSURE_POLICY_VERSION, None):
            raise ValueError("unexpected pressure policy version")
        raw_delta = _field_int(row, "predictive_delta_tier_q8")
        expected = counterfactual_deltas(
            raw_delta, _field_int(row, "native_tier"),
            _field_int(row, "native_tier_idx"),
            bool(row["special_native_protect"]), bool(row.get("model_valid", False)),
            level)
        observed = (_fixed_delta(row),
                    _field_int(row, "binary_bypass_delta_q8"),
                    _field_int(row, "pressure_aware_delta_q8"))
        if observed != (expected.fixed_delta_q8,
                        expected.binary_bypass_delta_q8,
                        expected.pressure_aware_delta_q8):
            raise ValueError("exported counterfactual deltas disagree with policy")
        if (_field_bool(row, "fixed_effective_protect") !=
                expected.fixed_effective_protect or
                _field_bool(row, "pressure_aware_effective_protect") !=
                expected.pressure_aware_effective_protect):
            raise ValueError("exported counterfactual protections disagree with policy")
        if level is PressureLevel.CRITICAL and observed[2] != 0:
            raise ValueError("critical pressure must be exactly Native")
        if observed[2] < 0 and (bool(row["special_native_protect"]) or
                                _field_int(row, "native_tier") !=
                                _field_int(row, "native_tier_idx") + 1):
            raise ValueError("pressure-aware downgrade escaped boundary")

    upgrade_rate = _rate_permyriad(
        selected, "PREDICTIVE_UPGRADE", lambda row: not _native_protect(row))
    downgrade_rate = _rate_permyriad(
        selected, "PREDICTIVE_DOWNGRADE",
        lambda row: _native_protect(row) and not bool(row["special_native_protect"])
        and _field_int(row, "native_tier") ==
        _field_int(row, "native_tier_idx") + 1)
    policies = {
        "FIXED_PROTECT_ONLY": _policy_summary(
            "FIXED_PROTECT_ONLY", selected,
            [_fixed_delta(row) for row in selected], True),
        "FIXED_BIDIRECTIONAL": _policy_summary(
            "FIXED_BIDIRECTIONAL", selected,
            [_fixed_delta(row) for row in selected], False),
        "PRESSURE_AWARE_PROTECT_ONLY": _policy_summary(
            "PRESSURE_AWARE_PROTECT_ONLY", selected,
            [_field_int(row, "pressure_aware_delta_q8") for row in selected],
            True),
        "PRESSURE_AWARE_BIDIRECTIONAL": _policy_summary(
            "PRESSURE_AWARE_BIDIRECTIONAL", selected,
            [_field_int(row, "pressure_aware_delta_q8") for row in selected],
            False),
        "BINARY_BYPASS": _policy_summary(
            "BINARY_BYPASS", selected,
            [_field_int(row, "binary_bypass_delta_q8") for row in selected],
            False),
        "RECENCY_ONLY_SHADOW": _policy_summary(
            "RECENCY_ONLY_SHADOW", selected,
            [_recency_delta(row) for row in selected], False),
        "RANDOM_RATE_MATCHED_SHADOW": _policy_summary(
            "RANDOM_RATE_MATCHED_SHADOW", selected,
            [_random_delta(row, upgrade_rate, downgrade_rate)
             for row in selected], False,
            {"upgrade": upgrade_rate, "downgrade": downgrade_rate}),
    }
    return {
        "status": "PHASE_F_PRESSURE_COUNTERFACTUALS_REPLAYED",
        "counterfactual_only": True,
        "actual_native_behavior_modified": False,
        "label_semantics": "FUTURE_REAL_ACCESS_NOT_REFAULT",
        "policy_version": PRESSURE_POLICY_VERSION,
        "policy_provenance": PRESSURE_POLICY_PROVENANCE,
        "candidate_records": len(selected),
        "candidate_base_pages": sum(_field_int(row, "folio_nr_pages")
                                    for row in selected),
        "policies": policies,
        "safety_checks": {
            "upgrade_strength_nonincreasing_by_pressure": True,
            "downgrade_boundary_only": True,
            "critical_exactly_native": True,
            "special_native_protection_preserved": True,
            "scan_amplification_unmeasured": True,
            "reclaimable_candidate_change_unmeasured": True,
        },
    }
