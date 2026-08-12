"""Independent future-window labeler for frozen selector outputs."""

from collections import defaultdict


def _key(candidate):
    return candidate["file_key_metadata"], candidate["partition_generation"]


def _observed_active(windows, index, candidate, horizon):
    key = _key(candidate)
    segment = int(candidate["segment_id"])
    end = index + horizon // 10
    if end >= len(windows):
        return "unknown", None
    seen_all = True
    for offset in range(1, horizon // 10 + 1):
        item = windows[index + offset]["files"].get(key)
        if item is None or not ((item["observed"] >> segment) & 1):
            seen_all = False
            continue
        if (item["active"] >> segment) & 1:
            return "positive", offset * 10
    return ("negative" if seen_all else "unknown"), None


def label_selection(selection, windows, index, horizons=(10, 30, 60, 120)):
    rows = []
    for candidate in selection:
        for horizon in horizons:
            status, reuse_seconds = _observed_active(windows, index, candidate, horizon)
            complete = index + horizon // 10 < len(windows)
            rows.append({
                "decision_id": candidate["decision_id"],
                "identity": candidate["identity"],
                "horizon_seconds": horizon,
                "status": status,
                "available": status != "unknown",
                "complete_future_window": complete,
                "reuse_seconds": reuse_seconds,
            })
    return rows


def support(rows):
    grouped = defaultdict(list)
    for row in rows:
        grouped[(row["selector_id"], row["quota_template"], row["app"], row["session_id"], row["horizon_seconds"])].append(row)
    output = []
    for key, values in sorted(grouped.items()):
        complete_values = [row for row in values if row.get("complete_future_window", True)]
        available = [row for row in complete_values if row["available"]]
        positives = [row for row in available if row["status"] == "positive"]
        negatives = [row for row in available if row["status"] == "negative"]
        decisions = {}
        for row in complete_values:
            decisions.setdefault(row["decision_id"], []).append(row)
        pairwise = sum(
            any(item["status"] == "positive" and item["available"] for item in items)
            and any(item["status"] == "negative" and item["available"] for item in items)
            for items in decisions.values()
        )
        per_decision = [sum(item["status"] == "positive" for item in items if item["available"]) for items in decisions.values()]
        output.append({
            "selector_id": key[0], "quota_template": key[1], "app": key[2],
            "session_id": key[3], "horizon_seconds": key[4],
            "complete_future_decisions": len(decisions),
            "candidate_count": len(complete_values),
            "label_available_count": len(available),
            "positive_count": len(positives),
            "negative_count": len(negatives),
            "unknown_count": len(complete_values) - len(available),
            "positive_ratio": len(positives) / len(available) if available else 0.0,
            "decisions_with_positive": sum(any(item["status"] == "positive" for item in items) for items in decisions.values()),
            "pairwise_evaluable_decisions": pairwise,
            "possible_pair_count": len(positives) * len(negatives),
            "median_positive_per_decision": sorted(per_decision)[len(per_decision) // 2] if per_decision else 0,
            "p95_positive_per_decision": sorted(per_decision)[min(len(per_decision) - 1, int(len(per_decision) * 0.95))] if per_decision else 0,
        })
    return output
