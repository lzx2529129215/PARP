"""Chronological, session-disjoint split with future-horizon purge gaps."""

from collections import defaultdict


def chronological_split(rows, purge_gap_ns=60_000_000_000):
    rows = [dict(row) for row in rows]
    ids = [row["window_id"] for row in rows]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate window_id")
    sessions = defaultdict(list)
    for row in rows:
        sessions[row["session_id"]].append(row)
    ordered = sorted(sessions, key=lambda key: min(
        row["window_start_ns"] for row in sessions[key]))
    if len(ordered) < 3:
        raise ValueError("at least three sessions are required")
    train_count = max(1, int(len(ordered) * .70))
    val_count = max(1, int(len(ordered) * .15))
    if train_count + val_count >= len(ordered):
        train_count = len(ordered) - 2
        val_count = 1
    session_sets = {
        "train": ordered[:train_count],
        "val": ordered[train_count:train_count + val_count],
        "test": ordered[train_count + val_count:],
    }
    result = {}
    previous_max = None
    purged = 0
    for name in ("train", "val", "test"):
        selected = sorted((row for session in session_sets[name]
                           for row in sessions[session]),
                          key=lambda row: row["window_start_ns"])
        if previous_max is not None:
            before = len(selected)
            selected = [row for row in selected
                        if row["window_start_ns"] - previous_max >= purge_gap_ns]
            purged += before - len(selected)
        if not selected:
            raise ValueError("purge gap removed an entire split")
        result[name] = selected
        previous_max = selected[-1]["window_start_ns"]
    result["audit"] = {
        "purge_gap_ns": purge_gap_ns,
        "purged_windows": purged,
        "session_disjoint": True,
        "future_horizon_isolated": True,
        "normalization_source": "train",
        "clustering_source": "train",
        "sessions": session_sets,
    }
    return result
