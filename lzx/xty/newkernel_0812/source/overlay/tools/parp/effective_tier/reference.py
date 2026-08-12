#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0
"""Pure integer reference for the PARP effective-tier contract.

The functions in this module deliberately model only the one GLOBAL reuse
model and the Q8 tier gate.  They have no workload/App routing and do not
consume generation-frontier state.
"""

from __future__ import annotations

import bisect
import hashlib
import json
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Iterable, List, Mapping, Optional, Sequence, Tuple

MAX_FEATURES = 6
MAX_BINS = 10
MAX_TIER = 3
TIER_SCALE = 256

MISSING_VALUE = -(1 << 63)
S16_MIN = -(1 << 15)
S16_MAX = (1 << 15) - 1
S32_MIN = -(1 << 31)
S32_MAX = (1 << 31) - 1
S64_MIN = -(1 << 63)
S64_MAX = (1 << 63) - 1
U32_MAX = (1 << 32) - 1
U64_MAX = (1 << 64) - 1

MODEL_TYPE = "pairwise_linear_ranker"
SCORE_SEMANTICS = "higher_score_means_earlier_next_real_access"
CHECKSUM_SCOPE = (
    "sorted compact JSON of model_type, model_version, ordered "
    "feature_names, bin_boundaries by name, quantized_weights by name, "
    "and quantized_bias"
)


class ModelError(ValueError):
    """The model cannot be consumed safely by the bounded kernel scorer."""


class TierAction(str, Enum):
    """Four tier-gate quadrants plus the non-overridable native case."""

    KEEP_RECLAIM = "KEEP_RECLAIM"
    PREDICTIVE_UPGRADE = "PREDICTIVE_UPGRADE"
    KEEP_PROTECT = "KEEP_PROTECT"
    PREDICTIVE_DOWNGRADE = "PREDICTIVE_DOWNGRADE"
    SPECIAL_NATIVE_PROTECT = "SPECIAL_NATIVE_PROTECT"


@dataclass(frozen=True)
class QuantModel:
    """One fixed-shape, globally shared additive lookup-table model."""

    model_name: str
    model_version: int
    feature_schema_version: int
    feature_names: Tuple[str, ...]
    bias: int
    cold_threshold: int
    hot_threshold_1: int
    hot_threshold_2: int
    hot_threshold_3: int
    max_upgrade_tiers: int
    max_downgrade_tiers: int
    bin_edges: Tuple[Tuple[int, ...], ...]
    weights: Tuple[Tuple[int, ...], ...]

    @property
    def nr_features(self) -> int:
        return len(self.weights)


@dataclass(frozen=True)
class ScoreResult:
    valid: bool
    score: int = 0
    model_name: str = "NATIVE"
    reason: str = "native"


@dataclass(frozen=True)
class TierDecision:
    reuse_score: int
    delta_tier_q8: int
    effective_tier_q8: int
    native_tier: int
    native_tier_idx: int
    native_protect: bool
    effective_protect: bool
    special_native_protect: bool
    model_valid: bool
    action: TierAction
    reason: str
    folio_nr_pages: int

    @property
    def native_actual_protect(self) -> bool:
        """Native sort protection including its separate lazy special case."""

        return self.special_native_protect or self.native_protect

    @property
    def actual_protect(self) -> bool:
        """Bidirectional policy result with special native protection forced."""

        return self.special_native_protect or self.effective_protect


def _as_rows(raw: object, field: str) -> Tuple[Tuple[int, ...], ...]:
    try:
        return tuple(tuple(int(value) for value in row) for row in raw)  # type: ignore[union-attr]
    except (TypeError, ValueError) as exc:
        raise ModelError(f"invalid {field}") from exc


