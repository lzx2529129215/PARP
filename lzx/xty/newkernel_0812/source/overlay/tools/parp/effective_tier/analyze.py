#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0
"""Offline Phase-E quality, quadrant, ablation, and latency analysis."""

from __future__ import annotations

import argparse
import bisect
import math
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import DefaultDict, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

try:
    from .contracts import (
        BASE_FEATURES,
        DEFAULT_RANKING_TIE_MARGIN_NS,
        FEATURE_EDGES,
        LABEL_WINDOWS_NS,
        MODEL_ABLATIONS,
        PRIMARY_LABEL,
        QUADRANTS,
        RANKING_TIE_MARGINS_NS,
        SPLITS,
        ContractError,
        read_jsonl,
        reject_live_path,
        session_key,
        validate_candidate,
        validate_telemetry,
        write_json,
    )
    from .ranking import (
        MODEL_TYPE,
        RANK_ABLATIONS,
        SCORE_SEMANTICS,
        RankingConfig,
        RankingError,
        build_pair_dataset,
        candidate_from_labeled,
        evaluate_ranker,
        fit_pairwise_ranker,
        make_model_document,
        quantize_ranker,
        quantized_ordering_consistency,
        score_all,
        select_validation_thresholds,
    )
except ImportError:  # Direct execution from this directory.
    from contracts import (  # type: ignore
        BASE_FEATURES,
        DEFAULT_RANKING_TIE_MARGIN_NS,
        FEATURE_EDGES,
        LABEL_WINDOWS_NS,
        MODEL_ABLATIONS,
        PRIMARY_LABEL,
        QUADRANTS,
        RANKING_TIE_MARGINS_NS,
        SPLITS,
        ContractError,
        read_jsonl,
        reject_live_path,
        session_key,
        validate_candidate,
        validate_telemetry,
        write_json,
    )
    from ranking import (  # type: ignore
        MODEL_TYPE,
        RANK_ABLATIONS,
        SCORE_SEMANTICS,
        RankingConfig,
        RankingError,
        build_pair_dataset,
        candidate_from_labeled,
        evaluate_ranker,
        fit_pairwise_ranker,
        make_model_document,
        quantize_ranker,
        quantized_ordering_consistency,
        score_all,
        select_validation_thresholds,
    )


Scored = Tuple[int, bool, int, Mapping[str, object]]


def validate_labeled(row: Mapping[str, object]) -> None:
    validate_candidate(row)
    required = (
        "quadrant", "split", "app", "workload", "mode",
        "pressure_level", "labels", "label_semantics",
        "next_reuse_delay_ns", "observed_within_horizon",
        "censored_by_session_end", "horizon_ns", "tie_margin_ns",
        "ranking_target_semantics",
        "trace_lost_measured", "trace_lost",
        "tier_gate_coverage_measured", "tier_gate_coverage_complete",
    )
    missing = [name for name in required if name not in row]
    if missing:
        raise ContractError("labeled candidate missing: %s" %
                            ", ".join(sorted(missing)))
    if row["quadrant"] not in QUADRANTS:
        raise ContractError("invalid quadrant")
    native_actual = bool(row["native_protect"] or
                         row["special_native_protect"])
    effective_actual = bool(row["effective_protect"] or
                            row["special_native_protect"])
    expected_quadrant = (
        "KEEP_RECLAIM" if not native_actual and not effective_actual else
        "PREDICTIVE_UPGRADE" if not native_actual and effective_actual else
        "KEEP_PROTECT" if native_actual and effective_actual else
        "PREDICTIVE_DOWNGRADE")
    if row["quadrant"] != expected_quadrant:
        raise ContractError(
            "labeled quadrant disagrees with effective-tier decision")
    if row["split"] not in SPLITS:
        raise ContractError("invalid session split")
    if row["label_semantics"] != "FUTURE_REAL_ACCESS_NOT_REFAULT":
        raise ContractError("future access labels cannot be called refaults")
    labels = row["labels"]
    if not isinstance(labels, Mapping):
        raise ContractError("labels must be an object")
    for name, _window in LABEL_WINDOWS_NS:
        if name not in labels or labels[name] not in (True, False, None):
            raise ContractError("invalid or missing label %s" % name)
    horizon = row["horizon_ns"]
    if isinstance(horizon, bool) or not isinstance(horizon, int) or \
            horizon != 5_000_000_000:
        raise ContractError("horizon_ns must be 5000000000")
    tie_margin = row["tie_margin_ns"]
    if isinstance(tie_margin, bool) or tie_margin not in (
            0, 10_000_000, 50_000_000):
        raise ContractError("unsupported tie_margin_ns")
    observed = row["observed_within_horizon"]
    censored = row["censored_by_session_end"]
    if not isinstance(observed, bool) or not isinstance(censored, bool):
        raise ContractError("ranking observation flags must be boolean")
    delay = row["next_reuse_delay_ns"]
    if observed:
        if isinstance(delay, bool) or not isinstance(delay, int) or not (
                1 <= delay <= horizon):
            raise ContractError(
                "observed ranking target requires an in-horizon delay")
        if censored:
            raise ContractError("observed ranking target cannot be censored")
    elif delay is not None:
        raise ContractError("unobserved ranking target delay must be null")
    if row["ranking_target_semantics"] != \
            "NEXT_REAL_ACCESS_DELAY_RIGHT_CENSORED_AT_HORIZON":
        raise ContractError("invalid ranking_target_semantics")
    if not isinstance(row["trace_lost_measured"], bool):
        raise ContractError("trace_lost_measured must be boolean")
    if row["trace_lost_measured"]:
        if isinstance(row["trace_lost"], bool) or not isinstance(
                row["trace_lost"], int):
            raise ContractError("measured trace_lost must be an integer")
    elif row["trace_lost"] is not None:
        raise ContractError("unmeasured trace_lost must be null")


def _label(row: Mapping[str, object], name: str) -> Optional[bool]:
    labels = row["labels"]
    assert isinstance(labels, Mapping)
    value = labels[name]
    return value if value is None else bool(value)


def _pages(row: Mapping[str, object]) -> int:
    return int(row["folio_nr_pages"])


def _rate(rows: Iterable[Mapping[str, object]], label_name: str) -> Dict[str, object]:
    positive = 0
    negative = 0
    records = 0
    for row in rows:
        value = _label(row, label_name)
        if value is None:
            continue
        records += 1
        if value:
            positive += _pages(row)
        else:
            negative += _pages(row)
    total = positive + negative
    rate = positive / total if total else None
    low, high = _wilson(positive, total)
    return {
        "known_records": records,
        "known_base_pages": total,
        "positive_base_pages": positive,
        "negative_base_pages": negative,
        "reuse_rate": rate,
        "reuse_rate_ci95": [low, high] if low is not None else None,
    }


def _wilson(positive: int, total: int) -> Tuple[Optional[float], Optional[float]]:
    if not total:
        return None, None
    z = 1.959963984540054
    p = positive / total
    denominator = 1.0 + z * z / total
    centre = (p + z * z / (2.0 * total)) / denominator
    radius = (z * math.sqrt((p * (1.0 - p) + z * z /
                            (4.0 * total)) / total)) / denominator
    return max(0.0, centre - radius), min(1.0, centre + radius)


def quadrant_analysis(rows: Sequence[Mapping[str, object]]) -> Dict[str, object]:
    quadrants: Dict[str, object] = {}
    for name in QUADRANTS:
        selected = [row for row in rows if row["quadrant"] == name]
        quadrants[name] = {
            "records": len(selected),
            "base_pages": sum(_pages(row) for row in selected),
            "special_native_protect_records": sum(
                1 for row in selected if row["special_native_protect"]),
        }
    return {
        "candidate_scope": "ALL_NATIVE_TIER_GATE_FOLIOS",
        "total_records": len(rows),
        "total_base_pages": sum(_pages(row) for row in rows),
        "quadrants": quadrants,
    }


def action_analysis(rows: Sequence[Mapping[str, object]]) -> Tuple[Dict[str, object],
                                                                    Dict[str, object]]:
    upgrades = [row for row in rows
                if row["quadrant"] == "PREDICTIVE_UPGRADE"]
    keep_reclaim = [row for row in rows if row["quadrant"] == "KEEP_RECLAIM"]
    downgrades = [row for row in rows
                  if row["quadrant"] == "PREDICTIVE_DOWNGRADE"]
    keep_protect = [row for row in rows if row["quadrant"] == "KEEP_PROTECT"]
    upgrade_windows: Dict[str, object] = {}
    downgrade_windows: Dict[str, object] = {}
    for label_name, _window in LABEL_WINDOWS_NS:
        upgrade = _rate(upgrades, label_name)
        reclaim = _rate(keep_reclaim, label_name)
        downgrade = _rate(downgrades, label_name)
        protect = _rate(keep_protect, label_name)
        upgrade_rate = upgrade["reuse_rate"]
        downgrade_rate = downgrade["reuse_rate"]
        upgrade_windows[label_name] = {
            "predictive_upgrade": upgrade,
            "keep_reclaim": reclaim,
            "upgrade_hit_rate": upgrade_rate,
            "upgrade_waste_rate": (1.0 - upgrade_rate
                                   if upgrade_rate is not None else None),
            "direction_holds": (
                upgrade_rate > reclaim["reuse_rate"]
                if upgrade_rate is not None and
                reclaim["reuse_rate"] is not None else None),
        }
        downgrade_windows[label_name] = {
            "predictive_downgrade": downgrade,
            "keep_protect": protect,
            "downgrade_mistake_rate": downgrade_rate,
            "downgrade_cold_precision": (1.0 - downgrade_rate
                                          if downgrade_rate is not None
                                          else None),
            "direction_holds": (
                downgrade_rate < protect["reuse_rate"]
                if downgrade_rate is not None and
                protect["reuse_rate"] is not None else None),
        }
    primary_up = upgrade_windows[PRIMARY_LABEL]
    primary_down = downgrade_windows[PRIMARY_LABEL]
    return ({
        "label_semantics": "FUTURE_REAL_ACCESS_NOT_REFAULT",
        "primary_label": PRIMARY_LABEL,
        "windows": upgrade_windows,
        "primary": primary_up,
    }, {
        "label_semantics": "FUTURE_REAL_ACCESS_NOT_REFAULT",
        "primary_label": PRIMARY_LABEL,
        "special_native_protect_downgrades": sum(
            1 for row in downgrades if row["special_native_protect"]),
        "non_boundary_downgrades": sum(
            1 for row in downgrades
            if int(row["native_tier"]) != int(row["native_tier_idx"]) + 1),
        "windows": downgrade_windows,
        "primary": primary_down,
    })


