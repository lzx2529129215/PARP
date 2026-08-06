"""Shape-constrained neural-style scorer for in-app operation prediction."""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Iterable


class OpScnnModel:
    """A small shape-constrained scorer without external dependencies.

    The model learns linear candidate scores with a projected perceptron update.
    Features that encode repeat evidence are constrained to be non-negative, so
    increasing repeat evidence cannot reduce a candidate's score.
    """

    def __init__(
        self,
        op_vocab: dict[str, dict[str, int]] | None = None,
        epochs: int = 5,
        learning_rate: float = 0.1,
    ) -> None:
        if epochs <= 0:
            raise ValueError("epochs must be positive")
        if learning_rate <= 0:
            raise ValueError("learning_rate must be positive")
        self.op_vocab = op_vocab or {}
        self.epochs = epochs
        self.learning_rate = learning_rate
        self.free_weights: dict[str, Counter[str]] = defaultdict(Counter)
        self.constrained_weights: dict[str, Counter[str]] = defaultdict(Counter)
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

    def _features(
        self,
        app: str,
        history_ops: Iterable[int],
        candidate: int,
    ) -> tuple[list[tuple[str, float]], list[tuple[str, float]]]:
        history = self._history(app, history_ops)
        free = [
            ("bias", 1.0),
            (f"cand={candidate}", 1.0),
            (f"hist_len={min(len(history), 8)}", 1.0),
        ]
        constrained: list[tuple[str, float]] = []
        if not history:
            return free, constrained

        counts = Counter(history)
        cand_count = min(counts.get(candidate, 0), 4)
        constrained.append(("cand_seen_in_history", 1.0 if cand_count else 0.0))
        constrained.append(("cand_history_count", float(cand_count)))

        for offset, op_id in enumerate(reversed(history[-4:]), start=1):
            free.append((f"recent_{offset}={op_id}", 1.0))
            free.append((f"recent_{offset}={op_id}|cand={candidate}", 1.0))
            if op_id == candidate:
                constrained.append((f"recent_{offset}_repeat", 1.0))
        return free, constrained

    def _score(self, app: str, history_ops: Iterable[int], candidate: int) -> float:
        free, constrained = self._features(app, history_ops, candidate)
        free_weights = self.free_weights[app]
        constrained_weights = self.constrained_weights[app]
        return sum(free_weights[name] * value for name, value in free) + sum(
            constrained_weights[name] * value for name, value in constrained
        )

    def _ranked_candidates(self, app: str, history_ops: Iterable[int]) -> list[int]:
        candidates = self.candidates.get(app) or self._candidate_ids(app)
        return [
            op_id
            for op_id, _ in sorted(
                ((op_id, self._score(app, history_ops, op_id)) for op_id in candidates),
                key=lambda item: (-item[1], item[0]),
            )
        ]

    def _apply_features(
        self,
        app: str,
        history_ops: Iterable[int],
        candidate: int,
        direction: float,
    ) -> None:
        free, constrained = self._features(app, history_ops, candidate)
        for name, value in free:
            self.free_weights[app][name] += direction * self.learning_rate * value
        for name, value in constrained:
            next_value = self.constrained_weights[app][name] + direction * self.learning_rate * value
            self.constrained_weights[app][name] = max(0.0, next_value)

    def _update(self, app: str, history_ops: Iterable[int], label: int, pred: int) -> None:
        self._apply_features(app, history_ops, label, 1.0)
        self._apply_features(app, history_ops, pred, -1.0)

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
                ranked = self._ranked_candidates(app, sample.get("history_ops", []))
                if not ranked:
                    continue
                pred = ranked[0]
                if pred != label:
                    self._update(app, sample.get("history_ops", []), label, pred)

    def predict_topk(self, app: str, history_ops: Iterable[int], k: int = 5) -> list[int]:
        special_ids = self._special_ids(app)
        ranked = [op_id for op_id in self._ranked_candidates(app, history_ops) if op_id not in special_ids]
        return ranked[:k]