def _validate_model(model: QuantModel) -> None:
    if model.model_name != "GLOBAL_REUSE_MODEL":
        raise ModelError("only GLOBAL_REUSE_MODEL is supported")
    if model.model_version <= 0 or model.feature_schema_version <= 0:
        raise ModelError("versions must be positive")
    if not 1 <= model.nr_features <= MAX_FEATURES:
        raise ModelError("invalid feature count")
    if (len(model.bin_edges) != model.nr_features or
            len(model.feature_names) != model.nr_features):
        raise ModelError("feature metadata shape mismatch")
    if len(set(model.feature_names)) != len(model.feature_names):
        raise ModelError("duplicate feature name")
    if any("frontier" in name.lower() for name in model.feature_names):
        raise ModelError("generation-frontier features are not supported")

    for edges, weights in zip(model.bin_edges, model.weights):
        if len(weights) != len(edges) + 1:
            raise ModelError("bin edge/weight shape mismatch")
        if not 1 <= len(weights) <= MAX_BINS:
            raise ModelError("invalid bin count")
        if any(left >= right for left, right in zip(edges, edges[1:])):
            raise ModelError("bin edges must be strictly increasing")
        if any(value < S64_MIN or value > S64_MAX for value in edges):
            raise ModelError("bin edge outside s64")
        if any(value < S16_MIN or value > S16_MAX for value in weights):
            raise ModelError("weight outside s16")

    scalar_s32 = (model.bias, model.cold_threshold,
                  model.hot_threshold_1, model.hot_threshold_2,
                  model.hot_threshold_3)
    if any(value < S32_MIN or value > S32_MAX for value in scalar_s32):
        raise ModelError("model scalar outside s32")
    if not (model.cold_threshold < model.hot_threshold_1 <
            model.hot_threshold_2 < model.hot_threshold_3):
        raise ModelError("thresholds must be strictly increasing")
    if model.max_upgrade_tiers not in (1, 2, 3):
        raise ModelError("max upgrade must be one, two or three tiers")
    if model.max_downgrade_tiers != 1:
        raise ModelError("max downgrade must be exactly one tier")

    minimum_score = model.bias + sum(min(row) for row in model.weights)
    maximum_score = model.bias + sum(max(row) for row in model.weights)
    if minimum_score < S32_MIN or maximum_score > S32_MAX:
        raise ModelError("score cannot be accumulated safely in s32")


def _checked_model(raw: Mapping[str, object], feature_names: Sequence[object],
                   schema_version: int) -> QuantModel:
    try:
        model = QuantModel(
            model_name=str(raw["model_name"]),
            model_version=int(raw["model_version"]),
            feature_schema_version=int(schema_version),
            feature_names=tuple(str(value) for value in feature_names),
            bias=int(raw["bias"]),
            cold_threshold=int(raw["cold_threshold"]),
            hot_threshold_1=int(raw["hot_threshold_1"]),
            hot_threshold_2=int(raw["hot_threshold_2"]),
            hot_threshold_3=int(raw["hot_threshold_3"]),
            max_upgrade_tiers=int(raw["max_upgrade_tiers"]),
            max_downgrade_tiers=int(raw["max_downgrade_tiers"]),
            bin_edges=_as_rows(raw["bin_edges"], "bin_edges"),
            weights=_as_rows(raw["weights"], "weights"),
        )
    except (KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, ModelError):
            raise
        raise ModelError("invalid model payload") from exc
    _validate_model(model)
    return model