def _feature_value(row: Mapping[str, object], name: str) -> Optional[int]:
    if name == "native_tier" or name == "native_tier_idx":
        return int(row[name])
    features = row["features"]
    assert isinstance(features, Mapping)
    value = features[name]
    return int(value) if value is not None else None


def _bin(value: int, edges: Sequence[int]) -> int:
    # Equality belongs to the lower bin, matching the kernel/Python oracle.
    return bisect.bisect_left(edges, value)


def _eligible(rows: Iterable[Mapping[str, object]], features: Sequence[str],
              split: Optional[str] = None) -> List[Mapping[str, object]]:
    result = []
    for row in rows:
        if split is not None and row["split"] != split:
            continue
        if row["mode"] != "SHADOW_EFFECTIVE_TIER":
            continue
        if _label(row, PRIMARY_LABEL) is None:
            continue
        if not row["tier_gate_coverage_complete"]:
            continue
        if any(_feature_value(row, name) is None for name in features):
            continue
        result.append(row)
    return result


def _logit(probability: float) -> float:
    clipped = min(max(probability, 1e-6), 1.0 - 1e-6)
    return math.log(clipped / (1.0 - clipped))


def _train_weights(rows: Sequence[Mapping[str, object]],
                   features: Sequence[str], scale: int = 32) -> Tuple[int, List[List[int]]]:
    total = sum(_pages(row) for row in rows)
    positive = sum(_pages(row) for row in rows
                   if _label(row, PRIMARY_LABEL))
    prior = (positive + 1.0) / (total + 2.0)
    bias = int(round(scale * _logit(prior)))
    all_weights: List[List[int]] = []
    for feature in features:
        edges = FEATURE_EDGES[feature]
        counts = [[0, 0] for _index in range(len(edges) + 1)]
        for row in rows:
            value = _feature_value(row, feature)
            assert value is not None
            index = _bin(value, edges)
            pages = _pages(row)
            counts[index][1] += pages
            if _label(row, PRIMARY_LABEL):
                counts[index][0] += pages
        weights: List[int] = []
        for positives, bin_total in counts:
            probability = (positives + 2.0 * prior) / (bin_total + 2.0)
            weight = int(round(scale * (_logit(probability) - _logit(prior))))
            weights.append(min(max(weight, -32768), 32767))
        all_weights.append(weights)
    return bias, all_weights


def _score(row: Mapping[str, object], features: Sequence[str], bias: int,
           weights: Sequence[Sequence[int]]) -> int:
    result = bias
    for feature, feature_weights in zip(features, weights):
        value = _feature_value(row, feature)
        if value is None:
            raise ContractError("cannot score a missing ablation feature")
        result += int(feature_weights[_bin(value, FEATURE_EDGES[feature])])
    return result


def _scored(rows: Sequence[Mapping[str, object]], features: Sequence[str],
            bias: int, weights: Sequence[Sequence[int]]) -> List[Scored]:
    return [(_score(row, features, bias, weights),
             bool(_label(row, PRIMARY_LABEL)), _pages(row), row)
            for row in rows]


def _thresholds(validation: Sequence[Scored],
                max_cold_mistake: float = 0.10,
                hot_precision_1: float = 0.60,
                hot_precision_2: float = 0.75) -> Tuple[int, int, int, Dict[str, object]]:
    scores = sorted(set(item[0] for item in validation))
    if not scores:
        raise ContractError("threshold selection requires validation scores")

    cold = scores[0] - 1
    cold_pages = 0
    for threshold in scores:
        selected = [item for item in validation if item[0] <= threshold]
        pages = sum(item[2] for item in selected)
        mistakes = sum(item[2] for item in selected if item[1])
        if pages and mistakes / pages <= max_cold_mistake and pages >= cold_pages:
            cold = threshold
            cold_pages = pages

    def hot_threshold(target: float, lower: int) -> Tuple[int, int]:
        best = scores[-1] + 1
        best_pages = 0
        for threshold in scores:
            if threshold < lower:
                continue
            selected = [item for item in validation if item[0] >= threshold]
            pages = sum(item[2] for item in selected)
            hits = sum(item[2] for item in selected if item[1])
            if pages and hits / pages >= target and pages > best_pages:
                best = threshold
                best_pages = pages
        return best, best_pages

    hot1, hot1_pages = hot_threshold(hot_precision_1, cold + 1)
    hot2, hot2_pages = hot_threshold(hot_precision_2, hot1 + 1)
    if hot1 <= cold:
        hot1 = cold + 1
    if hot2 <= hot1:
        hot2 = hot1 + 1
    return cold, hot1, hot2, {
        "cold_max_mistake_target": max_cold_mistake,
        "hot_precision_1_target": hot_precision_1,
        "hot_precision_2_target": hot_precision_2,
        "validation_cold_selected_pages": cold_pages,
        "validation_hot_1_selected_pages": hot1_pages,
        "validation_hot_2_selected_pages": hot2_pages,
    }


def _quality(values: Sequence[Scored]) -> Dict[str, object]:
    positive = sum(item[2] for item in values if item[1])
    negative = sum(item[2] for item in values if not item[1])
    buckets = _score_buckets(values)
    reuse_rates = [item["reuse_rate"] for item in buckets
                   if item["reuse_rate"] is not None]
    violations = sum(1 for left, right in zip(reuse_rates, reuse_rates[1:])
                     if right < left)
    return {
        "records": len(values),
        "base_pages": positive + negative,
        "positive_base_pages": positive,
        "positive_rate": (positive / (positive + negative)
                          if positive + negative else None),
        "roc_auc": _roc_auc(values),
        "pr_auc_average_precision": _average_precision(values),
        "ndcg": _ndcg(values),
        "score_bucket_reuse": buckets,
        "score_bucket_monotonic_non_decreasing": (
            violations == 0 if reuse_rates else None),
        "score_bucket_monotonicity_violations": violations,
    }


def _roc_auc(values: Sequence[Scored]) -> Optional[float]:
    positive = sum(item[2] for item in values if item[1])
    negative = sum(item[2] for item in values if not item[1])
    if not positive or not negative:
        return None
    groups: DefaultDict[int, List[int]] = defaultdict(lambda: [0, 0])
    for score, label, weight, _row in values:
        groups[score][0 if label else 1] += weight
    negatives_below = 0
    favorable = 0.0
    for score in sorted(groups):
        positives, negatives = groups[score]
        favorable += positives * negatives_below + 0.5 * positives * negatives
        negatives_below += negatives
    return favorable / (positive * negative)


def _average_precision(values: Sequence[Scored]) -> Optional[float]:
    total_positive = sum(item[2] for item in values if item[1])
    if not total_positive:
        return None
    groups: DefaultDict[int, List[int]] = defaultdict(lambda: [0, 0])
    for score, label, weight, _row in values:
        groups[score][0 if label else 1] += weight
    seen = 0
    hits = 0
    result = 0.0
    for score in sorted(groups, reverse=True):
        positives, negatives = groups[score]
        seen += positives + negatives
        hits += positives
        if positives:
            result += (positives / total_positive) * (hits / seen)
    return result


def _ndcg(values: Sequence[Scored]) -> Optional[float]:
    if not values or not any(item[1] for item in values):
        return None
    ranked = sorted(values, key=lambda item: item[0], reverse=True)
    dcg = sum((weight if label else 0) / math.log2(index + 2)
              for index, (_score_value, label, weight, _row) in
              enumerate(ranked))
    ideal = sorted(values, key=lambda item: (item[1], item[2]), reverse=True)
    idcg = sum((weight if label else 0) / math.log2(index + 2)
               for index, (_score_value, label, weight, _row) in
               enumerate(ideal))
    return dcg / idcg if idcg else None


def _score_buckets(values: Sequence[Scored]) -> List[Dict[str, object]]:
    if not values:
        return []
    ordered = sorted(values, key=lambda item: item[0])
    bucket_count = min(10, len(ordered))
    result = []
    for bucket in range(bucket_count):
        start = bucket * len(ordered) // bucket_count
        end = (bucket + 1) * len(ordered) // bucket_count
        selected = ordered[start:end]
        pages = sum(item[2] for item in selected)
        hits = sum(item[2] for item in selected if item[1])
        result.append({
            "bucket": bucket,
            "score_min": min(item[0] for item in selected),
            "score_max": max(item[0] for item in selected),
            "base_pages": pages,
            "reuse_rate": hits / pages if pages else None,
        })
    return result


def _simulate_quadrant(score: int, row: Mapping[str, object], cold: int,
                       hot1: int, hot2: int, max_upgrade: int) -> str:
    native = int(row["native_tier"])
    tier_idx = int(row["native_tier_idx"])
    special = bool(row["special_native_protect"])
    if score <= cold:
        delta = -1
    elif score >= hot2:
        delta = max_upgrade
    elif score >= hot1:
        delta = 1
    else:
        delta = 0
    # First-version downgrade safety: only the tier_idx+1 boundary is mutable.
    if delta < 0 and (special or native != tier_idx + 1):
        delta = 0
    effective = min(max(native + delta, 0), 3)
    native_actual = special or native > tier_idx
    effective_actual = special or effective > tier_idx
    if not native_actual and not effective_actual:
        return "KEEP_RECLAIM"
    if not native_actual and effective_actual:
        return "PREDICTIVE_UPGRADE"
    if native_actual and effective_actual:
        return "KEEP_PROTECT"
    return "PREDICTIVE_DOWNGRADE"


