"""Causal page history, probability, refault, and Apply safety primitives."""


def stable_topk(rows, count):
    return sorted(rows, key=lambda row: (-row["score"], -row["weighted"],
                                         -row["active"], str(row["key"])))[:count]


def summarize_other(rows, top):
    selected = {row["key"] for row in top}
    other = [row for row in rows if row["key"] not in selected]
    return {"count": len(other), "active": sum(row["active"] for row in other),
            "weighted": sum(row["weighted"] for row in other)}


def causal_history(rows, index, length):
    session = rows[index]["session"]
    return [row for row in rows[max(0, index - length + 1):index + 1]
            if row["session"] == session]


def cumulative_labels(states, current_index, last_index):
    output = {}
    for horizon in (10, 30, 60):
        steps = horizon // 10
        available = current_index + steps <= last_index
        future = [states.get(index) for index in range(current_index + 1,
                                                        current_index + steps + 1)]
        output[horizon] = {"available": available,
                           "active": any(value is True for value in future) if available else None}
    return output


def monotonic_probabilities(p10, p30, p60):
    p10 = max(0.0, min(1.0, p10))
    p30 = max(p10, max(0.0, min(1.0, p30)))
    p60 = max(p30, max(0.0, min(1.0, p60)))
    return p10, p30, p60


def version_prediction_valid(predicted_version, current_version,
                             predicted_generation, current_generation):
    return (predicted_version == current_version and
            predicted_generation == current_generation)


def normalized_refault(refaults, reclaimed_pages):
    return refaults * 1000.0 / reclaimed_pages if reclaimed_pages else None


def reclaim_comparable(reference, candidate, tolerance):
    if reference <= 0: return False
    return abs(candidate - reference) / reference <= tolerance


class ApplyGuard:
    def __init__(self, target_domain, max_pages, max_ratio):
        self.target_domain = target_domain
        self.max_pages = max_pages
        self.max_ratio = max_ratio
        self.tripped = False
        self.reason = None

    def trip(self, reason):
        self.tripped = True; self.reason = reason

    def evaluate(self, domain, now_ns, expires_ns, pages, apply_enabled,
                 domain_pages=100):
        if domain != self.target_domain: return "DOMAIN_MISMATCH"
        if now_ns > expires_ns: return "EXPIRED"
        if self.tripped: return "CIRCUIT_BREAKER"
        if not apply_enabled: return "OBSERVE_ONLY"
        if pages > self.max_pages or pages > domain_pages * self.max_ratio:
            return "BUDGET_EXCEEDED"
        return "ALLOW_PROTECT"
