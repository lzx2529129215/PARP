from __future__ import annotations

import json
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from predictor.workload_predictor import Prediction


def make_snapshot(prediction: Prediction, features: Any, *, mode: str = "OBSERVE", native_fallback: bool = False) -> dict[str, Any]:
    state = prediction.current
    next_state = prediction.next
    scope = features.scope
    return {
        "scope_type": scope["scope"],
        "scope_id": scope["scope_id"],
        "timestamp_ns": scope["timestamp"],
        "current_workload": {
            "access_order": state.access_order,
            "reuse_mode": state.reuse_mode,
            "hotspot_mode": state.hotspot_mode,
            "phase_mode": state.phase_mode,
            "dominant": state.dominant,
            "confidence_q15": state.confidence_q15,
        },
        "next_workload": {
            "access_order": next_state.access_order,
            "reuse_mode": next_state.reuse_mode,
            "hotspot_mode": next_state.hotspot_mode,
            "phase_mode": next_state.phase_mode,
            "dominant": next_state.dominant,
            "probability_q15": prediction.probability_q15,
        },
        "horizon_ms": prediction.horizon_ms,
        "ttl_ms": prediction.ttl_ms,
        "prediction_seq": prediction.prediction_seq,
        "wss_pages": int(features.working_set["wss_pages"]),
        "wss_slope_pages_per_sec": features.working_set["wss_slope_pages_per_sec"],
        "model_version": prediction.model_version,
        "method": prediction.method,
        "mode": mode,
        "native_fallback": native_fallback,
        "generated_at_ns": time.time_ns(),
    }


def write_snapshot(path: str | Path, snapshot: dict[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