def _simulation_metrics(scored: Sequence[Scored], cold: int, hot1: int,
                        hot2: int, max_upgrade: int) -> Dict[str, object]:
    copied: List[Dict[str, object]] = []
    for score, _label_value, _weight, row in scored:
        value = dict(row)
        value["quadrant"] = _simulate_quadrant(
            score, row, cold, hot1, hot2, max_upgrade)
        copied.append(value)
    up, down = action_analysis(copied)
    return {
        "max_upgrade_tiers": max_upgrade,
        "quadrants": quadrant_analysis(copied),
        "upgrade": up["primary"],
        "downgrade": down["primary"],
    }


def train_probability_ablation(
        rows: Sequence[Mapping[str, object]]) -> Dict[str, object]:
    """Run the isolated legacy 1-second log-odds heuristic.

    This is retained only to make historical Phase-E output reproducible.  It
    is neither a calibrated probability model nor part of model selection.
    The ranking pipeline below is the only mainline offline model.
    """

    result: Dict[str, object] = {
        "status": "LEGACY_1S_LOG_ODDS",
        "model_family": "LEGACY_1S_LOG_ODDS",
        "primary_task": False,
        "eligible_for_model_selection": False,
        "score_is_probability": False,
        "fixed_window_ns": 1_000_000_000,
        "app_routing_enabled": False,
        "split_unit": "session",
        "training_mode": "SHADOW_EFFECTIVE_TIER",
        "primary_label": PRIMARY_LABEL,
        "ablations": {},
    }
    ablations = result["ablations"]
    assert isinstance(ablations, dict)
    for ablation_id, feature_tuple in MODEL_ABLATIONS:
        train = _eligible(rows, feature_tuple, "train")
        validation = _eligible(rows, feature_tuple, "validation")
        test = _eligible(rows, feature_tuple, "test")
        if not train:
            ablations[ablation_id] = {
                "status": "INSUFFICIENT_TRAIN_SESSIONS",
                "features": list(feature_tuple),
            }
            continue
        if not validation:
            ablations[ablation_id] = {
                "status": "INSUFFICIENT_VALIDATION_SESSIONS",
                "features": list(feature_tuple),
            }
            continue
        if not test:
            ablations[ablation_id] = {
                "status": "INSUFFICIENT_TEST_SESSIONS",
                "features": list(feature_tuple),
            }
            continue
        bias, weights = _train_weights(train, feature_tuple)
        threshold_scores = _scored(validation, feature_tuple,
                                   bias, weights)
        cold, hot1, hot2, selection = _thresholds(threshold_scores)
        split_quality: Dict[str, object] = {}
        scored_splits: Dict[str, List[Scored]] = {}
        for split_name, split_rows in (("train", train),
                                       ("validation", validation),
                                       ("test", test)):
            values = _scored(split_rows, feature_tuple, bias, weights)
            scored_splits[split_name] = values
            split_quality[split_name] = _quality(values)

        test_scored = scored_splits["test"]
        per_app: Dict[str, object] = {}
        per_type: Dict[str, object] = {}
        per_session: Dict[str, object] = {}
        for app in sorted(set(str(item[3]["app"]) for item in test_scored)):
            per_app[app] = _quality([item for item in test_scored
                                     if item[3]["app"] == app])
        for page_type in ("anon", "file"):
            selected = [item for item in test_scored
                        if item[3]["page_type"] == page_type]
            per_type[page_type] = _quality(selected)
        for item in test_scored:
            key = "%s/%s" % session_key(item[3])
            per_session.setdefault(key, [])
            per_session[key].append(item)  # type: ignore[union-attr]
        per_session = {key: _quality(value)  # type: ignore[arg-type]
                       for key, value in per_session.items()}

        evaluated = test_scored or scored_splits["validation"]
        ablations[ablation_id] = {
            "status": "LEGACY_1S_LOG_ODDS",
            "model_name": "LEGACY_1S_LOG_ODDS",
            "features": list(feature_tuple),
            "kernel_shape_compatible_v1": len(feature_tuple) <= 6,
            "model": {
                "model_name": "LEGACY_1S_LOG_ODDS",
                "model_version": 1,
                "feature_schema_version": 1,
                "bias": bias,
                "cold_threshold": cold,
                "hot_threshold_1": hot1,
                "hot_threshold_2": hot2,
                "max_upgrade_tiers": 2,
                "max_downgrade_tiers": 1,
                "bin_edges": [list(FEATURE_EDGES[name])
                              for name in feature_tuple],
                "weights": weights,
            },
            "threshold_selection": selection,
            "quality": split_quality,
            "test_stability": {
                "per_app": per_app,
                "per_page_type": per_type,
                "per_session": per_session,
            },
            "upgrade_cap_ablation": [
                _simulation_metrics(evaluated, cold, hot1, hot2, cap)
                for cap in (1, 2, 3)
            ],
        }
    return result


def probability_ablation_report(
        rows: Sequence[Mapping[str, object]]) -> Dict[str, object]:
    """Describe the deliberately non-mainline probability work."""

    return {
        "status": "NOT_IMPLEMENTED",
        "requested_windows_ns": [
            100_000_000,
            500_000_000,
            1_000_000_000,
            5_000_000_000,
        ],
        "independent_probability_models_implemented": False,
        "probability_model_mainline": False,
        "product_q15_calibrated": False,
        "eligible_for_model_selection": False,
        "legacy_available": "LEGACY_1S_LOG_ODDS",
        "legacy_1s_log_odds": train_probability_ablation(rows),
    }


def _ranking_exclusion_reason(row: Mapping[str, object]) -> Optional[str]:
    if row["mode"] != "SHADOW_EFFECTIVE_TIER":
        return "not_shadow_effective_tier"
    if row.get("features_valid") is not True:
        return "features_invalid"
    if row.get("tier_gate_coverage_complete") is not True:
        return "tier_gate_coverage_incomplete"
    features = row.get("features")
    if not isinstance(features, Mapping):
        return "features_not_an_object"
    for name in BASE_FEATURES:
        value = features.get(name)
        if isinstance(value, bool) or not isinstance(value, int):
            return "base_feature_missing_or_invalid"
    return None


def _ranking_dataset(
        rows: Sequence[Mapping[str, object]]) -> Tuple[
            List[object], Dict[str, object]]:
    candidates: List[object] = []
    exclusions: Counter[str] = Counter()
    for row in rows:
        reason = _ranking_exclusion_reason(row)
        if reason is not None:
            exclusions[reason] += 1
            continue
        try:
            candidates.append(candidate_from_labeled(row))
        except RankingError as exc:
            raise ContractError("invalid ranking candidate: %s" % exc) from exc

    split_counts = Counter(str(candidate.split) for candidate in candidates)
    app_counts = Counter(str(candidate.app) for candidate in candidates)
    type_counts = Counter(str(candidate.page_type) for candidate in candidates)
    sessions = {
        (str(candidate.experiment_id), str(candidate.session_id)):
        str(candidate.split)
        for candidate in candidates
    }
    observation = Counter(
        "observed_within_horizon"
        if candidate.observed_within_horizon else
        "session_end_censored"
        if candidate.censored_by_session_end else
        "full_horizon_censored"
        for candidate in candidates
    )
    dataset = {
        "schema_version": 1,
        "status": ("READY_FOR_PAIR_CONSTRUCTION" if candidates else
                   "INSUFFICIENT_ELIGIBLE_CANDIDATES"),
        "primary_task": "pairwise_next-reuse_ranking",
        "target_semantics":
            "NEXT_REAL_ACCESS_DELAY_RIGHT_CENSORED_AT_HORIZON",
        "ranking_horizon_ns": 5_000_000_000,
        "session_split_only": True,
        "pairs_constructed_after_session_split": True,
        "input_candidate_count": len(rows),
        "eligible_candidate_count": len(candidates),
        "excluded_candidate_count": len(rows) - len(candidates),
        "exclusions": dict(exclusions),
        "session_count": len(sessions),
        "sessions_by_split": dict(Counter(sessions.values())),
        "candidates_by_split": dict(split_counts),
        "candidates_by_app": dict(app_counts),
        "candidates_by_page_type": dict(type_counts),
        "target_observation_counts": dict(observation),
    }
    return candidates, dataset


def _pair_sampling_for_model(pair_sampling: Mapping[str, object],
                             train_pair_count: int) -> Dict[str, object]:
    """Keep held-out outcomes out of the fitted model document."""

    fields = (
        "split_unit", "ranking_horizon_ns", "tie_margin_ns",
        "supported_tie_margins_ns", "fallback_window_ns",
        "max_pairs_per_group", "default_max_pairs_per_group",
        "supported_pair_cap_ablations",
        "app_pair_cap_per_split", "seed", "all_pairs_materialized",
    )
    result = {name: pair_sampling[name] for name in fields
              if name in pair_sampling}
    tie_selection = pair_sampling.get("tie_margin_selection")
    if isinstance(tie_selection, Mapping):
        result["tie_margin_selection_status"] = tie_selection.get("status")
        result["selected_tie_margin_ns"] = tie_selection.get(
            "selected_tie_margin_ns")
        result["effective_tie_margin_ns"] = tie_selection.get(
            "effective_tie_margin_ns")
        result["tie_margin_test_set_used"] = tie_selection.get(
            "test_set_used")
    result.update({
        "training_split": "train",
        "training_pair_count": train_pair_count,
        "validation_or_test_outcomes_used_for_fitting": False,
    })
    return result


def _score_distribution(scores: Mapping[object, float]) -> Dict[str, object]:
    values = [int(value) for value in scores.values()]
    result = _distribution(values)
    result.update({
        "min": min(values) if values else None,
        "unique_scores": len(set(values)),
    })
    return result