def load_model(path: Optional[Path] = None) -> QuantModel:
    """Load and fully validate the single global engineering model."""

    model_path = path or Path(__file__).with_name("default_model.json")
    try:
        payload = json.loads(model_path.read_text(encoding="utf-8"))
        if "models" in payload:
            raise ModelError("App model collections are not supported")
        schema_version = int(payload["feature_schema_version"])
        feature_names = payload["feature_names"]
        raw = payload["model"]
        if not isinstance(raw, Mapping):
            raise ModelError("model must be an object")
        if (payload["primary_task"] != "pairwise_next-reuse_ranking" or
                payload["model_type"] != MODEL_TYPE or
                payload["score_semantics"] != SCORE_SEMANTICS or
                payload["score_is_probability"] is not False or
                payload["runtime_pairwise_comparison"] is not False or
                payload["runtime_sorting"] is not False or
                payload["runtime_candidate_sorting"] is not False or
                payload["runtime_sigmoid"] is not False):
            raise ModelError("invalid pairwise scorer semantics")
        if (payload["artifact_status"] !=
                "REFERENCE_UNTRAINED_ENGINEERING_FIXTURE" or
                payload["model_provenance"] !=
                "ENGINEERING_FIXTURE_UNTRAINED" or
                payload["training_status"] !=
                "ENGINEERING_FIXTURE_UNTRAINED" or
                payload["selected_for_live_use"] is not False):
            raise ModelError("invalid engineering-fixture provenance")
        if payload["checksum_scope"] != CHECKSUM_SCOPE:
            raise ModelError("invalid embedded scorer checksum scope")
        checksum = payload["embedded_parameter_checksum"]
        if (not isinstance(checksum, str) or len(checksum) != 64 or
                any(char not in "0123456789abcdef" for char in checksum)):
            raise ModelError("embedded scorer checksum is required")
        names = [str(name) for name in feature_names]
        subset = {
            "model_type": str(payload["model_type"]),
            "model_version": int(raw["model_version"]),
            "feature_names": names,
            "bin_boundaries": dict(zip(names, raw["bin_edges"])),
            "quantized_weights": dict(zip(names, raw["weights"])),
            "quantized_bias": int(raw["bias"]),
        }
        encoded = json.dumps(subset, sort_keys=True,
                             separators=(",", ":")).encode("utf-8")
        actual = hashlib.sha256(encoded).hexdigest()
        if checksum != actual:
            raise ModelError("embedded scorer checksum mismatch")
        return _checked_model(raw, feature_names, schema_version)
    except ModelError:
        raise
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ModelError("invalid model document") from exc


def bin_index(value: int, edges: Sequence[int]) -> int:
    """Return a bounded lookup bin; equality belongs to the lower bin."""

    if value == MISSING_VALUE:
        raise ModelError("missing feature")
    return bisect.bisect_left(edges, value)


def score_model(model: QuantModel, features: Sequence[int],
                expected_model_version: int = 1,
                expected_schema_version: int = 1) -> ScoreResult:
    """Evaluate ``bias + sum(weights[feature][bin])`` using integers only."""

    try:
        _validate_model(model)
    except ModelError:
        return ScoreResult(False, model_name=model.model_name,
                           reason="model_invalid")
    if model.model_version != expected_model_version:
        return ScoreResult(False, model_name=model.model_name,
                           reason="model_version")
    if model.feature_schema_version != expected_schema_version:
        return ScoreResult(False, model_name=model.model_name,
                           reason="feature_schema")
    if len(features) != model.nr_features:
        return ScoreResult(False, model_name=model.model_name,
                           reason="feature_count")

    score = model.bias
    try:
        for value, edges, weights in zip(features, model.bin_edges,
                                         model.weights):
            score += weights[bin_index(int(value), edges)]
    except (ModelError, TypeError, ValueError, IndexError):
        return ScoreResult(False, model_name=model.model_name,
                           reason="missing_feature")
    if not S32_MIN <= score <= S32_MAX:
        return ScoreResult(False, model_name=model.model_name,
                           reason="score_overflow")
    return ScoreResult(True, score, model.model_name, "score")


def score_to_delta_q8(score: int, model: QuantModel) -> int:
    """Map a valid score to the conservative asymmetric Q8 tier delta."""

    _validate_model(model)
    if score <= model.cold_threshold:
        return -model.max_downgrade_tiers * TIER_SCALE
    if (score >= model.hot_threshold_3 and
            model.max_upgrade_tiers == 3):
        return 3 * TIER_SCALE
    if score >= model.hot_threshold_2:
        return min(model.max_upgrade_tiers, 2) * TIER_SCALE
    if score >= model.hot_threshold_1:
        return TIER_SCALE
    return 0


