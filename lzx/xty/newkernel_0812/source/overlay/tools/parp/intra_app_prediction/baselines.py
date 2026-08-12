"""Replaceable, dependency-free segment baselines."""

from collections import Counter


class LastWindow:
    def fit(self, _windows):
        return self

    def predict(self, windows):
        return {segment: 1.0 for segment in (windows[-1] if windows else set())}


class RecentFrequency:
    def __init__(self, history=6):
        if history <= 0:
            raise ValueError("history must be positive")
        self.history = history

    def fit(self, _windows):
        return self

    def predict(self, windows):
        recent = windows[-self.history:]
        if not recent:
            return {}
        counts = Counter(segment for window in recent for segment in window)
        return {segment: count / len(recent)
                for segment, count in sorted(counts.items())}


class GlobalFrequency:
    def __init__(self):
        self.probabilities = {}

    def fit(self, windows):
        windows = list(windows)
        counts = Counter(segment for window in windows for segment in window)
        self.probabilities = {segment: count / len(windows)
                              for segment, count in counts.items()} if windows else {}
        return self

    def predict(self, _windows):
        return dict(self.probabilities)