def _quantization_metadata(model: object) -> Dict[str, object]:
    weights = getattr(model, "weights")
    bias = int(getattr(model, "bias"))
    minimum = bias + sum(min(int(value) for value in row)
                         for row in weights)
    maximum = bias + sum(max(int(value) for value in row)
                         for row in weights)
    return {
        "quantized_bias": bias,
        "weight_scale": int(getattr(model, "weight_scale")),
        "lookup_scalar_type": "s16",
        "accumulator_type": "s32_checked",
        "minimum_possible_score": minimum,
        "maximum_possible_score": maximum,
        "accumulator_range_valid": (
            -(1 << 31) <= minimum <= maximum <= (1 << 31) - 1),
    }


def _empty_ranking_model(status: str) -> Dict[str, object]:
    training_status = (
        "SCORER_TRAINED_POLICY_NOT_EXPORTABLE"
        if status.startswith("TRAINED_OFFLINE") else
        "NOT_TRAINED")
    return {
        "schema_version": 2,
        "artifact_kind": "no_exportable_ranking_model",
        "status": status,
        "reason": status,
        "primary_task": "pairwise_next-reuse_ranking",
        "model_type": MODEL_TYPE,
        "model_provenance": None,
        "training_status": training_status,
        "score_semantics": SCORE_SEMANTICS,
        "score_is_probability": False,
        "runtime_pairwise_comparison": False,
        "runtime_sorting": False,
        "runtime_candidate_sorting": False,
        "runtime_sigmoid": False,
        "selected_for_live_use": False,
        "feature_schema_version": 1,
        "model_version": None,
        "intended_feature_names": list(RANK_ABLATIONS["rank_base"]),
        "learned_parameters": None,
        "score_threshold_cold": None,
        "score_threshold_hot_1": None,
        "score_threshold_hot_2": None,
        "score_threshold_hot_3": None,
        "scorer_checksum": None,
        "checksum": None,
        "model": None,
    }


def _select_validation_tie_margin(
        candidates: Sequence[object]) -> Tuple[
            List[object], Dict[str, object], Dict[str, object]]:
    """Choose 0/10/50ms using rank_base validation pairwise accuracy."""

    train_candidates = [candidate for candidate in candidates
                        if candidate.split == "train"]
    validation_candidates = [candidate for candidate in candidates
                             if candidate.split == "validation"]
    datasets: Dict[int, Tuple[List[object], Dict[str, object]]] = {}
    sensitivity: Dict[str, object] = {}
    viable: List[Tuple[float, int]] = []

    for margin in RANKING_TIE_MARGINS_NS:
        try:
            pairs, manifest = build_pair_dataset(
                candidates, RankingConfig(
                    tie_margin_ns=margin))  # type: ignore[arg-type]
        except RankingError as exc:
            raise ContractError(
                "tie-margin pair construction failed at %dns: %s" %
                (margin, exc)) from exc
        pairs = list(pairs)
        manifest = dict(manifest)
        datasets[margin] = (pairs, manifest)
        train_pairs = [pair for pair in pairs if pair.split == "train"]
        validation_pairs = [pair for pair in pairs
                            if pair.split == "validation"]
        detail: Dict[str, object] = {
            "tie_margin_ns": margin,
            "sampled_pair_count": len(pairs),
            "train_pair_count": len(train_pairs),
            "validation_pair_count": len(validation_pairs),
            "validation_pairwise": {
                "pairs": 0,
                "correct": 0,
                "score_ties": 0,
                "pairwise_accuracy": None,
            },
            "used_test_scores_or_outcomes_for_selection": False,
            "status": "NOT_EVALUATED",
        }
        sensitivity[str(margin)] = detail
        if not train_candidates or not train_pairs:
            detail["status"] = "INSUFFICIENT_TRAIN_SUPPORT"
            continue
        if not validation_candidates or not validation_pairs:
            detail["status"] = "INSUFFICIENT_VALIDATION_SUPPORT"
            continue
        try:
            float_model = fit_pairwise_ranker(
                train_candidates, train_pairs,
                RANK_ABLATIONS["rank_base"])  # type: ignore[arg-type]
            integer_model = quantize_ranker(float_model)
            validation_scores = score_all(
                integer_model,
                validation_candidates)  # type: ignore[arg-type]
            validation_quality = evaluate_ranker(
                validation_candidates,
                validation_pairs,  # type: ignore[arg-type]
                validation_scores)
        except RankingError as exc:
            raise ContractError(
                "tie-margin validation failed at %dns: %s" %
                (margin, exc)) from exc
        pairwise = validation_quality["pairwise"]
        assert isinstance(pairwise, Mapping)
        detail["validation_pairwise"] = dict(pairwise)
        accuracy = pairwise.get("pairwise_accuracy")
        if isinstance(accuracy, (int, float)) and not isinstance(
                accuracy, bool):
            detail["status"] = "EVALUATED_ON_VALIDATION"
            viable.append((float(accuracy), margin))
        else:
            detail["status"] = "INSUFFICIENT_VALIDATION_SUPPORT"

    if viable:
        # Accuracy is primary; exact ties prefer the canonical 10ms default,
        # then the closest margin and finally the smaller margin.
        _accuracy, selected_margin = max(
            viable,
            key=lambda item: (
                item[0],
                item[1] == DEFAULT_RANKING_TIE_MARGIN_NS,
                -abs(item[1] - DEFAULT_RANKING_TIE_MARGIN_NS),
                -item[1],
            ))
        status = "SELECTED_ON_VALIDATION_PAIRWISE_ACCURACY"
    else:
        selected_margin = DEFAULT_RANKING_TIE_MARGIN_NS
        status = "INSUFFICIENT_VALIDATION_SUPPORT_NOT_SELECTED"
    validation_selected = bool(viable)
    for margin_text, detail in sensitivity.items():
        assert isinstance(detail, dict)
        is_mainline = int(margin_text) == selected_margin
        detail["selected_on_validation"] = validation_selected and is_mainline
        detail["used_for_mainline_pair_dataset"] = is_mainline

    selected_pairs, selected_manifest = datasets[selected_margin]
    selection = {
        "status": status,
        "selection_split": "validation",
        "selection_metric": "quantized_pairwise_accuracy",
        "test_set_used": False,
        "tie_break_rule": "prefer_10ms_then_closest_then_smaller",
        "selected_tie_margin_ns": (
            selected_margin if validation_selected else None),
        "effective_tie_margin_ns": selected_margin,
        "fallback_tie_margin_ns": (
            None if validation_selected else selected_margin),
        "default_tie_margin_ns": DEFAULT_RANKING_TIE_MARGIN_NS,
        "supported_tie_margins_ns": list(RANKING_TIE_MARGINS_NS),
        "sensitivity": sensitivity,
    }
    return selected_pairs, dict(selected_manifest), selection


