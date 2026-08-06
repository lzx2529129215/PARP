from __future__ import annotations

import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "runtime_monitor") not in sys.path:
    sys.path.insert(0, str(ROOT / "runtime_monitor"))
if str(ROOT / "runtime_monitor" / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "runtime_monitor" / "scripts"))

from analyze_test3_memory_shadow import build_join, strategy_comparison
from core.memory_shadow import BATCH_FIELDS, EPISODE_FIELDS, MEMORY_FIELDS


def write(path: Path, fields: list[str], rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader(); writer.writerows(rows)


class Test3AnalysisTests(unittest.TestCase):
    def test_join_requires_all_four_causal_evidence_groups(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            session = Path(directory)
            candidates = [{"app_key": "B", "app_id": "2", "app": "b", "probability": 0.8, "rank": 1, "running_state": "RUNNING_BACKGROUND"}]
            write(session / "prediction" / "prediction_episodes.csv", EPISODE_FIELDS, [{
                "session_id": "s", "episode_id": "e1", "prediction_id": "p1", "prediction_batch_id": "p1-shadow",
                "generated_at_ns": 1_000_000_000, "valid_until_ns": 9_000_000_000, "current_app": "A",
                "trigger_type": "direct_app_switch", "candidate_apps_json": json.dumps(candidates), "monitored_apps": "B",
                "terminal_time_ns": 2_000_000_000, "terminal_reason": "NEXT_APP_SWITCH", "actual_next_app": "B", "recovery_deadline_ns": 5_000_000_000,
            }])
            write(session / "prediction" / "prediction_batches.csv", BATCH_FIELDS, [{"session_id": "s", "prediction_id": "p1", "prediction_batch_id": "p1-shadow", "snapshot_version_end": "10"}])
            base = {field: 0 for field in MEMORY_FIELDS}
            rows = []
            for timestamp, reason, referenced, pgscan, pss, faults, refault in [
                (1_000_000_000, "T0_PREDICTION", 100, 0, 100, 0, 0),
                (1_500_000_000, "PERIODIC", 50, 2, 50, 0, 0),
                (3_000_000_000, "T2_RECOVERY_WINDOW", 120, 2, 120, 5, 1),
            ]:
                rows.append({**base, "session_id": "s", "timestamp_ns": timestamp, "sample_reason": reason,
                             "episode_id": "e1", "prediction_id": "p1", "prediction_batch_id": "p1-shadow",
                             "app_key": "B", "referenced_bytes": referenced, "pss_bytes": pss,
                             "pgscan": pgscan, "pgfault": faults, "workingset_refault_file": refault,
                             "metric_status": "OK"})
            write(session / "memory" / "app_memory_shadow_250ms.csv", MEMORY_FIELDS, rows)
            write(session / "model" / "automation_trace.csv", ["ts_ns", "event_type", "action", "app_key"], [])
            write(session / "model" / "foreground_events.csv", ["ts_ns", "event_type", "new_app"], [])
            join, episodes, _ = build_join(session)
            self.assertEqual(len(join), 1)
            self.assertEqual(join[0]["causal_evidence_status"], "POTENTIALLY_AVOIDABLE")
            self.assertEqual(join[0]["potentially_avoidable_rebuild_bytes"], 50)
            strategies = strategy_comparison(join, episodes)
            self.assertEqual(next(row for row in strategies if row["strategy"] == "LSTM")["top1_accuracy"], 1.0)


if __name__ == "__main__":
    unittest.main()
