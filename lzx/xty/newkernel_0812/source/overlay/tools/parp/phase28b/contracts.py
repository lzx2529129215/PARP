"""Small deterministic contracts shared by the Phase2.8B pipeline."""

from dataclasses import dataclass


FORBIDDEN = ("operation", "action", "automation", "scenario", "repeat", "label",
             "ground_truth", "next_op", "current_op", "dominant_op", "window_title",
             "keyboard", "mouse", "path", "filename", "basename", "document",
             "file_id", "inode", "dev_major", "dev_minor")


@dataclass(frozen=True)
class WindowKey:
    session: str
    domain: int
    epoch: int
    start: int
    seconds: int


def align_start(timestamp_ns, seconds):
    width = seconds * 1_000_000_000
    return timestamp_ns // width * width


def assign_window(timestamp_ns, seconds):
    start = align_start(timestamp_ns, seconds)
    return start, start + seconds * 1_000_000_000


def causal_slice(rows, index, length):
    return rows[max(0, index - length + 1):index + 1]


def classify_quality(overlap):
    return "PURE" if overlap >= .8 else "MIXED" if overlap >= .5 else "LOW_CONFIDENCE"


def stable_topk(rows, count):
    return sorted(rows, key=lambda x: (-x["score"], -x["weighted"],
                                       -x["active"], str(x["key"])))[:count]


def summarize_other(rows, top):
    chosen = {x["key"] for x in top}
    rest = [x for x in rows if x["key"] not in chosen]
    return {"count": len(rest), "active": sum(x["active"] for x in rest),
            "weighted": sum(x["weighted"] for x in rest)}


def topk_conserves(rows, top, other, tolerance=1e-9):
    return (sum(x["active"] for x in rows) == sum(x["active"] for x in top) + other["active"] and
            abs(sum(x["weighted"] for x in rows) -
                sum(x["weighted"] for x in top) - other["weighted"]) <= tolerance)


def no_forbidden_features(names):
    return not any(word in name.lower() for name in names for word in FORBIDDEN)


def feature_source_complete(names, source_map):
    return set(names) == set(source_map) and all("source_type" in source_map[x] for x in names)


def operation_split():
    return {"wps_01": "train", "wps_02": "validation", "wps_03": "test",
            "files_01": "secondary_train", "files_02": "secondary_test"}


def label_pair_inventory(planned, starts, dones):
    return planned == starts == dones


def future_available(index, last_index, steps):
    return index + steps <= last_index


def cumulative_future(states, index, steps):
    future = states[index + 1:index + 1 + steps]
    if len(future) < steps or not any(x is not None for x in future):
        return (None, False)
    return (any(x is True for x in future if x is not None), True)


def enforce_monotonic(p10, p30, p60):
    p10 = min(1.0, max(0.0, p10)); p30 = max(p10, min(1.0, max(0.0, p30)))
    return p10, p30, max(p30, min(1.0, max(0.0, p60)))


def version_valid(predicted, current):
    return predicted == current


def partition_valid(predicted, current):
    return predicted == current


def normalize_refault_proxy(reuse, reclaimed):
    return reuse * 1000.0 / reclaimed if reclaimed else None


def raw_hash_equal(before, after):
    return before == after