def train_ranking_ablations(
        rows: Sequence[Mapping[str, object]]) -> Dict[str, object]:
    """Train five Bradley--Terry ablations without held-out leakage."""

    candidates, ranking_dataset = _ranking_dataset(rows)
    pairs, pair_sampling, tie_margin_selection = \
        _select_validation_tie_margin(candidates)

    pair_sampling = dict(pair_sampling)
    pair_sampling["task"] = "pairwise_next-reuse_ranking"
    pair_sampling["target_semantics"] = (
        "NEXT_REAL_ACCESS_DELAY_RIGHT_CENSORED_AT_HORIZON")
    pair_sampling["status"] = ("PAIRS_CONSTRUCTED" if pairs else
                               "INSUFFICIENT_ELIGIBLE_PAIRS")
    pair_sampling["construction_order"] = (
        "SESSION_SPLIT_THEN_IN_SPLIT_PAIR_CONSTRUCTION")
    pair_sampling["tie_margin_selection"] = tie_margin_selection
    pair_sampling["pair_cap_sensitivity"] = {
        "status": "MODEL_QUALITY_NOT_RUN",
        "supported_caps_per_group": [32, 64, 128],
        "mainline_cap_per_group": 64,
        "used_for_model_selection": False,
    }
    pairs_by_split = {
        split: [pair for pair in pairs if pair.split == split]
        for split in SPLITS
    }
    candidates_by_split = {
        split: [candidate for candidate in candidates
                if candidate.split == split]
        for split in SPLITS
    }

    quality: Dict[str, object] = {
        "status": "PAIRWISE_RANKING_ANALYZED",
        "primary_task": "pairwise_next-reuse_ranking",
        "model_type": MODEL_TYPE,
        "score_semantics": SCORE_SEMANTICS,
        "score_is_probability": False,
        "runtime_pairwise_comparison": False,
        "runtime_candidate_sorting": False,
        "runtime_sorting": False,
        "runtime_sigmoid": False,
        "probability_model_mainline": False,
        "session_split_only": True,
        "pairs_built_after_session_split": True,
        "training_split_only": "train",
        "threshold_selection_split_only": "validation",
        "test_set_used_for_training": False,
        "test_set_used_for_threshold_selection": False,
        "app_routing_enabled": False,
        "primary_ablation": "rank_base",
        "required_ablation_ids": list(RANK_ABLATIONS),
        "session_cluster_bootstrap_ci": {
            "status": "REPORTED_PER_ABLATION",
        },
        "tie_margin_selection": tie_margin_selection,
        "ablations": {},
    }
    score_distribution: Dict[str, object] = {
        "score_is_probability": False,
        "score_semantics": SCORE_SEMANTICS,
        "ablations": {},
    }
    monotonicity: Dict[str, object] = {
        "desired_direction": {
            "median_next_reuse_delay": "nonincreasing_with_score",
            "fixed_window_reuse_rate": "nondecreasing_with_score",
        },
        "ablations": {},
    }
    threshold_selection: Dict[str, object] = {
        "selection_split": "validation",
        "test_set_used": False,
        "session_cluster_bootstrap_ci": {
            "status": "REPORTED_PER_ABLATION",
        },
        "tie_margin_selection": tie_margin_selection,
        "ablations": {},
    }
    quantization: Dict[str, object] = {
        "runtime_integer_only": True,
        "runtime_sigmoid": False,
        "ablations": {},
    }

    ablations = quality["ablations"]
    distributions = score_distribution["ablations"]
    monotonic = monotonicity["ablations"]
    thresholds = threshold_selection["ablations"]
    quantized = quantization["ablations"]
    assert isinstance(ablations, dict)
    assert isinstance(distributions, dict)
    assert isinstance(monotonic, dict)
    assert isinstance(thresholds, dict)
    assert isinstance(quantized, dict)

    ranking_models: Dict[str, Mapping[str, object]] = {}
    for ablation_id, feature_names in RANK_ABLATIONS.items():
        entry: Dict[str, object] = {
            "status": "NOT_TRAINED",
            "model_type": MODEL_TYPE,
            "features": list(feature_names),
            "kernel_shape_compatible_v1": ablation_id == "rank_base",
            "kernel_deployable_v1": False,
            "offline_only": ablation_id != "rank_base",
            "app_routing_enabled": False,
            "training_pairs": len(pairs_by_split["train"]),
            "validation_pairs": len(pairs_by_split["validation"]),
            "test_pairs": len(pairs_by_split["test"]),
            "quality": {},
        }
        ablations[ablation_id] = entry
        distributions[ablation_id] = {
            "status": "NOT_EVALUATED", "splits": {}}
        monotonic[ablation_id] = {
            "status": "NOT_EVALUATED", "splits": {}}
        thresholds[ablation_id] = {
            "status": "NOT_SELECTED",
            "selected_on_split": "validation",
            "test_set_used": False,
            "session_cluster_bootstrap_ci": {
                "status": "NOT_EVALUATED",
                "gate_eligible": False,
            },
        }
        quantized[ablation_id] = {"status": "NOT_QUANTIZED"}

        train_candidates = candidates_by_split["train"]
        train_pairs = pairs_by_split["train"]
        if not train_candidates:
            entry["status"] = "INSUFFICIENT_TRAIN_CANDIDATES"
            continue
        if not train_pairs:
            entry["status"] = "INSUFFICIENT_TRAIN_PAIRS"
            continue
        try:
            float_model = fit_pairwise_ranker(
                train_candidates, train_pairs, feature_names)  # type: ignore[arg-type]
            integer_model = quantize_ranker(float_model)
        except RankingError as exc:
            raise ContractError("%s ranking training failed: %s" %
                                (ablation_id, exc)) from exc

        split_quality: Dict[str, object] = {}
        split_distributions: Dict[str, object] = {}
        split_monotonicity: Dict[str, object] = {}
        split_quantization: Dict[str, object] = {}

        # Fit and inspect train first, then select exclusively on validation.
        split_scores: Dict[str, Tuple[Mapping[object, float],
                                     Mapping[object, float]]] = {}
        for split in ("train", "validation"):
            split_candidates = candidates_by_split[split]
            split_pairs = pairs_by_split[split]
            float_scores = score_all(
                float_model, split_candidates)  # type: ignore[arg-type]
            integer_scores = score_all(
                integer_model, split_candidates)  # type: ignore[arg-type]
            split_scores[split] = (float_scores, integer_scores)
            split_quality[split] = {
                "float": evaluate_ranker(
                    split_candidates, split_pairs,  # type: ignore[arg-type]
                    float_scores),
                "quantized": evaluate_ranker(
                    split_candidates, split_pairs,  # type: ignore[arg-type]
                    integer_scores),
            }
            split_distributions[split] = _score_distribution(integer_scores)
            quantized_quality = split_quality[split]["quantized"]
            assert isinstance(quantized_quality, Mapping)
            split_monotonicity[split] = quantized_quality[
                "score_bucket_monotonicity"]
            split_quantization[split] = quantized_ordering_consistency(
                split_pairs, float_scores, integer_scores)

        selected_thresholds: Optional[Mapping[str, object]] = None
        if not candidates_by_split["validation"]:
            thresholds[ablation_id] = {
                "status": "INSUFFICIENT_VALIDATION_CANDIDATES",
                "selected_on_split": "validation",
                "test_set_used": False,
                "session_cluster_bootstrap_ci": {
                    "status": "NOT_EVALUATED",
                    "gate_eligible": False,
                },
            }
        elif not pairs_by_split["validation"]:
            thresholds[ablation_id] = {
                "status": "INSUFFICIENT_VALIDATION_PAIRS",
                "selected_on_split": "validation",
                "test_set_used": False,
                "session_cluster_bootstrap_ci": {
                    "status": "NOT_EVALUATED",
                    "gate_eligible": False,
                },
            }
        else:
            try:
                selected = select_validation_thresholds(
                    candidates_by_split["validation"],
                    split_scores["validation"][1])  # type: ignore[arg-type]
                threshold_report = dict(selected)
                if threshold_report.get(
                        "all_runtime_thresholds_validation_selected") is True:
                    threshold_report["status"] = (
                        "SELECTED_VALIDATION_ONLY")
                    selected_thresholds = threshold_report
                else:
                    threshold_report["status"] = (
                        "VALIDATION_THRESHOLDS_NOT_FULLY_SELECTED")
                thresholds[ablation_id] = threshold_report
            except RankingError as exc:
                thresholds[ablation_id] = {
                    "status": "INSUFFICIENT_VALIDATION_SCORE_SUPPORT",
                    "selected_on_split": "validation",
                    "test_set_used": False,
                    "reason": str(exc),
                    "session_cluster_bootstrap_ci": {
                        "status": "NOT_EVALUATED",
                        "gate_eligible": False,
                    },
                }

        model_document: Optional[Mapping[str, object]] = None
        if selected_thresholds is not None:
            try:
                model_document = make_model_document(
                    integer_model, selected_thresholds,
                    _pair_sampling_for_model(
                        pair_sampling, len(train_pairs)),
                    model_provenance="TRAINED_PAIRWISE_OFFLINE")
            except RankingError as exc:
                raise ContractError("%s model export failed: %s" %
                                    (ablation_id, exc)) from exc
            ranking_models[ablation_id] = model_document

        # The held-out test split is first scored only after fitting and
        # validation-only threshold selection have completed.
        test_candidates = candidates_by_split["test"]
        test_pairs = pairs_by_split["test"]
        test_float_scores = score_all(
            float_model, test_candidates)  # type: ignore[arg-type]
        test_integer_scores = score_all(
            integer_model, test_candidates)  # type: ignore[arg-type]
        split_quality["test"] = {
            "float": evaluate_ranker(
                test_candidates, test_pairs,  # type: ignore[arg-type]
                test_float_scores),
            "quantized": evaluate_ranker(
                test_candidates, test_pairs,  # type: ignore[arg-type]
                test_integer_scores),
        }
        split_distributions["test"] = _score_distribution(
            test_integer_scores)
        test_quantized_quality = split_quality["test"]["quantized"]
        assert isinstance(test_quantized_quality, Mapping)
        split_monotonicity["test"] = test_quantized_quality[
            "score_bucket_monotonicity"]
        split_quantization["test"] = quantized_ordering_consistency(
            test_pairs, test_float_scores, test_integer_scores)

        entry.update({
            "status": (
                "TRAINED_OFFLINE_THRESHOLD_NOT_SELECTED"
                if model_document is None else
                "TRAINED_OFFLINE_NO_TEST_PAIRS"
                if not test_pairs else
                "TRAINED_OFFLINE"),
            "optimizer": {
                "family": "Bradley-Terry pairwise logistic loss",
                "training_split": "train",
                "epochs": int(float_model.epochs),
                "learning_rate": float(float_model.learning_rate),
                "l2": float(float_model.l2),
            },
            "threshold_selection": thresholds[ablation_id],
            "model": model_document,
            "quality": split_quality,
        })
        distributions[ablation_id] = {
            "status": "EVALUATED", "splits": split_distributions}
        monotonic[ablation_id] = {
            "status": "EVALUATED", "splits": split_monotonicity}
        quantized[ablation_id] = dict(
            _quantization_metadata(integer_model),
            status="QUANTIZED_AND_RANGE_CHECKED",
            splits=split_quantization,
        )

    base_model = ranking_models.get("rank_base")
    if base_model is None:
        base_status = ablations["rank_base"]["status"]
        ranking_model: Mapping[str, object] = _empty_ranking_model(
            str(base_status))
    else:
        ranking_model = base_model

    base = ablations["rank_base"]
    base_status = str(base["status"])
    quality["status"] = base_status
    return {
        "ranking_dataset": ranking_dataset,
        "pair_sampling": pair_sampling,
        "ranking_model": ranking_model,
        "ranking_quality": quality,
        "score_distribution": score_distribution,
        "score_reuse_monotonicity": monotonicity,
        "threshold_selection": threshold_selection,
        "ranking_quantization": quantization,
    }


