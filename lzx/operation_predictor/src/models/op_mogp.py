"""Multi-output Gaussian-process-style scorer for operation prediction."""

from __future__ import annotations

from collections import Counter, defaultdict
from math import exp, sqrt
from typing import Iterable


class OpMogpModel:
    """A dependency-free MOGP-style experiment group.

    Each operation candidate is treated as one output. Training samples are
    summarized as prototype contexts per output, and prediction combines an RBF
    context similarity with output co-occurrence correlations.
    """

    def __init__(
        self,
        op_vocab: dict[str, dict[str, int]] | None = None,
        length_scale: float = 2.0,
        correlation_weight: float = 0.35,
    ) -> None:
        if length_scale <= 0:
            raise ValueError("length_scale must be positive")
        if correlation_weight < 0:
            raise ValueError("correlation_weight must be non-negative")
        self.op_vocab = op_vocab or {}
        self.length_scale = length_scale
        self.correlation_weight = correlation_weight
        self.label_counts: dict[str, Counter[int]] = defaultdict(Counter)
        self.context_sums: dict[str, dict[int, list[float]]] = defaultdict(dict)
        self.context_counts: dict[str, Counter[int]] = defaultdict(Counter)
        self.co_counts: dict[str, dict[int, Counter[int]]] = defaultdict(lambda: defaultdict(Counter))
        self.candidates: dict[str, list[int]] = {}

    def _pad_id(self, app: str) -> int | None:
        value = self.op_vocab.get(app, {}).get("<PAD>")
        return int(value) if value is not None else None

    def _unk_id(self, app: str) -> int | None:
        value = self.op_vocab.get(app, {}).get("<UNK>")
        return int(value) if value is not None else None

    def _special_ids(self, app: str) -> set[int]:
        return {
            op_id
            for op_id in (self._pad_id(app), self._unk_id(app))
            if op_id is not None
        }

    def _history(self, app: str, history_ops: Iterable[int]) -> list[int]:
        special_ids = self._special_ids(app)
        return [int(op_id) for op_id in history_ops if int(op_id) not in special_ids]

    def _candidate_ids(self, app: str) -> list[int]:
        vocab = self.op_vocab.get(app, {})
        special_ids = self._special_ids(app)
        ids = {int(op_id) for op_id in vocab.values() if int(op_id) not in special_ids}
        ids.update(self.label_counts.get(app, {}).keys())
        return sorted(op_id for op_id in ids if op_id not in special_ids)

    def _vector(self, app: str, history_ops: Iterable[int]) -> list[float]:
        history = self._history(app, history_ops)
        if not history:
            return [0.0, 0.0, 0.0, 0.0]

        recent = history[-4:]
        counts = Counter(recent)
        last = float(recent[-1])
        prev = float(recent[-2]) if len(recent) >= 2 else 0.0
        repeat_ratio = max(counts.values()) / len(recent)
        diversity = len(counts) / len(recent)
        return [last, prev, repeat_ratio, diversity]

    @staticmethod
    def _add_to_sum(acc: list[float], values: list[float]) -> None:
        for idx, value in enumerate(values):
            acc[idx] += value

    @staticmethod
    def _mean(values: list[float], count: int) -> list[float]:
        return [value / count for value in values]

    @staticmethod
    def _distance_sq(left: list[float], right: list[float]) -> float:
        return sum((a - b) * (a - b) for a, b in zip(left, right))

    def _kernel(self, left: list[float], right: list[float]) -> float:
        return exp(-self._distance_sq(left, right) / (2.0 * self.length_scale * self.length_scale))

    def fit(self, samples: Iterable[dict]) -> None:
        rows = list(samples)
        for sample in rows:
            app = sample["app"]
            label = int(sample["label_next_op"])
            if label in self._special_ids(app):
                continue

            vector = self._vector(app, sample.get("history_ops", []))
            self.label_counts[app][label] += 1
            self.context_counts[app][label] += 1
            if label not in self.context_sums[app]:
                self.context_sums[app][label] = [0.0 for _ in vector]
            self._add_to_sum(self.context_sums[app][label], vector)

            for op_id in set(self._history(app, sample.get("history_ops", []))):
                self.co_counts[app][op_id][label] += 1

        self.candidates = {app: self._candidate_ids(app) for app in self.label_counts}

    def _prototype_score(self, app: str, history_ops: Iterable[int], candidate: int) -> float:
        count = self.context_counts.get(app, {}).get(candidate, 0)
        if not count:
            return 0.0
        vector = self._vector(app, history_ops)
        mean = self._mean(self.context_sums[app][candidate], count)
        prior = sqrt(self.label_counts[app][candidate])
        return self._kernel(vector, mean) * prior

    def _correlation_score(self, app: str, history_ops: Iterable[int], candidate: int) -> float:
        history = self._history(app, history_ops)
        if not history:
            return 0.0
        score = 0.0
        for offset, op_id in enumerate(reversed(history[-4:]), start=1):
            score += self.co_counts[app].get(op_id, {}).get(candidate, 0) / offset
        return self.correlation_weight * score

    def _score(self, app: str, history_ops: Iterable[int], candidate: int) -> float:
        return self._prototype_score(app, history_ops, candidate) + self._correlation_score(app, history_ops, candidate)

    def predict_topk(self, app: str, history_ops: Iterable[int], k: int = 5) -> list[int]:
        special_ids = self._special_ids(app)
        candidates = self.candidates.get(app) or self._candidate_ids(app)
        ranked = [
            op_id
            for op_id, _ in sorted(
                ((op_id, self._score(app, history_ops, op_id)) for op_id in candidates if op_id not in special_ids),
                key=lambda item: (-item[1], item[0]),
            )
        ]
        return ranked[:k]
