from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass
from typing import Any, Iterable

Q15_ONE = 32767

ACCESS_ORDER = ("SEQUENTIAL", "RANDOM", "UNKNOWN")
REUSE_MODE = ("ONE_SHOT", "CYCLIC", "HIGH_REUSE", "UNKNOWN")
HOTSPOT_MODE = ("SINGLE_HOTSPOT", "MULTI_HOTSPOT", "SHIFTING_HOTSPOT", "UNKNOWN")
PHASE_MODE = ("STABLE", "STREAMING", "EXPANDING", "TRANSITION", "COLD", "EMERGENCY", "UNKNOWN")
DOMINANT = ("STABLE_HOT", "STREAMING", "CYCLIC", "RANDOM", "MULTI_HOTSPOT", "BURST_EXPANSION", "LOW_VALUE_COLD", "MIXED", "UNKNOWN")


def q15(value: float) -> int:
    return max(0, min(Q15_ONE, int(round(float(value) * Q15_ONE))))


def _num(row: dict[str, Any], name: str, default: float = 0.0) -> float:
    try:
        value = float(row.get(name, default))
        return value if math.isfinite(value) else default
    except (TypeError, ValueError):
        return default


def _safe_ratio(numerator: float, denominator: float) -> float:
    return numerator / denominator if denominator > 0 else 0.0


@dataclass(frozen=True)
class Observation:
    scope_type: str
    scope_id: str
    window_start_ns: int
    window_end_ns: int
    timestamp_ns: int
    sampling_interval_ms: int
    region_ids: tuple[str, ...] = ()
    region_accesses: tuple[float, ...] = ()
    region_timestamps_ns: tuple[int, ...] = ()
    counters: dict[str, float] | None = None
    feature_version: int = 1

    @property
    def interval_seconds(self) -> float:
        if self.sampling_interval_ms > 0:
            return self.sampling_interval_ms / 1000.0
        return max(0.001, (self.window_end_ns - self.window_start_ns) / 1_000_000_000)


@dataclass(frozen=True)
class FeatureVector:
    scope: dict[str, Any]
    access: dict[str, float]
    reuse: dict[str, float]
    hotspot: dict[str, float]
    working_set: dict[str, float]
    pressure: dict[str, float]
    data_quality: dict[str, Any]
    feature_version: int


def extract_features(observation: Observation) -> FeatureVector:
    counters = observation.counters or {}
    accesses = list(observation.region_accesses)
    ids = list(observation.region_ids)
    has_order = len(accesses) >= 2 and len(ids) == len(accesses)
    total = sum(max(0.0, value) for value in accesses)
    adjacent = sum(1 for left, right in zip(ids, ids[1:]) if left and left == right)
    adjacent_ratio = _safe_ratio(adjacent, max(1, len(ids) - 1)) if has_order else 0.0
    forward = sum(1 for left, right in zip(ids, ids[1:]) if left and right and left < right)
    backward = sum(1 for left, right in zip(ids, ids[1:]) if left and right and left > right)
    direction_consistency = _safe_ratio(max(forward, backward), forward + backward) if has_order else 0.0
    entropy = 0.0
    if total > 0:
        for value in accesses:
            probability = max(0.0, value) / total
            if probability > 0:
                entropy -= probability * math.log(probability, 2)
    unique = len(set(ids)) if ids else 0
    hotspots = sum(1 for value in accesses if value > 0)
    max_access = max(accesses, default=0.0)
    reuse_count = sum(1 for value in accesses if value > 1)
    reuse_distance = [index - previous for index, previous in _reuse_pairs(ids)]
    reuse_peak = _mode(reuse_distance)
    counters = {str(key): float(value) for key, value in counters.items()}
    interval = observation.interval_seconds
    return FeatureVector(
        scope={
            "scope": observation.scope_type,
            "scope_id": observation.scope_id,
            "window_start": observation.window_start_ns,
            "window_end": observation.window_end_ns,
            "timestamp": observation.timestamp_ns,
            "sampling_interval": observation.sampling_interval_ms,
        },
        access={
            "adjacent_region_ratio": adjacent_ratio,
            "direction_consistency": direction_consistency,
            "sequential_run_length": _longest_run(ids),
            "spatial_locality": adjacent_ratio,
            "working_set_forward_motion": direction_consistency if has_order else 0.0,
            "address_entropy": entropy,
        },
        reuse={
            "reuse_distance_peak": float(reuse_peak),
            "reuse_distance_stability": _stability(reuse_distance),
            "access_periodicity": _periodicity(observation.region_timestamps_ns),
            "cycle_period": float(reuse_peak),
            "cycle_working_set": float(unique),
            "reuse_rate": _safe_ratio(reuse_count, len(accesses)),
            "single_access_page_ratio": _safe_ratio(sum(1 for value in accesses if value == 1), len(accesses)),
        },
        hotspot={
            "hotspot_count": float(hotspots),
            "hotspot_concentration": _safe_ratio(max_access, total),
            "hotspot_jaccard": _num(counters, "hotspot_jaccard"),
            "hotspot_shift_rate": _num(counters, "hotspot_shift_rate"),
        },
        working_set={
            "wss_pages": _num(counters, "wss_pages", unique),
            "wss_slope_pages_per_sec": _safe_ratio(_num(counters, "wss_delta_pages"), interval),
            "region_count": float(unique),
            "anon_file_ratio": _safe_ratio(_num(counters, "anon_pages"), _num(counters, "anon_pages") + _num(counters, "file_pages")),
        },
        pressure={
            "allocation_rate_pages_per_sec": _safe_ratio(_num(counters, "allocation_delta_pages"), interval),
            "page_fault_rate": _safe_ratio(_num(counters, "pgfault_delta"), interval),
            "refault_rate": _safe_ratio(_num(counters, "refault_delta"), interval),
            "psi": _num(counters, "psi"),
            "direct_reclaim": _num(counters, "direct_reclaim"),
            "pgscan": _num(counters, "pgscan"),
            "pgsteal": _num(counters, "pgsteal"),
            "pswpin": _num(counters, "pswpin"),
            "pswpout": _num(counters, "pswpout"),
            "foreground": _num(counters, "foreground"),
        },
        data_quality={
            "has_region_order": has_order,
            "region_resolution": "HIGH" if has_order else "LOW",
            "observation_count": len(accesses),
        },
        feature_version=observation.feature_version,
    )


def _reuse_pairs(values: list[str]) -> Iterable[tuple[int, int]]:
    previous: dict[str, int] = {}
    for index, value in enumerate(values):
        if value in previous:
            yield index, previous[value]
        previous[value] = index


def _mode(values: list[int]) -> int:
    return Counter(values).most_common(1)[0][0] if values else 0


def _stability(values: list[int]) -> float:
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    if mean <= 0:
        return 0.0
    variance = sum((value - mean) ** 2 for value in values) / len(values)
    return max(0.0, 1.0 - math.sqrt(variance) / mean)


def _periodicity(values: tuple[int, ...]) -> float:
    if len(values) < 3:
        return 0.0
    gaps = [right - left for left, right in zip(values, values[1:]) if right > left]
    return _stability(gaps)


def _longest_run(values: list[str]) -> float:
    if not values:
        return 0.0
    longest = current = 1
    for left, right in zip(values, values[1:]):
        current = current + 1 if left and right and left != right else 1
        longest = max(longest, current)
    return float(longest)
