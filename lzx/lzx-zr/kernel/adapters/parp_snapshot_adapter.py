from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

Q15_ONE = 32767


def snapshot_is_usable(snapshot: dict[str, Any], *, now_ns: int | None = None) -> bool:
    """Accept only fresh, typed, non-unknown predictions for SHADOW hints."""
    if snapshot.get("mode") not in {"OBSERVE", "SHADOW"}:
        return False
    if snapshot.get("apply") is True or snapshot.get("native_fallback") is True:
        return False
    ttl_ms = int(snapshot.get("ttl_ms", 0))
    timestamp_ns = int(snapshot.get("timestamp_ns", 0))
    current_ns = time.time_ns() if now_ns is None else int(now_ns)
    if ttl_ms <= 0 or timestamp_ns <= 0 or current_ns > timestamp_ns + ttl_ms * 1_000_000:
        return False
    current = snapshot.get("current_workload", {})
    next_workload = snapshot.get("next_workload", {})
    if current.get("dominant") in {None, "UNKNOWN", "MIXED"}:
        return False
    if next_workload.get("dominant") in {None, "UNKNOWN", "MIXED"}:
        return False
    confidence = int(current.get("confidence_q15", -1))
    probability = int(next_workload.get("probability_q15", -1))
    return 0 <= confidence <= Q15_ONE and 0 <= probability <= Q15_ONE


def to_parp_shadow_hint(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Create a bounded, observe-only hint compatible with PARP snapshot concepts."""
    valid = snapshot_is_usable(snapshot)
    return {
        "interface": "parp-snapshot-adapter-v1",
        "mode": "SHADOW" if valid else "NATIVE",
        "prediction_seq": int(snapshot.get("prediction_seq", 0)),
        "ttl_ms": int(snapshot.get("ttl_ms", 0)),
        "app_or_scope": {"scope_type": snapshot.get("scope_type"), "scope_id": snapshot.get("scope_id")},
        "protection_hint": "protect_next_workload" if valid else "native",
        "reclaim_hint": "preserve_hot_regions" if valid else "native",
        "preclean_hint": "defer_to_kernel_policy",
        "compression_hint": "observe_only",
        "migration_hint": "observe_only",
        "confidence_q15": int(snapshot.get("current_workload", {}).get("confidence_q15", 0)),
        "probability_q15": int(snapshot.get("next_workload", {}).get("probability_q15", 0)),
        "q15_max": Q15_ONE,
        "apply": False,
    }


def write_hint(path: str | Path, snapshot: dict[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(to_parp_shadow_hint(snapshot), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
