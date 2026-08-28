from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from runtime_monitor.features.engine import FeatureVector, q15


@dataclass(frozen=True)
class WorkloadState:
    access_order: str
    reuse_mode: str
    hotspot_mode: str
    phase_mode: str
    dominant: str
    confidence_q15: int
    reason: str
    state_changed: bool = False


@dataclass
class StateMachine:
    min_dwell_windows: int = 2
    cooldown_windows: int = 1
    confidence_threshold: float = 0.55
    _state: WorkloadState | None = None
    _candidate: WorkloadState | None = None
    _candidate_windows: int = 0
    _cooldown: int = 0

    def update(self, features: FeatureVector) -> WorkloadState:
        proposed = classify(features, self.confidence_threshold)
        if self._state is None:
            self._state = proposed
            return proposed
        if proposed == self._state:
            self._candidate = None
            self._candidate_windows = 0
            self._cooldown = max(0, self._cooldown - 1)
            return self._state
        if self._cooldown:
            self._cooldown -= 1
            return self._state
        if self._candidate == proposed:
            self._candidate_windows += 1
        else:
            self._candidate = proposed
            self._candidate_windows = 1
        if self._candidate_windows >= self.min_dwell_windows:
            self._state = WorkloadState(**{**proposed.__dict__, "state_changed": True})
            self._candidate = None
            self._candidate_windows = 0
            self._cooldown = self.cooldown_windows
        return self._state


def classify(features: FeatureVector, threshold: float = 0.55) -> WorkloadState:
    access = features.access
    reuse = features.reuse
    hotspot = features.hotspot
    wss = features.working_set
    pressure = features.pressure
    quality = features.data_quality
    scores: dict[str, float] = {}
    if not quality["has_region_order"]:
        access_order = "UNKNOWN"
        random_score = 0.0
        sequential_score = 0.0
    else:
        sequential_score = min(1.0, 0.5 * access["direction_consistency"] + 0.5 * access["spatial_locality"])
        random_score = min(1.0, 0.5 * min(1.0, access["address_entropy"] / 4.0) + 0.5 * (1.0 - access["spatial_locality"]))
        access_order = "SEQUENTIAL" if sequential_score >= threshold and sequential_score >= random_score else "RANDOM" if random_score >= threshold else "UNKNOWN"
    reuse_mode = "CYCLIC" if reuse["reuse_distance_stability"] >= threshold and reuse["access_periodicity"] >= threshold else "HIGH_REUSE" if reuse["reuse_rate"] >= threshold else "ONE_SHOT" if reuse["single_access_page_ratio"] >= threshold else "UNKNOWN"
    hotspot_mode = "SHIFTING_HOTSPOT" if hotspot["hotspot_shift_rate"] >= threshold else "MULTI_HOTSPOT" if hotspot["hotspot_count"] >= 3 and hotspot["hotspot_concentration"] < 0.6 else "SINGLE_HOTSPOT" if hotspot["hotspot_concentration"] >= threshold else "UNKNOWN"
    emergency = pressure["psi"] >= 0.2 or pressure["direct_reclaim"] > 0 or pressure["refault_rate"] > 0
    expanding = wss["wss_slope_pages_per_sec"] > 0 or pressure["allocation_rate_pages_per_sec"] > 0
    phase_mode = "EMERGENCY" if emergency else "EXPANDING" if expanding else "STREAMING" if access_order == "SEQUENTIAL" and reuse_mode == "ONE_SHOT" else "COLD" if sum(pressure.values()) == 0 and wss["wss_pages"] == 0 else "STABLE"
    if access_order == "UNKNOWN" and reuse_mode == "UNKNOWN" and hotspot_mode == "UNKNOWN":
        dominant = "UNKNOWN"
    elif phase_mode == "EMERGENCY":
        dominant = "MIXED"
    elif expanding:
        dominant = "BURST_EXPANSION"
    elif hotspot_mode == "MULTI_HOTSPOT" or hotspot_mode == "SHIFTING_HOTSPOT":
        dominant = "MULTI_HOTSPOT"
    elif access_order == "RANDOM":
        dominant = "RANDOM"
    elif reuse_mode == "CYCLIC":
        dominant = "CYCLIC"
    elif phase_mode == "STREAMING":
        dominant = "STREAMING"
    elif hotspot_mode == "SINGLE_HOTSPOT" and reuse_mode == "HIGH_REUSE":
        dominant = "STABLE_HOT"
    elif phase_mode == "COLD":
        dominant = "LOW_VALUE_COLD"
    else:
        dominant = "MIXED"
    known = sum(value != "UNKNOWN" for value in (access_order, reuse_mode, hotspot_mode, phase_mode))
    confidence = min(1.0, known / 4.0)
    if dominant == "UNKNOWN":
        confidence = 0.0
    return WorkloadState(access_order, reuse_mode, hotspot_mode, phase_mode, dominant, q15(confidence), f"quality_order={quality['has_region_order']};phase={phase_mode}")