def effective_tier_q8(native_tier: int, delta_tier_q8: int) -> int:
    """Add a Q8 virtual delta and clamp to Linux MGLRU's tier range."""

    if not 0 <= native_tier <= MAX_TIER:
        raise ValueError("native tier outside MGLRU tier range")
    value = int(native_tier) * TIER_SCALE + int(delta_tier_q8)
    return min(max(value, 0), MAX_TIER * TIER_SCALE)


def decide_effective_tier(
        model: Optional[QuantModel], features: Sequence[int],
        native_tier: int, tier_idx: int, *,
        special_native_protect: bool = False, folio_nr_pages: int = 1,
        expected_model_version: int = 1,
        expected_schema_version: int = 1) -> TierDecision:
    """Score and classify one folio at the native MGLRU tier gate.

    ``native_protect`` and ``effective_protect`` describe the ordinary strict
    tier comparison.  The separate special native condition always wins when
    deriving ``actual_protect`` and ``action``.
    """

    if not 0 <= native_tier <= MAX_TIER:
        raise ValueError("native tier outside MGLRU tier range")
    if not 0 <= tier_idx <= MAX_TIER:
        raise ValueError("tier_idx outside MGLRU tier range")
    if not 1 <= folio_nr_pages <= U64_MAX:
        raise ValueError("folio_nr_pages must be a positive u64 base-page count")

    if model is None:
        result = ScoreResult(False, reason="no_model")
    else:
        result = score_model(model, features, expected_model_version,
                             expected_schema_version)
    delta = score_to_delta_q8(result.score, model) if result.valid and model else 0
    effective_q8 = effective_tier_q8(native_tier, delta)
    native_protect = native_tier > tier_idx
    effective_protect = effective_q8 > tier_idx * TIER_SCALE

    if special_native_protect:
        action = TierAction.SPECIAL_NATIVE_PROTECT
    elif not native_protect and effective_protect:
        action = TierAction.PREDICTIVE_UPGRADE
    elif native_protect and not effective_protect:
        action = TierAction.PREDICTIVE_DOWNGRADE
    elif native_protect:
        action = TierAction.KEEP_PROTECT
    else:
        action = TierAction.KEEP_RECLAIM

    return TierDecision(
        reuse_score=result.score,
        delta_tier_q8=delta,
        effective_tier_q8=effective_q8,
        native_tier=native_tier,
        native_tier_idx=tier_idx,
        native_protect=native_protect,
        effective_protect=effective_protect,
        special_native_protect=bool(special_native_protect),
        model_valid=result.valid,
        action=action,
        reason=result.reason,
        folio_nr_pages=folio_nr_pages,
    )


def add_base_pages(total_pages: int, folio_nr_pages: int) -> int:
    """Charge a folio in base pages with explicit unsigned-64 overflow checks."""

    if not 0 <= total_pages <= U64_MAX:
        raise ValueError("total_pages must fit u64")
    if not 1 <= folio_nr_pages <= U64_MAX:
        raise ValueError("folio_nr_pages must be a positive u64 value")
    if total_pages > U64_MAX - folio_nr_pages:
        raise OverflowError("base-page counter overflow")
    return total_pages + folio_nr_pages


def u32_elapsed(now: int, then: int) -> int:
    """Return modulo-2^32 elapsed ticks, matching unsigned kernel subtraction."""

    return ((int(now) & U32_MAX) - (int(then) & U32_MAX)) & U32_MAX


def deterministic_vectors(model: QuantModel) -> Iterable[List[int]]:
    """Yield boundary-heavy score vectors suitable for future C parity tests."""

    base = [50, 200, 300, 1, 700, 128][:model.nr_features]
    yield list(base)
    for feature, edges in enumerate(model.bin_edges):
        for edge in edges:
            for value in (edge - 1, edge, edge + 1):
                vector = list(base)
                vector[feature] = value
                yield vector
    yield [-(1 << 62)] * model.nr_features
    yield [(1 << 62)] * model.nr_features
