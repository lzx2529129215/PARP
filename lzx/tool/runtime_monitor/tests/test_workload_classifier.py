from __future__ import annotations

import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from runtime_monitor.core.workload_classifier import (
    INPUT_METRIC_FIELDS,
    classify_metrics,
    classify_session,
)
from core.mglru_markov_debugfs import MGLRUMarkovDebugfsWriter
from core.workload_markov_builder import build_workload_markov


class WorkloadClassifierTests(unittest.TestCase):
    def test_rule_priority_and_workload_ids(self) -> None:
        cases = [
            ({"pgmajfault_delta": 1, "workingset_refault_file_delta": 2}, 4),
            ({"workingset_refault_file_delta": 1}, 3),
            ({"pgfault_delta": 10, "file_delta": 1}, 2),
            ({"pgfault_delta": 10, "anon_delta": 1}, 1),
            ({"memory_current_delta": 4 * 1024 * 1024}, 5),
            ({"file_delta": -(4 * 1024 * 1024)}, 6),
            ({}, 0),
        ]
        for overrides, expected in cases:
            values = {field: 0 for field in INPUT_METRIC_FIELDS}
            values.update(overrides)
            workload_id, _reason = classify_metrics(values)
            self.assertEqual(workload_id, expected)

    def test_session_output_maps_scope_and_tracks_state_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session_dir = Path(tmp) / "session_test"
            model_dir = session_dir / "model"
            model_dir.mkdir(parents=True)
            config_path = Path(tmp) / "scope.json"
            config_path.write_text(
                json.dumps(
                    {
                        "apps": [
                            {
                                "app_key": "FILES",
                                "app_id": 3,
                                "scope_name": "automation-files.scope",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            input_path = model_dir / "cgroup_memory_workload_delta_1s.csv"
            fields = ["session_id", "timestamp", "scope_name", *INPUT_METRIC_FIELDS]
            with input_path.open("w", encoding="utf-8", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=fields)
                writer.writeheader()
                for pgmajfault in (0, 0, 1):
                    writer.writerow(
                        {
                            "session_id": "session_test",
                            "timestamp": "2026-07-09T00:00:00",
                            "scope_name": "automation-files.scope",
                            **{field: 0 for field in INPUT_METRIC_FIELDS},
                            "pgmajfault_delta": pgmajfault,
                        }
                    )

            result = classify_session(session_dir, config_path)

            self.assertEqual(result.final_result, "PASS")
            with result.output_file.open("r", encoding="utf-8", newline="") as f:
                rows = list(csv.DictReader(f))
            self.assertEqual([row["app_key"] for row in rows], ["FILES"] * 3)
            self.assertEqual([row["app_id"] for row in rows], ["3"] * 3)
            self.assertEqual(
                [row["state_changed"] for row in rows],
                ["true", "false", "true"],
            )

    def test_workload_update_writes_command_and_passes_strict(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            debugfs_path = root / "lru_gen_workload_markov"
            debugfs_path.touch()
            writer = MGLRUMarkovDebugfsWriter(
                enabled=True,
                strict=True,
                debugfs_path=debugfs_path,
                session_id="session_test",
                model_dir=root / "model",
                review_dir=root / "review",
                ttl_ms=180000,
            )
            writer.write_workload_update(
                1234,
                3,
                0,
                app_key="FILES",
                workload_name="LOW_ACTIVITY",
            )
            self.assertEqual(
                debugfs_path.read_text(encoding="utf-8"),
                "workload update 1234 3 0\n",
            )
            self.assertEqual(writer.workload_update_write_attempts, 1)
            self.assertEqual(writer.workload_update_write_ok, 1)
            self.assertEqual(writer.final_result(), "PASS")
            writer.close()

            with writer.csv_path.open("r", encoding="utf-8", newline="") as f:
                row = next(csv.DictReader(f))
            self.assertEqual(row["event_type"], "workload_update")
            self.assertEqual(row["app_key"], "FILES")
            self.assertEqual(row["cgroup_id"], "1234")
            self.assertEqual(row["workload_id"], "0")

    def test_workload_update_error_fails_strict(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            debugfs_path = root / "debugfs_directory"
            debugfs_path.mkdir()
            writer = MGLRUMarkovDebugfsWriter(
                enabled=True,
                strict=True,
                debugfs_path=debugfs_path,
                session_id="session_test",
                model_dir=root / "model",
                review_dir=root / "review",
                ttl_ms=180000,
            )
            writer.write_workload_update(1234, 3, 6)
            writer.write_markov_set(
                app_id=3,
                prev_workload_id=0,
                current_workload_id=6,
                entries=[
                    {
                        "next_workload_id": 0,
                        "confidence": 10000,
                        "boost_level": 3,
                    }
                ],
            )
            self.assertEqual(writer.workload_update_write_error, 1)
            self.assertEqual(writer.markov_set_write_error, 1)
            self.assertEqual(writer.final_result(), "FAIL")
            writer.close()

    def test_markov_builder_uses_state_changes_and_builds_topk(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session_dir = Path(tmp) / "session_test"
            model_dir = session_dir / "model"
            model_dir.mkdir(parents=True)
            input_path = model_dir / "cgroup_workload_state_1s.csv"
            fields = [
                "session_id",
                "timestamp",
                "scope_name",
                "app_key",
                "app_id",
                "cgroup_id",
                "workload_id",
                "workload_name",
                "state_changed",
            ]
            sequence = [0, 2, 4, 0, 2, 3]
            with input_path.open("w", encoding="utf-8", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=fields)
                writer.writeheader()
                for index, workload_id in enumerate(sequence):
                    writer.writerow(
                        {
                            "session_id": "session_test",
                            "timestamp": f"2026-07-09T00:00:{index:02d}",
                            "scope_name": "automation-wps.scope",
                            "app_key": "WPS",
                            "app_id": 1,
                            "cgroup_id": 1234,
                            "workload_id": workload_id,
                            "workload_name": "",
                            "state_changed": "true",
                        }
                    )
                writer.writerow(
                    {
                        "session_id": "session_test",
                        "timestamp": "2026-07-09T00:00:07",
                        "scope_name": "automation-wps.scope",
                        "app_key": "WPS",
                        "app_id": 1,
                        "cgroup_id": 1234,
                        "workload_id": 6,
                        "workload_name": "",
                        "state_changed": "false",
                    }
                )

            result = build_workload_markov(session_dir)

            self.assertEqual(result.final_result, "PASS")
            self.assertEqual(result.total_state_rows, 7)
            self.assertEqual(result.total_state_changed_rows, 6)
            self.assertEqual(result.total_transition_keys, 3)
            self.assertEqual(result.total_transition_rows, 4)
            with result.output_file.open("r", encoding="utf-8", newline="") as f:
                rows = list(csv.DictReader(f))
            key_rows = [
                row
                for row in rows
                if row["prev_workload_id"] == "0"
                and row["current_workload_id"] == "2"
            ]
            self.assertEqual(
                [(row["next_workload_id"], row["confidence"]) for row in key_rows],
                [("3", "5000"), ("4", "5000")],
            )
            self.assertEqual([row["boost_level"] for row in key_rows], ["2", "2"])

    def test_markov_set_writes_topk_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            debugfs_path = root / "lru_gen_workload_markov"
            debugfs_path.touch()
            writer = MGLRUMarkovDebugfsWriter(
                enabled=True,
                strict=True,
                debugfs_path=debugfs_path,
                session_id="session_test",
                model_dir=root / "model",
                review_dir=root / "review",
                ttl_ms=180000,
            )
            writer.write_markov_set(
                app_id=1,
                prev_workload_id=0,
                current_workload_id=2,
                entries=[
                    {
                        "next_workload_id": 3,
                        "confidence": 7500,
                        "boost_level": 2,
                    },
                    {
                        "next_workload_id": 4,
                        "confidence": 2500,
                        "boost_level": 1,
                    },
                ],
                app_key="WPS",
            )
            self.assertEqual(
                debugfs_path.read_text(encoding="utf-8"),
                "markov set 1 0 2 3 7500 2 4 2500 1\n",
            )
            self.assertEqual(writer.markov_set_write_attempts, 1)
            self.assertEqual(writer.markov_set_write_ok, 1)
            self.assertEqual(writer.final_result(), "PASS")
            writer.close()

            with writer.csv_path.open("r", encoding="utf-8", newline="") as f:
                row = next(csv.DictReader(f))
            self.assertEqual(row["event_type"], "markov_set")
            self.assertEqual(row["next_workload_ids"], "3|4")
            self.assertEqual(row["confidences"], "7500|2500")
            self.assertEqual(row["boost_levels"], "2|1")


if __name__ == "__main__":
    unittest.main()
