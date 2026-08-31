from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Iterable

from runtime_monitor.detector.state_machine import WorkloadState
from runtime_monitor.features.engine import q15


@dataclass(frozen=True)
class Prediction:
    current: WorkloadState
    next: WorkloadState
    probability_q15: int
    horizon_ms: int
    ttl_ms: int
    prediction_seq: int
    model_version: int
    method: str


class WorkloadPredictor:
    def __init__(self, horizon_ms: int = 3000, ttl_ms: int = 5000, model_version: int = 1) -> None:
        self.horizon_ms = horizon_ms
        self.ttl_ms = ttl_ms
        self.model_version = model_version
        self._seq = 0
        self._transitions: dict[str, Counter[str]] = defaultdict(Counter)

    def observe_transition(self, previous: WorkloadState, current: WorkloadState) -> None:
        self._transitions[previous.dominant][current.dominant] += 1

    def predict_rule_trend(self, current: WorkloadState) -> WorkloadState:
        next_state = current
        if current.phase_mode == "EXPANDING":
            next_state = WorkloadState(current.access_order, current.reuse_mode, current.hotspot_mode, "STABLE", "STABLE_HOT", current.confidence_q15, "expansion-trend")
        elif current.phase_mode == "EMERGENCY":
            next_state = WorkloadState(current.access_order, current.reuse_mode, current.hotspot_mode, "STABLE", "STABLE_HOT", current.confidence_q15, "emergency-trend")
        self._seq += 1
        return Prediction(current, next_state, current.confidence_q15, self.horizon_ms, self.ttl_ms, self._seq, self.model_version, "rule_trend")

    def predict_markov(self, history: Iterable[WorkloadState], current: WorkloadState) -> Prediction:
        counts = self._transitions.get(current.dominant, Counter())
        if counts:
            target, count = counts.most_common(1)[0]
            probability = count / max(1, sum(counts.values()))
        else:
            target = current.dominant
            probability = 0.5
        next_state = current if target == current.dominant else WorkloadState("UNKNOWN", "UNKNOWN", "UNKNOWN", "UNKNOWN", target, q15(0.5), "markov-dominant-only")
        self._seq += 1
        return Prediction(current, next_state, q15(probability), self.horizon_ms, self.ttl_ms, self._seq, self.model_version, "second_order_markov")
