#!/usr/bin/env python3
"""Select Test4B-2's threshold and consecutive-batch rule offline only."""
from __future__ import annotations

import argparse
import csv
import glob
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
TARGETS = {"FIREFOX", "THUNDERBIRD", "TELEGRAM"}
GRID = (.05, .08, .10, .12, .15, .20)
CONSECUTIVE_BATCHES = (1, 2, 3)


def rows(path: Path) -> list[dict[str, str]]:
    try:
        with path.open(encoding="utf-8", newline="") as stream:
            return list(csv.DictReader(stream))
    except OSError:
        return []


def next_foreground(events: list[dict[str, str]], ts: int) -> str:
    for item in events:
        try:
            event_ts = int(item.get("ts_ns", 0))
        except ValueError:
            continue
        if event_ts <= ts or item.get("event_type") not in {"APP_OPEN", "APP_SWITCH"}:
            continue
        value = item.get("foreground_app") or item.get("new_app") or item.get("app") or ""
        if value in TARGETS:
            return value
    return ""


def main() -> None:
    parser = argparse.ArgumentParser(); parser.add_argument("--output", type=Path, required=True); args = parser.parse_args()
    batches: list[dict[str, Any]] = []
    pattern = str(ROOT / "outputs/runtime_monitor/test4_probability_activity_reclaim_shadow_*_r*/model")
    for model_dir in sorted(glob.glob(pattern)):
        model = Path(model_dir); direct = rows(model / "direct_app_events.csv")
        predictions = rows(model / "online_lstm_predictions.csv")
        by_id: dict[str, list[dict[str, str]]] = defaultdict(list)
        for row in predictions:
            # The v3 normalized CSV identifies an inference batch by the
            # event-time feature_window_id; direct_app_events retains the
            # externally visible prediction_id.  They are intentionally not
            # the same string, so join by event_id/feature_window_id.
            if row.get("feature_window_id") and row.get("app_key") in TARGETS:
                by_id[row["feature_window_id"]].append(row)
        for event in direct:
            event_id = event.get("event_id", "")
            prediction_id = event.get("prediction_id", "")
            if not prediction_id or event_id not in by_id:
                continue
            try: timestamp = int(event.get("ts_ns", 0))
            except ValueError: continue
            actual = next_foreground(direct, timestamp)
            if not actual: continue
            probabilities: dict[str, float] = {}
            for row in by_id[event_id]:
                try: probability = float(row.get("probability", ""))
                except ValueError: continue
                probabilities[row["app_key"]] = probability
            if probabilities:
                batches.append({"session": model.parent.name, "prediction_id": prediction_id, "timestamp_ns": timestamp,
                                "probabilities": probabilities, "actual_next": actual})
    grid: list[dict[str, Any]] = []
    for threshold in GRID:
        for required in CONSECUTIVE_BATCHES:
            candidates: list[dict[str, Any]] = []
            by_session: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for batch in batches: by_session[str(batch["session"])].append(batch)
            for session_batches in by_session.values():
                low = {app: 0 for app in TARGETS}
                for batch in sorted(session_batches, key=lambda item: int(item["timestamp_ns"])):
                    for app in TARGETS:
                        probability = float(batch["probabilities"].get(app, 1.0))
                        low[app] = low[app] + 1 if probability < threshold else 0
                        if low[app] >= required:
                            candidates.append({"candidate": app, "probability": probability, "actual_next": batch["actual_next"], "streak": low[app]})
            hits = [item for item in candidates if item["candidate"] == item["actual_next"]]
            grid.append({"threshold": threshold, "required_low_probability_batches": required,
                         "candidate_rows": len(candidates), "next_foreground_hit_rows": len(hits),
                         "next_foreground_hit_risk": len(hits) / len(candidates) if candidates else None,
                         "candidate_apps": sorted({item["candidate"] for item in candidates}),
                         "selection_eligible": len(candidates) >= 3 and (len(hits) / len(candidates) if candidates else 1.0) <= .10})
    # K=2 is preferred as the actual Test4B-2 low-probability continuity
    # policy.  K=1 is only an explicitly reported fallback when validation
    # evidence does not support K=2; K=3 is shown for sensitivity.
    selected = next((item for preferred_k in (2, 1, 3) for item in grid
                     if item["required_low_probability_batches"] == preferred_k and item["selection_eligible"]), None)
    result = {"status": "READY" if selected else "BLOCKED", "scope": "prior Test4 validation-split sessions only; no future labels enter runtime control",
              "evidence_batches": len(batches), "evidence_rows": sum(len(item["probabilities"]) for item in batches), "grid": grid, "selected": selected,
              "selection_rule": "smallest threshold at preferred K=2 (then K=1, then K=3) with >=3 candidates and next-foreground risk <=0.10; candidate count is not a runtime success criterion"}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
