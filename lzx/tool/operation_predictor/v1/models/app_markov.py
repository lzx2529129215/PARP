"""V1 horizon-specific Markov baseline for application prediction."""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Iterable

from src.eval.metrics import _label_set


class AppMarkovV1:
    """Application Markov baseline kept as the v1 reference model."""

    def __init__(self, order: int = 4) -> None:
        if order not in (1, 2, 3, 4):
            raise ValueError("order must be between 1 and 4")
        self.order = order
        self.transitions: dict[int, dict[tuple[int, ...], Counter[int]]] = defaultdict(lambda: defaultdict(Counter))
        self.global_counts: dict[int, Counter[int]] = defaultdict(Counter)

    @staticmethod
    def _label_keys(sample: dict, horizons: Iterable[int] | None) -> list[tuple[int, str]]:
        if horizons is not None:
            return [(int(horizon), f"label_{int(horizon)}min") for horizon in horizons]

        keys: list[tuple[int, str]] = []
        for key in sample:
            if key.startswith("label_") and key.endswith("min"):
                horizon = int(key.removeprefix("label_").removesuffix("min"))
                keys.append((horizon, key))
        return sorted(keys)

    def fit(self, samples: Iterable[dict], horizons: Iterable[int] | None = None) -> None:
        for sample in samples:
            history = [int(app_id) for app_id in sample.get("history_apps", [])]
            if not history:
                continue

            max_context_order = min(self.order, len(history))
            contexts = [tuple(history[-context_order:]) for context_order in range(1, max_context_order + 1)]
            for horizon, label_key in self._label_keys(sample, horizons):
                labels = _label_set(sample.get(label_key, []))
                if not labels:
                    continue
                for label in labels:
                    self.global_counts[horizon][label] += 1
                    for context in contexts:
                        self.transitions[horizon][context][label] += 1

    def predict_topk(self, history_apps: Iterable[int], horizon: int, k: int = 5) -> list[int]:
        history = [int(app_id) for app_id in history_apps]
        counts: Counter[int] | None = None
        for context_order in range(min(self.order, len(history)), 0, -1):
            context = tuple(history[-context_order:])
            counts = self.transitions.get(horizon, {}).get(context)
            if counts:
                break
        if not counts:
            counts = self.global_counts.get(horizon, Counter())
        return [app_id for app_id, _ in sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:k]]

    def context_count(self) -> int:
        return sum(len(contexts) for contexts in self.transitions.values())
