"""Deterministic causal inactive candidate selectors.

This module intentionally has no access to the downstream scoring/annotation
objects.  It only consumes a decision-time candidate mapping.
"""

import hashlib
import json
import math


QUOTAS = {
    "Q_BALANCED": (32, 32, 32, 32),
    "Q_COLD_HEAVY": (64, 32, 16, 16),
    "Q_MIDDLE": (48, 32, 24, 24),
}


def _hash(items):
    value = "\n".join(item["identity"] for item in items)
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _number(row, name, default=0.0):
    try:
        value = float(row.get(name, default))
        return value if math.isfinite(value) else float(default)
    except (TypeError, ValueError):
        return float(default)


def eligible_universe(rows):
    output = []
    expected_session = next((row.get("session_id") for row in rows if row.get("session_id") not in (None, "")), None)
    expected_domain = next((row.get("domain_id") for row in rows if row.get("domain_id") not in (None, "")), None)
    for source in rows:
        row = dict(source)
        flags = row.get("validity_flags", {})
        if row.get("current_active") is not False:
            continue
        if row.get("observation_state") in (None, "NOT_OBSERVED"):
            continue
        if flags and (not flags.get("file_version", True) or not flags.get("partition_generation", True)):
            continue
        if not row.get("identity") or row.get("file_version") in (None, ""):
            continue
        if row.get("partition_generation") in (None, ""):
            continue
        history_fields = ("age", "segment_age", "time_since_last_active", "recent_access_count")
        if not any(row.get(field) is not None for field in history_fields):
            continue
        if expected_session is not None and row.get("session_id") not in (None, expected_session):
            continue
        if expected_domain is not None and row.get("domain_id") not in (None, expected_domain):
            continue
        output.append(row)
    return output


