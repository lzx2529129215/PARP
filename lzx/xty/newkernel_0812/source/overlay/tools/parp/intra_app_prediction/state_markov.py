"""Duration-aware PageAccessState transition model (not operation Markov)."""

from collections import Counter, defaultdict


DURATION_BINS = ((10, "0_10"), (30, "10_30"), (60, "30_60"),
                 (float("inf"), "60_plus"))


def duration_bin(seconds):
    if seconds < 0:
        raise ValueError("duration cannot be negative")
    for bound, name in DURATION_BINS:
        if seconds <= bound:
            return name
    raise AssertionError("unreachable")


class PageStateMarkov:
    model_type = "page_state_markov"

    def __init__(self):
        self.transitions = defaultdict(Counter)

    def fit(self, transitions):
        for current, duration, following in transitions:
            self.transitions[(int(current), str(duration))][int(following)] += 1
        return self

    def predict(self, current, duration):
        counts = self.transitions.get((int(current), str(duration)))
        if not counts:
            return {}
        total = sum(counts.values())
        return {state: count / total for state, count in sorted(counts.items())}

    def to_dict(self):
        return {f"{state}|{duration}": dict(counts)
                for (state, duration), counts in sorted(self.transitions.items())}
