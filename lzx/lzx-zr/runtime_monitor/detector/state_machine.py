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
    # 判断当前窗口属于哪个状态
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
        directional = access["direction_consistency"]
        locality = access["spatial_locality"]
        entropy = access["address_entropy"]
        sequential_score = min(1.0, 0.6 * directional + 0.4 * locality)
        random_score = min(1.0, 0.5 * min(1.0, entropy / 4.0) + 0.5 * (1.0 - locality))
        if directional >= 0.7 and entropy <= 2.0:
            access_order = "SEQUENTIAL"
        elif entropy >= 1.5 and (1.0 - locality) >= 0.7 and directional < 0.5:
            access_order = "RANDOM"
        else:
            access_order = "SEQUENTIAL" if sequential_score >= threshold and sequential_score >= random_score else "RANDOM" if random_score >= threshold else "UNKNOWN"
    reuse_mode = "CYCLIC" if reuse["reuse_distance_stability"] >= threshold and reuse["access_periodicity"] >= threshold else "HIGH_REUSE" if reuse["reuse_rate"] >= threshold else "ONE_SHOT" if reuse["single_access_page_ratio"] >= threshold else "UNKNOWN"
    hotspot_mode = "SHIFTING_HOTSPOT" if hotspot["hotspot_shift_rate"] >= 0.8 else "MULTI_HOTSPOT" if hotspot["hotspot_count"] >= 3 and hotspot["hotspot_jaccard"] >= 0.55 else "SINGLE_HOTSPOT" if hotspot["hotspot_jaccard"] >= 0.75 or hotspot["hotspot_concentration"] >= 0.6 else "UNKNOWN"
    emergency = pressure["psi"] >= 0.2 or pressure["direct_reclaim"] > 0 or pressure["refault_rate"] > 0
    expanding = wss["wss_slope_pages_per_sec"] > 0 or pressure["allocation_rate_pages_per_sec"] > 0
    pressure_load = sum(value for key, value in pressure.items() if key != "foreground")
    phase_mode = "EMERGENCY" if emergency else "EXPANDING" if expanding else "STREAMING" if access_order == "SEQUENTIAL" and reuse_mode == "ONE_SHOT" else "COLD" if pressure_load == 0 and wss["wss_pages"] == 0 else "STABLE"
    no_order_evidence = not quality["has_region_order"] and wss["wss_pages"] == 0
    if no_order_evidence and sum(pressure.values()) == 0:
        dominant = "UNKNOWN"
    elif phase_mode == "EMERGENCY":
        dominant = "MIXED"
    elif phase_mode == "COLD":
        dominant = "LOW_VALUE_COLD"
    elif hotspot["hotspot_jaccard"] >= 0.75 and hotspot["hotspot_shift_rate"] < 0.35 and reuse_mode in {"HIGH_REUSE", "CYCLIC"} and wss["wss_pages"] > 0:
        dominant = "STABLE_HOT"
    elif phase_mode == "STREAMING":
        dominant = "STREAMING"
    elif expanding:
        dominant = "BURST_EXPANSION"
    elif hotspot_mode in {"MULTI_HOTSPOT", "SHIFTING_HOTSPOT"}:
        dominant = "MULTI_HOTSPOT"
    elif access_order == "RANDOM" and reuse_mode == "ONE_SHOT" and hotspot_mode == "UNKNOWN" and phase_mode == "STABLE":
        dominant = "UNKNOWN"
    elif access_order == "RANDOM":
        dominant = "RANDOM"
    elif reuse_mode == "CYCLIC":
        dominant = "CYCLIC"
    elif access_order == "UNKNOWN" and reuse_mode == "UNKNOWN" and hotspot_mode == "UNKNOWN":
        dominant = "UNKNOWN"
    elif hotspot_mode == "SINGLE_HOTSPOT" and reuse_mode == "HIGH_REUSE":
        dominant = "STABLE_HOT"
    else:
        dominant = "MIXED"
    known = sum(value != "UNKNOWN" for value in (access_order, reuse_mode, hotspot_mode, phase_mode))
    confidence = min(1.0, known / 4.0)
    if dominant == "UNKNOWN":
        confidence = 0.0
    return WorkloadState(access_order, reuse_mode, hotspot_mode, phase_mode, dominant, q15(confidence), f"quality_order={quality['has_region_order']};phase={phase_mode}")
