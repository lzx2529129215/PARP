"""Neural-quantile-style scorer for in-app next-operation prediction."""

from __future__ import annotations

from collections import Counter, defaultdict
from hashlib import blake2b
from typing import Iterable


class OpQuantileModel:
    """A lightweight quantile-regression experiment group.

    This dependency-free model uses hashed sparse features as a tiny neural-like
    representation and trains one set of weights per quantile with pinball-loss
    style updates. Candidate operations are ranked by the selected quantile
    score, so it fits the same Top-K evaluation contract as the other models.
    """

    def __init__(
        self,
        op_vocab: dict[str, dict[str, int]] | None = None,
        quantiles: Iterable[float] = (0.5, 0.9),
        rank_quantile: float = 0.9,
        hidden_size: int = 64,
        epochs: int = 5,
        learning_rate: float = 0.05,
    ) -> None:
        self.quantiles = sorted(float(q) for q in quantiles)
        if not self.quantiles or any(q <= 0.0 or q >= 1.0 for q in self.quantiles):
            raise ValueError("quantiles must be between 0 and 1")
        if rank_quantile not in self.quantiles:
            raise ValueError("rank_quantile must be present in quantiles")
        if hidden_size <= 0:
            raise ValueError("hidden_size must be positive")
        if epochs <= 0:
            raise ValueError("epochs must be positive")
        if learning_rate <= 0:
            raise ValueError("learning_rate must be positive")

        self.op_vocab = op_vocab or {}
        self.rank_quantile = rank_quantile
        self.hidden_size = hidden_size
        self.epochs = epochs
        self.learning_rate = learning_rate
        self.weights: dict[str, dict[float, Counter[int]]] = defaultdict(lambda: defaultdict(Counter))
        self.bias: dict[str, Counter[float]] = defaultdict(Counter)
        self.label_counts: dict[str, Counter[int]] = defaultdict(Counter)
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

    def _bucket(self, key: str) -> int:
        digest = blake2b(key.encode("utf-8"), digest_size=8).digest()
        return int.from_bytes(digest, "little") % self.hidden_size

    def _feature_buckets(self, app: str, history_ops: Iterable[int], candidate: int) -> list[int]:
        history = self._history(app, history_ops)
        keys = [
            "bias",
            f"app={app}|cand={candidate}",
            f"hist_len={min(len(history), 8)}|cand={candidate}",
        ]
        if history:
            counts = Counter(history)
            keys.append(f"cand_count={min(counts.get(candidate, 0), 4)}|cand={candidate}")
            for offset, op_id in enumerate(reversed(history[-4:]), start=1):
                keys.append(f"recent_{offset}={op_id}|cand={candidate}")
                if op_id == candidate:
                    keys.append(f"recent_{offset}_repeat|cand={candidate}")
        return [self._bucket(key) for key in keys]

    def _score(self, app: str, history_ops: Iterable[int], candidate: int, quantile: float) -> float:
        weights = self.weights[app][quantile]
        return self.bias[app][quantile] + sum(weights[bucket] for bucket in self._feature_buckets(app, history_ops, candidate))

    def _update(self, app: str, history_ops: Iterable[int], candidate: int, target: float) -> None:
        buckets = self._feature_buckets(app, history_ops, candidate)
        for quantile in self.quantiles:
            pred = self._score(app, history_ops, candidate, quantile)
            residual = target - pred
            grad = quantile if residual > 0 else quantile - 1.0
            step = self.learning_rate * grad
            self.bias[app][quantile] += step
            for bucket in buckets:
                self.weights[app][quantile][bucket] += step

    def fit(self, samples: Iterable[dict]) -> None:
        rows = list(samples)
        for sample in rows:
            app = sample["app"]
            label = int(sample["label_next_op"])
            if label not in self._special_ids(app):
                self.label_counts[app][label] += 1

        self.candidates = {app: self._candidate_ids(app) for app in self.label_counts}

        for _ in range(self.epochs):
            for sample in rows:
                app = sample["app"]
                label = int(sample["label_next_op"])
                if label in self._special_ids(app):
                    continue
                candidates = self.candidates.get(app, [])
                if not candidates:
                    continue
                for candidate in candidates:
                    target = 1.0 if candidate == label else 0.0
                    self._update(app, sample.get("history_ops", []), candidate, target)

    def predict_topk(self, app: str, history_ops: Iterable[int], k: int = 5) -> list[int]:
        special_ids = self._special_ids(app)
        candidates = self.candidates.get(app) or self._candidate_ids(app)
        ranked = [
            op_id
            for op_id, _ in sorted(
                (
                    (op_id, self._score(app, history_ops, op_id, self.rank_quantile))
                    for op_id in candidates
                    if op_id not in special_ids
                ),
                key=lambda item: (-item[1], item[0]),
            )
        ]
        return ranked[:k]
