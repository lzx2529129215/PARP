#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0
"""Pure integer reference for the PARP frontier score contract."""

from __future__ import annotations

import bisect
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

MAX_FEATURES = 8
MAX_BINS = 10
MISSING_VALUE = -(1 << 63)
S16_MIN = -(1 << 15)
S16_MAX = (1 << 15) - 1
S32_MIN = -(1 << 31)
S32_MAX = (1 << 31) - 1


class ModelError(ValueError):
    """The model cannot be consumed safely by the fixed kernel scorer."""


@dataclass(frozen=True)
class QuantModel:
    app_id: int
    model_name: str
    model_version: int
    feature_schema_version: int
    threshold: int
    bin_edges: Tuple[Tuple[int, ...], ...]
    weights: Tuple[Tuple[int, ...], ...]

    @property
    def nr_features(self) -> int:
        return len(self.weights)


@dataclass(frozen=True)
class ScoreResult:
    valid: bool
    score: int = 0
    would_promote: bool = False
    model_name: str = "NATIVE"
    reason: str = "native"


def _checked_model(raw: Mapping[str, object], schema_version: int) -> QuantModel:
    edges = tuple(tuple(int(value) for value in row)
                  for row in raw["bin_edges"])  # type: ignore[index]
    weights = tuple(tuple(int(value) for value in row)
                    for row in raw["weights"])  # type: ignore[index]
    if not 1 <= len(weights) <= MAX_FEATURES or len(edges) != len(weights):
        raise ModelError("invalid feature count")
    for edge_row, weight_row in zip(edges, weights):
        if len(weight_row) != len(edge_row) + 1:
            raise ModelError("bin edge/weight shape mismatch")
        if not 1 <= len(weight_row) <= MAX_BINS:
            raise ModelError("invalid bin count")
        if any(left >= right for left, right in zip(edge_row, edge_row[1:])):
            raise ModelError("bin edges must be strictly increasing")
        if any(value < S16_MIN or value > S16_MAX for value in weight_row):
            raise ModelError("weight outside s16")
    threshold = int(raw["threshold"])
    max_abs_score = sum(max(abs(value) for value in row) for row in weights)
    if max_abs_score > S32_MAX or not S32_MIN <= threshold <= S32_MAX:
        raise ModelError("score cannot be accumulated safely in s32")
    model_version = int(raw["model_version"])
    if model_version <= 0 or schema_version <= 0:
        raise ModelError("versions must be positive")
    return QuantModel(
        app_id=int(raw["app_id"]),
        model_name=str(raw["model_name"]),
        model_version=model_version,
        feature_schema_version=schema_version,
        threshold=threshold,
        bin_edges=edges,
        weights=weights,
    )


def load_models(path: Optional[Path] = None) -> Dict[int, QuantModel]:
    model_path = path or Path(__file__).with_name("default_models.json")
    payload = json.loads(model_path.read_text(encoding="utf-8"))
    schema = int(payload["feature_schema_version"])
    models = [_checked_model(raw, schema) for raw in payload["models"]]
    if len({model.app_id for model in models}) != len(models):
        raise ModelError("duplicate app model")
    return {model.app_id: model for model in models}


def route_model(models: Mapping[int, QuantModel], app_id: int,
                allow_generic: bool = True) -> Optional[QuantModel]:
    model = models.get(app_id)
    if model is not None:
        return model
    return models.get(0) if allow_generic else None


def bin_index(value: int, edges: Sequence[int]) -> int:
    if value == MISSING_VALUE:
        raise ModelError("missing feature")
    # An edge belongs to its lower bin. This is identical to the bounded C loop.
    return bisect.bisect_left(edges, value)


def score_model(model: QuantModel, features: Sequence[int],
                expected_model_version: int,
                expected_schema_version: int) -> ScoreResult:
    if model.model_version != expected_model_version:
        return ScoreResult(False, model_name=model.model_name,
                           reason="model_version")
    if model.feature_schema_version != expected_schema_version:
        return ScoreResult(False, model_name=model.model_name,
                           reason="feature_schema")
    if len(features) != model.nr_features:
        return ScoreResult(False, model_name=model.model_name,
                           reason="feature_count")
    score = 0
    try:
        for value, edges, weights in zip(features, model.bin_edges,
                                         model.weights):
            score += weights[bin_index(int(value), edges)]
    except ModelError:
        return ScoreResult(False, model_name=model.model_name,
                           reason="missing_feature")
    if not S32_MIN <= score <= S32_MAX:
        return ScoreResult(False, model_name=model.model_name,
                           reason="score_overflow")
    return ScoreResult(True, score, score >= model.threshold,
                       model.model_name, "score")


def score_for_app(models: Mapping[int, QuantModel], app_id: int,
                  features: Sequence[int], model_version: int = 1,
                  schema_version: int = 1,
                  allow_generic: bool = True) -> ScoreResult:
    model = route_model(models, app_id, allow_generic)
    if model is None:
        return ScoreResult(False, reason="no_model")
    return score_model(model, features, model_version, schema_version)


def promotion_budget_pages(app_remaining: int, batch_remaining: int,
                           epoch_remaining: int, frontier_headroom: int) -> int:
    values = (app_remaining, batch_remaining, epoch_remaining,
              frontier_headroom)
    if any(value < 0 for value in values):
        raise ValueError("budgets cannot be negative")
    return min(values)


def frontier_for_capacities(capacities: Sequence[int], remaining: int,
                            eta_q15: int) -> Optional[Tuple[int, int, int]]:
    """Return (frontier index, effective capacity, frontier headroom)."""
    if remaining <= 0 or not 0 < eta_q15 <= 32768:
        return None
    cumulative = 0
    for index, pages in enumerate(capacities):
        if pages < 0:
            return None
        effective = pages * eta_q15 // 32768
        previous = cumulative
        cumulative += effective
        if cumulative >= remaining:
            demand_in_frontier = remaining - previous
            return index, effective, effective - demand_in_frontier
    return None


def deterministic_vectors(models: Mapping[int, QuantModel]) -> Iterable[Tuple[int, List[int]]]:
    """Boundary-heavy vectors shared by Python and the standalone C scorer."""
    base = [50, 200, 128, 300, 1, 2, 700, 20000]
    for app_id in (0, 1, 2, 3):
        yield app_id, list(base)
        model = models[app_id]
        for feature, edges in enumerate(model.bin_edges):
            for edge in edges:
                for value in (edge - 1, edge, edge + 1):
                    vector = list(base)
                    vector[feature] = value
                    yield app_id, vector
        low = [-(1 << 62)] * model.nr_features
        high = [(1 << 62)] * model.nr_features
        yield app_id, low
        yield app_id, high