def _quantile_strata(rows, field, names, reverse=False):
    ordered = sorted(
        range(len(rows)),
        key=lambda index: (_number(rows[index], field), rows[index]["identity"]),
        reverse=reverse,
    )
    result = [None] * len(rows)
    for position, index in enumerate(ordered):
        bucket = min(len(names) - 1, position * len(names) // max(1, len(rows)))
        result[index] = names[bucket]
    return result


def assign_generation_strata(rows):
    rows = [dict(row) for row in rows]
    values = sorted({_number(row, "generation_proxy") for row in rows})
    ranks = {}
    for index, value in enumerate(values):
        ranks[value] = min(3, index * 4 // max(1, len(values)))
    for row in rows:
        rank = ranks.get(_number(row, "generation_proxy"), 3)
        row["generation_rank"] = rank
        row["generation_stratum"] = "G%d" % rank
        row["tier_proxy"] = _number(row, "generation_proxy")
    return rows


def assign_recency_strata(rows):
    rows = [dict(row) for row in rows]
    strata = _quantile_strata(rows, "time_since_last_active", ("R0", "R1", "R2", "R3"), reverse=True)
    for row, name in zip(rows, strata):
        row["recency_stratum"] = name
    return rows


def assign_hybrid_strata(rows):
    rows = assign_recency_strata(assign_generation_strata(rows))
    for row in rows:
        generation = row["generation_stratum"]
        recency = row["recency_stratum"]
        if generation == "G0" and recency == "R0":
            name = "H0"
        elif generation in ("G0", "G1") or recency in ("R0", "R1"):
            name = "H1"
        elif generation in ("G1", "G2") or recency in ("R1", "R2"):
            name = "H2"
        else:
            name = "H3"
        row["hybrid_stratum"] = name
    return rows


def _fallback_strata(rows):
    rows = [dict(row) for row in rows]
    strata = _quantile_strata(rows, "time_since_last_active", ("F0", "F1", "F2", "F3"), reverse=True)
    for row, name in zip(rows, strata):
        row["fallback_stratum"] = name
    return rows


def _stable_key(row):
    return (
        -_number(row, "generation_proxy"),
        -_number(row, "time_since_last_active"),
        -_number(row, "age"),
        -_number(row, "consecutive_inactive_windows", row.get("consecutive_inactive", 0)),
        int(row.get("ordinal", 0)),
        str(row.get("identity", "")),
    )


def _cold_key(row):
    return (
        _number(row, "generation_proxy"),
        -_number(row, "time_since_last_active"),
        -_number(row, "age"),
        -_number(row, "consecutive_inactive_windows", row.get("consecutive_inactive", 0)),
        str(row.get("identity", "")),
    )


def _fill(rows, strata_field, names, quotas):
    groups = {name: [] for name in names}
    for row in rows:
        groups.setdefault(row[strata_field], []).append(row)
    for values in groups.values():
        values.sort(key=_cold_key)
    selected = []
    fill_counts = {name: 0 for name in names}
    for name, quota in zip(names, quotas):
        take = groups[name][:quota]
        selected.extend(take)
        fill_counts[name] += len(take)
    needed = min(128, len(rows)) - len(selected)
    if needed > 0:
        for name in names:
            remainder = groups[name][fill_counts[name]:]
            take = remainder[:needed]
            selected.extend(take)
            fill_counts[name] += len(take)
            needed -= len(take)
            if needed == 0:
                break
    selected = sorted(selected, key=_stable_key)
    return selected, fill_counts


def select(rows, selector_id, quota_template="Q_BALANCED"):
    if quota_template not in QUOTAS:
        raise ValueError("unknown quota template")
    rows = eligible_universe(rows)
    fallback_only_generation_missing = False
    if selector_id == "S0":
        selected = sorted(rows, key=_stable_key)[:128]
        strata_field, names = None, ()
        fill_counts = {}
    else:
        if selector_id == "S1":
            rows = assign_generation_strata(rows)
            strata_field, names = "generation_stratum", ("G0", "G1", "G2", "G3")
        elif selector_id == "S2":
            rows = assign_recency_strata(rows)
            strata_field, names = "recency_stratum", ("R0", "R1", "R2", "R3")
        elif selector_id == "S3":
            rows = assign_hybrid_strata(rows)
            strata_field, names = "hybrid_stratum", ("H0", "H1", "H2", "H3")
        elif selector_id == "S4":
            rows = [row for row in rows if row.get("generation_proxy") in (None, "")]
            fallback_only_generation_missing = True
            rows = _fallback_strata(rows)
            strata_field, names = "fallback_stratum", ("F0", "F1", "F2", "F3")
        else:
            raise ValueError("unknown selector")
        selected, fill_counts = _fill(rows, strata_field, names, QUOTAS[quota_template])
    for row in selected:
        row["selector_id"] = selector_id
        row["quota_template"] = quota_template
    candidate_hash = _hash(selected)
    return selected, {
        "selector_id": selector_id,
        "quota_template": quota_template,
        "candidate_count": len(selected),
        "partial_candidate_universe": len(rows) < 128,
        "duplicate_count": len(selected) - len({row["identity"] for row in selected}),
        "current_active_count": sum(bool(row.get("current_active")) for row in selected),
        "current_inactive_count": sum(not bool(row.get("current_active")) for row in selected),
        "candidate_hash": candidate_hash,
        "stratum_counts": {name: sum(row.get(strata_field) == name for row in selected) for name in names} if names else {},
        "fill_counts": fill_counts,
        "fallback_only_generation_missing": fallback_only_generation_missing,
        "stable_sort_contract": "generation_age_recency_inactivity_identity",
        "causal_inputs_only": True,
    }


def source_contract():
    return {
        "candidate_count": 128,
        "active_allowed": False,
        "unknown_allowed": False,
        "file_id_role": "identity_version_check_and_deterministic_tie_break_only",
        "features": [
            "generation_proxy", "generation_rank", "tier_proxy", "age",
            "time_since_last_active", "time_since_last_observed",
            "consecutive_inactive_windows", "recent_access_count",
            "segment_access_ema", "file_access_ema",
        ],
        "causal_only": True,
    }
