"""Causal native-tail distance and fixed-budget v2.1 templates."""

import hashlib
import math

from phase210b.selector_v2 import eligible_universe


QUOTAS = {
    "C1": (64, 48, 16, 0),
    "C2": (56, 40, 24, 8),
    "C3": (48, 48, 32, 0),
    "C4": (40, 40, 32, 16),
}
BANDS = ("T0", "T1", "T2", "T3")


def number(row, field, fallback=0.0):
    value = row.get(field, fallback)
    try:
        value = float(value)
        return value if math.isfinite(value) else float(fallback)
    except (TypeError, ValueError):
        return float(fallback)


def native_key(row):
    """Exact V1 causal comparator, excluding active rows before sorting."""
    return (
        -number(row, "generation_proxy"),
        -number(row, "delta_since_last_access", row.get("time_since_last_active", 0)),
        -number(row, "segment_age", row.get("age", 0)),
        int(row.get("ordinal", 0) or 0),
        str(row.get("identity", "")),
    )


def candidate_hash(rows):
    return hashlib.sha256("\n".join(row["identity"] for row in rows).encode("utf-8")).hexdigest()


def native_tail_order(rows):
    eligible = eligible_universe(rows)
    ordered = sorted(eligible, key=native_key)
    size = max(len(ordered) - 1, 1)
    output = []
    for rank, source in enumerate(ordered):
        row = dict(source)
        row["tail_rank"] = rank
        row["tail_distance"] = rank / size
        output.append(row)
    return output


def assign_bands(rows):
    output = []
    for source in native_tail_order(rows):
        row = dict(source)
        distance = row["tail_distance"]
        if distance <= .01:
            band = "T0"
        elif distance <= .05:
            band = "T1"
        elif distance <= .10:
            band = "T2"
        elif distance <= .20:
            band = "T3"
        else:
            band = "EXCLUDED"
        row["tail_band"] = band
        output.append(row)
    return output


def _fill_sources(band):
    return {"T0": ("T1", "T2", "T3"), "T1": ("T0", "T2", "T3"), "T2": ("T1", "T0", "T3"), "T3": ("T2", "T1", "T0")}[band]


def select(rows, template):
    if template not in QUOTAS:
        raise ValueError("unknown template")
    banded = assign_bands(rows)
    groups = {band: [row for row in banded if row["tail_band"] == band] for band in BANDS}
    selected = []
    used = set()
    requested = dict(zip(BANDS, QUOTAS[template]))
    fill_source = []
    for band in BANDS:
        for row in groups[band][:requested[band]]:
            selected.append(row); used.add(row["identity"])
    for band in BANDS:
        missing = requested[band] - sum(row["tail_band"] == band for row in selected)
        for source in _fill_sources(band):
            for row in groups[source]:
                if missing <= 0: break
                if row["identity"] in used: continue
                selected.append(row); used.add(row["identity"]); missing -= 1
                fill_source.append({"requested_band": band, "source_band": source, "identity": row["identity"]})
            if missing <= 0: break
    selected = sorted(selected, key=lambda row: (row["tail_rank"], row["identity"]))[:128]
    actual = {band: sum(row["tail_band"] == band for row in selected) for band in BANDS}
    audit = {
        "selector_id": "TAIL_CONSTRAINED_SELECTOR_V21",
        "selector_version": "2.1",
        "quota_template": template,
        "candidate_count": len(selected),
        "partial_tail_universe": len([row for row in banded if row["tail_distance"] <= .20]) < 128,
        "duplicate_count": len(selected) - len({row["identity"] for row in selected}),
        "current_active_count": sum(bool(row.get("current_active")) for row in selected),
        "not_observed_count": sum(row.get("observation_state") == "NOT_OBSERVED" for row in selected),
        "tail_over_20_count": sum(row.get("tail_distance", 1) > .20 for row in selected),
        "requested_quota": requested,
        "actual_stratum_counts": actual,
        "fill_source": fill_source,
        "fill_count": len(fill_source),
        "candidate_hash": candidate_hash(selected),
        "candidate_order_hash": candidate_hash(selected),
        "causal_inputs_only": True,
        "stable_sort_contract": "native_v1_tail_rank_then_identity",
    }
    return selected, audit, banded


def realism(universe, selected):
    age = lambda row: number(row, "age", row.get("segment_age", 0))
    gap = lambda row: number(row, "time_since_last_active", row.get("delta_since_last_access", 0))
    generation = sorted(number(row, "generation_proxy") for row in universe)
    cutoff = generation[len(generation) // 2] if generation else 0
    metrics = {
        "universe_count": len(universe), "selected_count": len(selected),
        "current_active_ratio": sum(bool(row.get("current_active")) for row in selected) / max(1, len(selected)),
        "current_inactive_ratio": sum(not bool(row.get("current_active")) for row in selected) / max(1, len(selected)),
        "not_observed_count": sum(row.get("observation_state") == "NOT_OBSERVED" for row in selected),
        "duplicate_count": len(selected) - len({row.get("identity") for row in selected}),
        "tail_over_20_count": sum(number(row, "tail_distance", 1) > .20 for row in selected),
        "t3_ratio": sum(.10 < number(row, "tail_distance", 1) <= .20 for row in selected) / max(1, len(selected)),
        "oldest_half_ratio": sum(number(row, "generation_proxy") <= cutoff for row in selected) / max(1, len(selected)),
        "top10_tail_ratio": sum(number(row, "tail_distance", 1) <= .10 for row in selected) / max(1, len(selected)),
        "top1_tail_ratio": sum(number(row, "tail_distance", 1) <= .01 for row in selected) / max(1, len(selected)),
        "top5_tail_ratio": sum(number(row, "tail_distance", 1) <= .05 for row in selected) / max(1, len(selected)),
        "top20_tail_ratio": sum(number(row, "tail_distance", 1) <= .20 for row in selected) / max(1, len(selected)),
        "selected_age_median": sorted(age(row) for row in selected)[len(selected) // 2] if selected else None,
        "universe_age_median": sorted(age(row) for row in universe)[len(universe) // 2] if universe else None,
        "selected_gap_median": sorted(gap(row) for row in selected)[len(selected) // 2] if selected else None,
        "universe_gap_median": sorted(gap(row) for row in universe)[len(universe) // 2] if universe else None,
    }
    metrics["hard_passed"] = all((
        len(selected) == 128,
        metrics["current_active_ratio"] == 0,
        metrics["current_inactive_ratio"] == 1,
        metrics["not_observed_count"] == 0,
        metrics["duplicate_count"] == 0,
        metrics["tail_over_20_count"] == 0,
        metrics["t3_ratio"] <= .125,
        metrics["oldest_half_ratio"] >= .70,
        metrics["top10_tail_ratio"] >= .75,
        metrics["selected_age_median"] is not None and metrics["selected_age_median"] >= metrics["universe_age_median"],
        metrics["selected_gap_median"] is not None and metrics["selected_gap_median"] >= metrics["universe_gap_median"],
    ))
    return metrics


def support_gate(row):
    return row.get("positive_count", 0) >= 20 and row.get("pairwise_evaluable_decisions", 0) >= 10