def _percentile(values: Sequence[int], percentile: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    position = percentile * (len(ordered) - 1)
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return float(ordered[lower])
    fraction = position - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


def _distribution(values: Sequence[int]) -> Dict[str, object]:
    return {
        "samples": len(values),
        "p50": _percentile(values, 0.50),
        "p95": _percentile(values, 0.95),
        "p99": _percentile(values, 0.99),
        "p99_9": _percentile(values, 0.999),
        "max": max(values) if values else None,
    }


def analyze_telemetry(records: Sequence[Mapping[str, object]]) -> Dict[str, object]:
    score_groups: DefaultDict[str, List[int]] = defaultdict(list)
    lock_groups: DefaultDict[str, List[int]] = defaultdict(list)
    lock_per_second: DefaultDict[str, Dict[int, int]] = defaultdict(dict)
    reclaim_groups: DefaultDict[str, List[int]] = defaultdict(list)
    app_groups: DefaultDict[str, List[int]] = defaultdict(list)
    app_failures: Counter[str] = Counter()
    app_sessions: DefaultDict[str, Counter[str]] = defaultdict(Counter)
    efficiency: DefaultDict[str, Counter[str]] = defaultdict(Counter)
    vm_counters: DefaultDict[str, Counter[str]] = defaultdict(Counter)
    trace_loss: List[Dict[str, object]] = []

    for record in records:
        validate_telemetry(record)
        kind = str(record["event_kind"])
        mode = str(record["mode"])
        if kind == "score_latency":
            key = "%s/%s" % (mode, record["component"])
            score_groups[key].append(int(record["duration_ns"]))
        elif kind == "lock_latency":
            base = "%s/%s" % (mode, record["scope"])
            for field in ("held_ns", "wait_ns", "irq_disabled_ns"):
                if record[field] is not None:
                    lock_groups[base + "/" + field].append(int(record[field]))
            second = int(record["timestamp_ns"]) // 1_000_000_000
            held = int(record["held_ns"])
            previous = lock_per_second[base].get(second)
            if previous is None or held > previous:
                lock_per_second[base][second] = held
        elif kind == "reclaim_latency":
            key = "%s/%s" % (mode, record["scope"])
            reclaim_groups[key].append(int(record["duration_ns"]))
        elif kind == "app_latency":
            key = "%s/%s/%s" % (mode, record["app"], record["operation"])
            app_groups[key].append(int(record["duration_ns"]))
            if not record["success"]:
                app_failures[key] += 1
        elif kind == "app_session_summary":
            key = "%s/%s" % (mode, record["app"])
            target = app_sessions[key]
            target["sessions"] += 1
            for field in ("total_duration_ns", "stalls", "timeouts",
                          "failures"):
                target[field] += int(record[field])
        elif kind == "reclaim_efficiency":
            target = efficiency[mode]
            for field in (
                    "scanned", "isolated", "reclaimed", "native_protected",
                    "predictive_upgraded", "predictive_downgraded", "pgscan",
                    "pgsteal", "no_progress_rounds", "priority_drops",
                    "younger_generation_moves"):
                target[field] += int(record[field])
        elif kind == "vm_counter_delta":
            vm_counters[mode][str(record["counter"])] += int(record["delta"])
        elif kind == "trace_loss":
            trace_loss.append(dict(record))

    efficiency_output: Dict[str, object] = {}
    for mode, counter in efficiency.items():
        values = dict(counter)
        values["reclaimed_per_scanned"] = (
            counter["reclaimed"] / counter["scanned"]
            if counter["scanned"] else None)
        values["reclaimed_per_isolated"] = (
            counter["reclaimed"] / counter["isolated"]
            if counter["isolated"] else None)
        efficiency_output[mode] = values
    return {
        "latency": {
            "score_and_effective_tier_ns": {
                key: _distribution(values) for key, values in score_groups.items()
            },
            "reclaim_ns": {
                key: _distribution(values) for key, values in reclaim_groups.items()
            },
        },
        "lock_latency": {
            "lru_lock_ns": {
                key: _distribution(values) for key, values in lock_groups.items()
            },
            "per_second_max_held_ns": {
                key: {
                    "distribution": _distribution(list(values.values())),
                    "seconds": [
                        {
                            "second_start_ns": second * 1_000_000_000,
                            "max_held_ns": maximum,
                        }
                        for second, maximum in sorted(values.items())
                    ],
                }
                for key, values in lock_per_second.items()
            },
        },
        "reclaim_efficiency": efficiency_output,
        "app_latency": {
            "operations": {
                key: dict(_distribution(values), failures=app_failures[key])
                for key, values in app_groups.items()
            },
            "sessions": {
                key: dict(values) for key, values in app_sessions.items()
            },
        },
        "vm_counter_deltas": {
            mode: dict(values) for mode, values in vm_counters.items()
        },
        "trace_loss": trace_loss,
    }


def dataset_stability(rows: Sequence[Mapping[str, object]]) -> Dict[str, object]:
    def grouped(field: str) -> Dict[str, object]:
        values: DefaultDict[str, List[Mapping[str, object]]] = defaultdict(list)
        for row in rows:
            values[str(row[field])].append(row)
        return {key: _rate(selected, PRIMARY_LABEL)
                for key, selected in sorted(values.items())}

    sessions: DefaultDict[str, List[Mapping[str, object]]] = defaultdict(list)
    for row in rows:
        sessions["%s/%s" % session_key(row)].append(row)
    positive_by_app = {
        key: value["positive_base_pages"]
        for key, value in grouped("app").items()  # type: ignore[union-attr]
    }
    total_positive = sum(int(value) for value in positive_by_app.values())
    dominance = (max(positive_by_app.values()) / total_positive
                 if total_positive and positive_by_app else None)
    return {
        "primary_label": PRIMARY_LABEL,
        "per_session": {key: _rate(value, PRIMARY_LABEL)
                        for key, value in sorted(sessions.items())},
        "per_app": grouped("app"),
        "per_page_type": grouped("page_type"),
        "per_split": grouped("split"),
        "positive_base_pages_by_app": positive_by_app,
        "largest_app_positive_share": dominance,
    }


def candidate_latency(rows: Sequence[Mapping[str, object]]) -> Dict[str, object]:
    """Summarize durations carried by every tier-gate decision record."""

    groups: DefaultDict[str, List[int]] = defaultdict(list)
    for row in rows:
        mode = str(row["mode"])
        score_ns = int(row["score_duration_ns"])
        groups[mode + "/score"].append(score_ns)
        decision_raw = row.get("decision_duration_ns")
        if isinstance(decision_raw, int) and not isinstance(decision_raw, bool):
            decision_ns = int(decision_raw)
            groups[mode + "/complete_decision"].append(decision_ns)
            groups[mode + "/non_score_decision_overhead"].append(
                max(0, decision_ns - score_ns))
    return {key: _distribution(values) for key, values in groups.items()}


HELDOUT_MIN_PAIRWISE_PAIRS = 20
HELDOUT_MIN_PAIRWISE_ACCURACY_EXCLUSIVE = 0.5
QUANTIZED_MIN_ORDERING_COMPARISONS = 20
QUANTIZED_MIN_ORDERING_CONSISTENCY = 0.95
FIXED_TIER_MIN_PAIRWISE_PAIRS = 20
FIXED_TIER_MIN_BOUNDARY_OBSERVATIONS = 20
FIXED_TIER_MIN_ACCURACY_EXCLUSIVE = 0.5
FIXED_TIER_MIN_BOUNDARY_SPEARMAN = 0.10


def _heldout_pairwise_evidence(
        quantized_test: Mapping[str, object]) -> Dict[str, object]:
    pairwise = quantized_test.get("pairwise")
    pairs = 0
    accuracy: Optional[float] = None
    if isinstance(pairwise, Mapping):
        raw_pairs = pairwise.get("pairs")
        raw_accuracy = pairwise.get("pairwise_accuracy")
        if isinstance(raw_pairs, int) and not isinstance(raw_pairs, bool):
            pairs = raw_pairs
        if isinstance(raw_accuracy, (int, float)) and not isinstance(
                raw_accuracy, bool):
            accuracy = float(raw_accuracy)
    support_pass = pairs >= HELDOUT_MIN_PAIRWISE_PAIRS
    accuracy_pass = (
        accuracy is not None and
        accuracy > HELDOUT_MIN_PAIRWISE_ACCURACY_EXCLUSIVE)
    return {
        "minimum_pairs": HELDOUT_MIN_PAIRWISE_PAIRS,
        "accuracy_floor_exclusive":
            HELDOUT_MIN_PAIRWISE_ACCURACY_EXCLUSIVE,
        "pairs": pairs,
        "pairwise_accuracy": accuracy,
        "support_pass": support_pass,
        "accuracy_pass": accuracy_pass,
        "gate_pass": support_pass and accuracy_pass,
    }


def _quantized_ordering_evidence(
        ranking: Mapping[str, object]) -> Dict[str, object]:
    compared = 0
    consistency: Optional[float] = None
    artifact = ranking.get("ranking_quantization")
    if isinstance(artifact, Mapping):
        ablations = artifact.get("ablations")
        if isinstance(ablations, Mapping):
            base = ablations.get("rank_base")
            if isinstance(base, Mapping):
                splits = base.get("splits")
                if isinstance(splits, Mapping):
                    test = splits.get("test")
                    if isinstance(test, Mapping):
                        raw_compared = test.get(
                            "compared_non_tied_float_pairs")
                        raw_consistency = test.get("ordering_consistency")
                        if (isinstance(raw_compared, int) and
                                not isinstance(raw_compared, bool)):
                            compared = raw_compared
                        if (isinstance(raw_consistency, (int, float)) and
                                not isinstance(raw_consistency, bool)):
                            consistency = float(raw_consistency)
    support_pass = compared >= QUANTIZED_MIN_ORDERING_COMPARISONS
    consistency_pass = (
        consistency is not None and
        consistency >= QUANTIZED_MIN_ORDERING_CONSISTENCY)
    return {
        "minimum_compared_non_tied_float_pairs":
            QUANTIZED_MIN_ORDERING_COMPARISONS,
        "minimum_ordering_consistency":
            QUANTIZED_MIN_ORDERING_CONSISTENCY,
        "compared_non_tied_float_pairs": compared,
        "ordering_consistency": consistency,
        "support_pass": support_pass,
        "consistency_pass": consistency_pass,
        "gate_pass": support_pass and consistency_pass,
    }


def _fixed_native_tier_evidence(
        quantized_test: Mapping[str, object]) -> Dict[str, object]:
    fixed = quantized_test.get("fixed_native_tier")
    native_tiers: Mapping[str, object] = {}
    boundary: Mapping[str, object] = {}
    if isinstance(fixed, Mapping):
        raw_tiers = fixed.get("native_tier")
        raw_boundary = fixed.get(
            "boundary_native_tier_eq_tier_idx_plus_1")
        if isinstance(raw_tiers, Mapping):
            native_tiers = raw_tiers
        if isinstance(raw_boundary, Mapping):
            boundary = raw_boundary

    supported_tiers = []
    above_chance_tiers = []
    supported_tier_evidence = []
    for tier, value in sorted(native_tiers.items()):
        if not isinstance(value, Mapping):
            continue
        pairwise = value.get("pairwise")
        if not isinstance(pairwise, Mapping):
            continue
        raw_pairs = pairwise.get("pairs")
        raw_accuracy = pairwise.get("pairwise_accuracy")
        pairs = (raw_pairs if isinstance(raw_pairs, int) and
                 not isinstance(raw_pairs, bool) else 0)
        accuracy = (float(raw_accuracy)
                    if isinstance(raw_accuracy, (int, float)) and
                    not isinstance(raw_accuracy, bool) else None)
        if pairs >= FIXED_TIER_MIN_PAIRWISE_PAIRS:
            supported_tiers.append(str(tier))
            supported_tier_evidence.append({
                "native_tier": str(tier),
                "pairs": pairs,
                "pairwise_accuracy": accuracy,
                "accuracy_floor_pass": bool(
                    accuracy is not None and
                    accuracy > FIXED_TIER_MIN_ACCURACY_EXCLUSIVE),
            })
            if (accuracy is not None and
                    accuracy > FIXED_TIER_MIN_ACCURACY_EXCLUSIVE):
                above_chance_tiers.append(str(tier))

    supported_accuracies = [
        (int(item["pairs"]), float(item["pairwise_accuracy"]))
        for item in supported_tier_evidence
        if item["pairwise_accuracy"] is not None]
    supported_pair_count = sum(pairs for pairs, _accuracy in
                               supported_accuracies)
    weighted_accuracy = (
        sum(pairs * accuracy for pairs, accuracy in supported_accuracies) /
        supported_pair_count if supported_pair_count else None)
    minimum_accuracy = (
        min(accuracy for _pairs, accuracy in supported_accuracies)
        if (supported_accuracies and
            len(supported_accuracies) == len(supported_tier_evidence))
        else None)

    raw_boundary_count = boundary.get("candidate_count")
    boundary_count = (
        raw_boundary_count
        if isinstance(raw_boundary_count, int) and
        not isinstance(raw_boundary_count, bool) else 0)
    raw_boundary_observed = boundary.get(
        "spearman_observed_candidate_count")
    boundary_observed = (
        raw_boundary_observed
        if isinstance(raw_boundary_observed, int) and
        not isinstance(raw_boundary_observed, bool) else 0)
    raw_boundary_spearman = boundary.get("spearman")
    boundary_spearman = (
        float(raw_boundary_spearman)
        if isinstance(raw_boundary_spearman, (int, float)) and
        not isinstance(raw_boundary_spearman, bool) else None)
    boundary_support_pass = (
        boundary_observed >= FIXED_TIER_MIN_BOUNDARY_OBSERVATIONS)
    boundary_discrimination_pass = (
        boundary_spearman is not None and
        boundary_spearman >= FIXED_TIER_MIN_BOUNDARY_SPEARMAN)
    within_tier_support_pass = bool(supported_tier_evidence)
    every_supported_tier_pass = bool(
        within_tier_support_pass and all(
            item["accuracy_floor_pass"] is True
            for item in supported_tier_evidence))
    aggregate_accuracy_pass = bool(
        weighted_accuracy is not None and
        weighted_accuracy > FIXED_TIER_MIN_ACCURACY_EXCLUSIVE)
    minimum_accuracy_pass = bool(
        minimum_accuracy is not None and
        minimum_accuracy > FIXED_TIER_MIN_ACCURACY_EXCLUSIVE)
    within_tier_pass = bool(
        every_supported_tier_pass and aggregate_accuracy_pass and
        minimum_accuracy_pass)
    return {
        "minimum_pairs_in_one_native_tier": FIXED_TIER_MIN_PAIRWISE_PAIRS,
        "within_tier_accuracy_floor_exclusive":
            FIXED_TIER_MIN_ACCURACY_EXCLUSIVE,
        "supported_native_tiers": supported_tiers,
        "above_chance_native_tiers": above_chance_tiers,
        "supported_native_tier_evidence": supported_tier_evidence,
        "supported_native_tier_pair_count": supported_pair_count,
        "pair_weighted_supported_tier_accuracy": weighted_accuracy,
        "minimum_supported_tier_accuracy": minimum_accuracy,
        "within_tier_support_pass": within_tier_support_pass,
        "every_supported_tier_accuracy_floor_pass":
            every_supported_tier_pass,
        "pair_weighted_accuracy_floor_pass": aggregate_accuracy_pass,
        "minimum_accuracy_floor_pass": minimum_accuracy_pass,
        "within_tier_discrimination_pass": within_tier_pass,
        "minimum_boundary_spearman_observations":
            FIXED_TIER_MIN_BOUNDARY_OBSERVATIONS,
        "boundary_candidate_count": boundary_count,
        "boundary_spearman_observed_candidate_count": boundary_observed,
        "boundary_spearman": boundary_spearman,
        "minimum_boundary_spearman":
            FIXED_TIER_MIN_BOUNDARY_SPEARMAN,
        "boundary_support_pass": boundary_support_pass,
        "boundary_discrimination_pass": boundary_discrimination_pass,
        "gate_pass": bool(
            within_tier_pass and boundary_support_pass and
            boundary_discrimination_pass),
    }


def analyze(rows: Sequence[Mapping[str, object]],
            telemetry: Sequence[Mapping[str, object]]) -> Dict[str, object]:
    for row in rows:
        validate_labeled(row)
    if not rows:
        raise ContractError("no labeled candidates were supplied")

    # Prove that every experiment/session belongs to exactly one split.
    split_by_session: Dict[Tuple[str, str], str] = {}
    for row in rows:
        key = session_key(row)
        split = str(row["split"])
        if key in split_by_session and split_by_session[key] != split:
            raise ContractError("session split leakage for %s/%s" % key)
        split_by_session[key] = split

    tier = quadrant_analysis(rows)
    upgrade, downgrade = action_analysis(rows)
    tier_by_mode: Dict[str, object] = {}
    upgrade_by_mode: Dict[str, object] = {}
    downgrade_by_mode: Dict[str, object] = {}
    for mode in sorted(set(str(row["mode"]) for row in rows)):
        selected = [row for row in rows if row["mode"] == mode]
        mode_upgrade, mode_downgrade = action_analysis(selected)
        tier_by_mode[mode] = quadrant_analysis(selected)
        upgrade_by_mode[mode] = mode_upgrade
        downgrade_by_mode[mode] = mode_downgrade
    tier["by_mode"] = tier_by_mode
    upgrade["by_mode"] = upgrade_by_mode
    downgrade["by_mode"] = downgrade_by_mode
    try:
        ranking = train_ranking_ablations(rows)
    except RankingError as exc:
        raise ContractError("ranking analysis failed: %s" % exc) from exc
    probability = probability_ablation_report(rows)
    models = ranking["ranking_quality"]
    assert isinstance(models, dict)
    models["probability_ablation"] = {
        "status": probability["status"],
        "legacy_available": probability["legacy_available"],
        "probability_model_mainline": False,
    }
    observation = analyze_telemetry(telemetry)
    observation["latency"]["tier_gate_candidate_ns"] = candidate_latency(rows)
    all_trace_measured = all(bool(row["trace_lost_measured"]) for row in rows)
    trace_lost_sessions = {
        session_key(row): row["trace_lost"] for row in rows
    }
    trace_lost = (sum(int(value or 0) for value in trace_lost_sessions.values())
                  if all_trace_measured else None)
    coverage_complete = all(bool(row["tier_gate_coverage_complete"])
                            for row in rows)
    primary_up = upgrade["primary"]
    primary_down = downgrade["primary"]
    bidirectional_quality = bool(
        primary_up["direction_holds"] is True and
        primary_down["direction_holds"] is True)
    model_ablations = models["ablations"]
    assert isinstance(model_ablations, Mapping)
    base_ablation = model_ablations["rank_base"]
    assert isinstance(base_ablation, Mapping)
    base_quality = base_ablation.get("quality")
    quantized_test: Mapping[str, object] = {}
    float_test: Mapping[str, object] = {}
    if isinstance(base_quality, Mapping):
        test_quality = base_quality.get("test")
        if isinstance(test_quality, Mapping):
            raw_quantized = test_quality.get("quantized")
            raw_float = test_quality.get("float")
            if isinstance(raw_quantized, Mapping):
                quantized_test = raw_quantized
            if isinstance(raw_float, Mapping):
                float_test = raw_float

    quantized_pairwise: Optional[float] = None
    raw_pairwise = quantized_test.get("pairwise")
    if isinstance(raw_pairwise, Mapping):
        raw_accuracy = raw_pairwise.get("pairwise_accuracy")
        if isinstance(raw_accuracy, (int, float)) and not isinstance(
                raw_accuracy, bool):
            quantized_pairwise = float(raw_accuracy)
    float_pairwise: Optional[float] = None
    raw_float_pairwise = float_test.get("pairwise")
    if isinstance(raw_float_pairwise, Mapping):
        raw_accuracy = raw_float_pairwise.get("pairwise_accuracy")
        if isinstance(raw_accuracy, (int, float)) and not isinstance(
                raw_accuracy, bool):
            float_pairwise = float(raw_accuracy)
    # This is a deployment gate: absent evidence is a failed gate, not a
    # successful or inferred measurement.  Detailed metrics retain nulls.
    monotonicity_pass = False
    raw_monotonicity = quantized_test.get("score_bucket_monotonicity")
    if isinstance(raw_monotonicity, Mapping):
        raw_pass = raw_monotonicity.get("monotonicity_pass")
        if isinstance(raw_pass, bool):
            monotonicity_pass = raw_pass

    base_thresholds = base_ablation.get("threshold_selection")
    if not isinstance(base_thresholds, Mapping):
        base_thresholds = {}
    threshold_protect_candidate = (
        base_thresholds.get("protect_only_gate_pass") is True)
    threshold_bidirectional_candidate = (
        base_thresholds.get("bidirectional_gate_pass") is True)
    bootstrap = base_thresholds.get("session_cluster_bootstrap_ci")
    bootstrap_gate_eligible = (
        isinstance(bootstrap, Mapping) and
        bootstrap.get("gate_eligible") is True)
    heldout_ranking_evaluated = quantized_pairwise is not None
    upgrade_direction_holds = primary_up["direction_holds"] is True
    downgrade_direction_holds = primary_down["direction_holds"] is True
    pairwise_evidence = _heldout_pairwise_evidence(quantized_test)
    ordering_evidence = _quantized_ordering_evidence(ranking)
    fixed_tier_evidence = _fixed_native_tier_evidence(quantized_test)
    ranking_protect_gate = bool(
        threshold_protect_candidate and monotonicity_pass and
        bootstrap_gate_eligible and pairwise_evidence["gate_pass"] is True and
        ordering_evidence["gate_pass"] is True and
        fixed_tier_evidence["gate_pass"] is True)
    ranking_bidirectional_gate = bool(
        threshold_bidirectional_candidate and ranking_protect_gate)
    gate_blockers = []
    if not threshold_protect_candidate:
        gate_blockers.append("rank_base_validation_hot_threshold_gate")
    if not threshold_bidirectional_candidate:
        gate_blockers.append("rank_base_validation_cold_threshold_gate")
    if not heldout_ranking_evaluated:
        gate_blockers.append("held_out_quantized_pairwise_evaluation")
    if not monotonicity_pass:
        gate_blockers.append("held_out_score_reuse_monotonicity")
    if not bootstrap_gate_eligible:
        gate_blockers.append("session_cluster_bootstrap_ci")
    if pairwise_evidence["gate_pass"] is not True:
        gate_blockers.append("held_out_pairwise_support_and_accuracy")
    if ordering_evidence["gate_pass"] is not True:
        gate_blockers.append("held_out_quantized_ordering_consistency")
    if fixed_tier_evidence["gate_pass"] is not True:
        gate_blockers.append("fixed_native_tier_residual_discrimination")
    gate_blockers.append("live_authorization_and_review")
    deployment_gates = {
        "current_policy_auxiliary_direction_gate": bidirectional_quality,
        "current_policy_auxiliary_direction": {
            "upgrade_1s_direction_holds": upgrade_direction_holds,
            "downgrade_1s_direction_holds": downgrade_direction_holds,
            "used_by_ranking_gates": False,
        },
        "rank_base_validation_protect_only_candidate":
            threshold_protect_candidate,
        "rank_base_validation_bidirectional_candidate":
            threshold_bidirectional_candidate,
        "heldout_quantized_ranking_evaluated":
            heldout_ranking_evaluated,
        "heldout_score_reuse_monotonicity_pass": monotonicity_pass,
        "heldout_pairwise_evidence": pairwise_evidence,
        "heldout_quantized_ordering_evidence": ordering_evidence,
        "fixed_native_tier_residual_evidence": fixed_tier_evidence,
        "session_cluster_bootstrap_gate_eligible":
            bootstrap_gate_eligible,
        "ranking_protect_only_gate": ranking_protect_gate,
        "ranking_bidirectional_gate": ranking_bidirectional_gate,
        "live_review_complete": False,
        "kernel_deployable_v1": False,
        "blockers": gate_blockers,
    }
    models["deployment_gates"] = deployment_gates

    summary = {
        "status": "PARP_EFFECTIVE_TIER_OFFLINE_ANALYSIS_COMPLETE",
        "primary_task": "pairwise_next-reuse_ranking",
        "model_type": MODEL_TYPE,
        "score_semantics": SCORE_SEMANTICS,
        "score_is_probability": False,
        "runtime_pairwise_comparison": False,
        "runtime_candidate_sorting": False,
        "runtime_sorting": False,
        "runtime_sigmoid": False,
        "probability_model_mainline": False,
        "ranking_status": base_ablation["status"],
        "pairwise_accuracy": quantized_pairwise,
        "float_pairwise_accuracy": float_pairwise,
        "quantized_pairwise_accuracy": quantized_pairwise,
        "ndcg": quantized_test.get("ndcg_at_10"),
        "c_index": quantized_test.get("c_index"),
        "score_reuse_monotonicity_pass": monotonicity_pass,
        "heldout_pairwise_evidence": pairwise_evidence,
        "heldout_quantized_ordering_evidence": ordering_evidence,
        "fixed_native_tier_residual_evidence": fixed_tier_evidence,
        "candidate_scope": "ALL_NATIVE_TIER_GATE_FOLIOS",
        "label_semantics": "FUTURE_REAL_ACCESS_NOT_REFAULT",
        "session_split_only": True,
        "sessions": len(split_by_session),
        "session_split_counts": dict(Counter(split_by_session.values())),
        "candidate_records": len(rows),
        "candidate_base_pages": sum(_pages(row) for row in rows),
        "quadrant_base_pages": {
            name: tier["quadrants"][name]["base_pages"]
            for name in QUADRANTS
        },
        "predictive_upgrade_pages": tier["quadrants"][
            "PREDICTIVE_UPGRADE"]["base_pages"],
        "predictive_downgrade_pages": tier["quadrants"][
            "PREDICTIVE_DOWNGRADE"]["base_pages"],
        "upgrade_hit_rate_1s": primary_up["upgrade_hit_rate"],
        "upgrade_waste_rate_1s": primary_up["upgrade_waste_rate"],
        "downgrade_mistake_rate_1s":
            primary_down["downgrade_mistake_rate"],
        "downgrade_cold_precision_1s":
            primary_down["downgrade_cold_precision"],
        "score_latency_ns": _distribution([
            int(row["score_duration_ns"]) for row in rows
        ]),
        "trace_lost_measured": all_trace_measured,
        "trace_lost": trace_lost,
        "tier_gate_coverage_complete": coverage_complete,
        "current_policy_auxiliary_direction_gate": bidirectional_quality,
        "current_policy_auxiliary_direction": {
            "upgrade_1s_direction_holds": upgrade_direction_holds,
            "downgrade_1s_direction_holds": downgrade_direction_holds,
            "used_by_ranking_gates": False,
        },
        "ranking_threshold_protect_only_candidate":
            threshold_protect_candidate,
        "ranking_threshold_bidirectional_candidate":
            threshold_bidirectional_candidate,
        "ranking_protect_only_gate": ranking_protect_gate,
        "ranking_bidirectional_gate": ranking_bidirectional_gate,
        "ranking_gate_blockers": gate_blockers,
        "global_model_only": True,
        "app_routing_enabled": False,
        "live_shadow_collected_by_this_tool": False,
        "protect_apply_executed_by_this_tool": False,
        "bidirectional_apply_executed_by_this_tool": False,
        "pressure_executed_by_this_tool": False,
        "next_status": "PARP_EFFECTIVE_TIER_LIVE_AUTH_REQUIRED",
    }
    return {
        "summary": summary,
        "tier_reclassification": tier,
        "upgrade_analysis": upgrade,
        "downgrade_analysis": downgrade,
        "dataset_stability": dataset_stability(rows),
        "model_quality": models,
        "ranking_dataset": ranking["ranking_dataset"],
        "pair_sampling": ranking["pair_sampling"],
        "ranking_model": ranking["ranking_model"],
        "ranking_quality": models,
        "score_distribution": ranking["score_distribution"],
        "score_reuse_monotonicity":
            ranking["score_reuse_monotonicity"],
        "threshold_selection": ranking["threshold_selection"],
        "ranking_quantization": ranking["ranking_quantization"],
        "probability_ablation": probability,
        "observability": observation,
    }


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Analyze an exported effective-tier dataset offline")
    parser.add_argument("--samples", required=True, type=Path,
                        help="collector labeled_candidates.jsonl")
    parser.add_argument("--telemetry", type=Path,
                        help="optional exported observability JSONL")
    parser.add_argument("--output-dir", required=True, type=Path)
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    try:
        paths = [args.samples, args.output_dir]
        if args.telemetry is not None:
            paths.append(args.telemetry)
        for path in paths:
            reject_live_path(path)
        rows = read_jsonl([args.samples])
        telemetry = read_jsonl([args.telemetry]) if args.telemetry else []
        result = analyze(rows, telemetry)
        args.output_dir.mkdir(parents=True, exist_ok=True)
        write_json(args.output_dir / "summary.json", result["summary"])
        write_json(args.output_dir / "tier_reclassification.json",
                   result["tier_reclassification"])
        write_json(args.output_dir / "upgrade_analysis.json",
                   result["upgrade_analysis"])
        write_json(args.output_dir / "downgrade_analysis.json",
                   result["downgrade_analysis"])
        write_json(args.output_dir / "dataset_stability.json",
                   result["dataset_stability"])
        write_json(args.output_dir / "model_quality.json",
                   result["model_quality"])
        ranking_artifacts = (
            ("ranking_dataset.json", "ranking_dataset"),
            ("pair_sampling.json", "pair_sampling"),
            ("ranking_model.json", "ranking_model"),
            ("ranking_quality.json", "ranking_quality"),
            ("score_distribution.json", "score_distribution"),
            ("score_reuse_monotonicity.json",
             "score_reuse_monotonicity"),
            ("threshold_selection.json", "threshold_selection"),
            ("ranking_quantization.json", "ranking_quantization"),
            ("probability_ablation.json", "probability_ablation"),
        )
        for filename, key in ranking_artifacts:
            write_json(args.output_dir / filename, result[key])
        # global_model.json is an exact compatibility alias for the selected
        # rank_base artifact; there is no independent probability model.
        write_json(args.output_dir / "global_model.json",
                   result["ranking_model"])
        observation = result["observability"]
        assert isinstance(observation, Mapping)
        write_json(args.output_dir / "latency.json", observation["latency"])
        write_json(args.output_dir / "lock_latency.json",
                   observation["lock_latency"])
        write_json(args.output_dir / "reclaim_efficiency.json",
                   observation["reclaim_efficiency"])
        write_json(args.output_dir / "app_latency.json",
                   observation["app_latency"])
        write_json(args.output_dir / "vm_counter_deltas.json", {
            "vm_counter_deltas": observation["vm_counter_deltas"],
            "trace_loss": observation["trace_loss"],
        })
    except (ContractError, RankingError) as exc:
        print("analyze: %s" % exc, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
