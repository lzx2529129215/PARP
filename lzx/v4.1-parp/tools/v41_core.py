#!/usr/bin/env python3
"""Pure-Python reference model for the v4.1 PARP App-LSTM experiment.

The module deliberately has no torch or Linux-MM dependency.  It consumes the
same user-space prediction rows that can be emitted by the optional LSTM
adapter and computes the App-level counterfactual policy used for evaluation.
It is therefore safe to run on a normal development host.
"""

from __future__ import annotations

import csv
import json
import math
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


Q15_ONE = 32767


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def q15(value: float) -> int:
    return int(round(clamp(float(value), 0.0, 1.0) * Q15_ONE))


def parse_bool(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def parse_int(value: Any, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def parse_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class Sample:
    sample_id: str
    timestamp: str
    current_app_id: int
    current_app: str
    actual_next_app_id: int
    actual_next_app: str
    available_pages: int
    base_headroom_pages: int
    burst_pages: int


@dataclass(frozen=True)
class AppState:
    sample_id: str
    app_id: int
    app: str
    domain_id: int
    running: bool
    foreground: bool
    reclaimable_pages: int
    launch_pages: int


@dataclass(frozen=True)
class Prediction:
    sample_id: str
    horizon_ms: int
    rank: int
    app_id: int
    app: str
    raw_score: float
    use_score: float
    score_mode: str
    model_version: int
    ttl_ms: int


@dataclass(frozen=True)
class PolicyResult:
    mode: str
    predicted_launch_pages: float
    target_headroom_pages: int
    total_reclaim_pages: int
    budgets: dict[int, int]
    app_scores: dict[int, float]


def load_json(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def load_samples(path: str | Path) -> list[Sample]:
    rows: list[Sample] = []
    with Path(path).open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            rows.append(
                Sample(
                    sample_id=str(row["sample_id"]),
                    timestamp=str(row.get("timestamp", "")),
                    current_app_id=parse_int(row.get("current_app_id")),
                    current_app=str(row.get("current_app", "")),
                    actual_next_app_id=parse_int(row.get("actual_next_app_id")),
                    actual_next_app=str(row.get("actual_next_app", "")),
                    available_pages=parse_int(row.get("available_pages")),
                    base_headroom_pages=parse_int(row.get("base_headroom_pages")),
                    burst_pages=parse_int(row.get("burst_pages")),
                )
            )
    return rows


def load_app_states(path: str | Path) -> dict[str, list[AppState]]:
    result: dict[str, list[AppState]] = defaultdict(list)
    with Path(path).open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            state = AppState(
                sample_id=str(row["sample_id"]),
                app_id=parse_int(row.get("app_id")),
                app=str(row.get("app", "")),
                domain_id=parse_int(row.get("domain_id")),
                running=parse_bool(row.get("running")),
                foreground=parse_bool(row.get("foreground")),
                reclaimable_pages=max(0, parse_int(row.get("reclaimable_pages"))),
                launch_pages=max(0, parse_int(row.get("launch_pages"))),
            )
            result[state.sample_id].append(state)
    return dict(result)


def load_predictions(
    path: str | Path,
    *,
    horizon_ms: int,
    model_version: int | None = None,
) -> dict[str, list[Prediction]]:
    result: dict[str, list[Prediction]] = defaultdict(list)
    with Path(path).open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            if parse_int(row.get("horizon_ms")) != int(horizon_ms):
                continue
            version = parse_int(row.get("model_version"))
            if model_version is not None and version != model_version:
                continue
            use_score = parse_float(row.get("use_score"), parse_float(row.get("probability")))
            if use_score <= 0.0:
                continue
            result[str(row["sample_id"])].append(
                Prediction(
                    sample_id=str(row["sample_id"]),
                    horizon_ms=horizon_ms,
                    rank=parse_int(row.get("rank"), 0),
                    app_id=parse_int(row.get("app_id")),
                    app=str(row.get("app", "")),
                    raw_score=parse_float(row.get("raw_score"), use_score),
                    use_score=clamp(use_score, 0.0, 1.0),
                    score_mode=str(row.get("score_mode", "unknown")),
                    model_version=version,
                    ttl_ms=parse_int(row.get("ttl_ms")),
                )
            )
    for rows in result.values():
        rows.sort(key=lambda item: (item.rank, -item.use_score, item.app_id))
    return dict(result)


def largest_remainder_allocate(total: int, weights: dict[int, float]) -> dict[int, int]:
    """Allocate integer pages deterministically while preserving the total."""

    if total <= 0 or not weights:
        return {key: 0 for key in weights}
    denominator = sum(max(0.0, value) for value in weights.values())
    if denominator <= 0.0:
        return {key: 0 for key in weights}
    raw = {key: total * max(0.0, value) / denominator for key, value in weights.items()}
    result = {key: int(math.floor(value)) for key, value in raw.items()}
    remaining = total - sum(result.values())
    order = sorted(raw, key=lambda key: (-(raw[key] - result[key]), key))
    for key in order[:remaining]:
        result[key] += 1
    return result


def prediction_score_map(
    predictions: Iterable[Prediction],
    *,
    current_app_id: int,
) -> dict[int, float]:
    """Return the LSTM prior for candidate Apps, excluding current foreground.

    Softmax scores retain their absolute mass.  Sigmoid scores from the current
    v2 model are explicitly treated as uncalibrated and normalized over the
    supplied candidate rows before they affect a memory estimate.
    """

    rows = [item for item in predictions if item.app_id != current_app_id and item.use_score > 0.0]
    if not rows:
        return {}
    if any(item.score_mode == "sigmoid" for item in rows):
        denominator = sum(item.use_score for item in rows)
        if denominator <= 0.0:
            return {}
        return {item.app_id: item.use_score / denominator for item in rows}
    return {item.app_id: item.use_score for item in rows}


def compute_policy(
    sample: Sample,
    states: Iterable[AppState],
    predictions: Iterable[Prediction],
    config: dict[str, Any],
    *,
    mode: str,
) -> PolicyResult:
    """Compute Native or LSTM-guided App budget without touching kernel state."""

    state_list = list(states)
    state_by_id = {item.app_id: item for item in state_list}
    if mode == "lstm":
        app_scores = prediction_score_map(predictions, current_app_id=sample.current_app_id)
    elif mode == "native":
        app_scores = {}
    else:
        raise ValueError(f"unsupported policy mode: {mode}")

    predicted_launch_pages = sum(
        app_scores.get(item.app_id, 0.0) * item.launch_pages
        for item in state_list
        if not item.running
    )
    base = sample.base_headroom_pages
    burst = sample.burst_pages
    target = int(math.ceil(base + burst + predicted_launch_pages))
    pressure = max(0, target - sample.available_pages)

    fg_weight = parse_float(config.get("foreground_protection_weight"), 4.0)
    next_weight = parse_float(config.get("next_app_protection_weight"), 3.0)
    priority_weight = parse_float(config.get("priority_protection_weight"), 0.0)
    budget_weights: dict[int, float] = {}
    for item in state_list:
        if not item.running or item.reclaimable_pages <= 0:
            continue
        protection = 0.0
        if item.foreground:
            protection += fg_weight
        if mode == "lstm":
            protection += next_weight * app_scores.get(item.app_id, 0.0)
        protection += priority_weight
        budget_weights[item.app_id] = item.reclaimable_pages / (1.0 + protection)

    total_reclaim = min(pressure, sum(item.reclaimable_pages for item in state_list if item.running))
    budgets = largest_remainder_allocate(total_reclaim, budget_weights)
    return PolicyResult(
        mode=mode,
        predicted_launch_pages=predicted_launch_pages,
        target_headroom_pages=target,
        total_reclaim_pages=total_reclaim,
        budgets=budgets,
        app_scores=app_scores,
    )


def evaluate_sample(
    sample: Sample,
    states: Iterable[AppState],
    predictions: list[Prediction],
    config: dict[str, Any],
) -> dict[str, Any]:
    native = compute_policy(sample, states, predictions, config, mode="native")
    lstm = compute_policy(sample, states, predictions, config, mode="lstm")
    ranked = [item.app_id for item in predictions if item.app_id != sample.current_app_id]
    deduped_ranked = list(dict.fromkeys(ranked))
    actual = sample.actual_next_app_id
    rank = deduped_ranked.index(actual) + 1 if actual in deduped_ranked else 0
    topk = int(config.get("metric_top_k", 5))
    actual_state = next((item for item in states if item.app_id == actual), None)
    actual_next_running = int(actual_state is not None and actual_state.running)
    actual_launch_pages = (
        actual_state.launch_pages if actual_state is not None and not actual_state.running else 0
    )
    baseline_budget = native.budgets.get(actual, 0)
    lstm_budget = lstm.budgets.get(actual, 0)
    return {
        "sample_id": sample.sample_id,
        "timestamp": sample.timestamp,
        "current_app_id": sample.current_app_id,
        "actual_next_app_id": actual,
        "actual_next_app": sample.actual_next_app,
        "prediction_coverage": int(bool(predictions)),
        "prediction_rank": rank,
        "hit_at_1": int(rank == 1),
        "hit_at_k": int(0 < rank <= topk),
        "mrr": 1.0 / rank if rank else 0.0,
        "actual_next_running": actual_next_running,
        "actual_launch_pages": actual_launch_pages,
        "native_predicted_launch_pages": native.predicted_launch_pages,
        "lstm_predicted_launch_pages": lstm.predicted_launch_pages,
        "native_headroom_pages": native.target_headroom_pages,
        "lstm_headroom_pages": lstm.target_headroom_pages,
        "native_total_reclaim_pages": native.total_reclaim_pages,
        "lstm_total_reclaim_pages": lstm.total_reclaim_pages,
        "native_actual_next_budget_pages": baseline_budget,
        "lstm_actual_next_budget_pages": lstm_budget,
        "actual_next_budget_reduction_pages": baseline_budget - lstm_budget,
        "lstm_actual_next_use_score": lstm.app_scores.get(actual, 0.0),
        "headroom_abs_error": abs(lstm.predicted_launch_pages - actual_launch_pages),
        "prediction_ranked_apps": "|".join(str(item) for item in deduped_ranked),
    }


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    count = len(rows)
    if not count:
        return {"sample_count": 0}

    def mean(key: str) -> float:
        return sum(float(row.get(key, 0.0)) for row in rows) / count

    running_next = [row for row in rows if int(row.get("actual_next_running", 0))]
    return {
        "sample_count": count,
        "prediction_coverage": mean("prediction_coverage"),
        "hit_at_1": mean("hit_at_1"),
        "hit_at_k": mean("hit_at_k"),
        "mrr": mean("mrr"),
        "mean_actual_launch_pages": mean("actual_launch_pages"),
        "mean_lstm_predicted_launch_pages": mean("lstm_predicted_launch_pages"),
        "mean_headroom_abs_error": mean("headroom_abs_error"),
        "mean_native_total_reclaim_pages": mean("native_total_reclaim_pages"),
        "mean_lstm_total_reclaim_pages": mean("lstm_total_reclaim_pages"),
        "mean_actual_next_budget_reduction_pages": mean("actual_next_budget_reduction_pages"),
        "next_app_budget_comparison_samples": len(running_next),
        "lstm_changed_reclaim_target_samples": sum(
            int(row["native_total_reclaim_pages"] != row["lstm_total_reclaim_pages"])
            for row in rows
        ),
    }
