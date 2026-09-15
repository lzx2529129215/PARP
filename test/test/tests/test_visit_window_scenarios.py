"""Contract tests for real-time labels, scenario intent and migrated ownership."""
import importlib.util
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

PATH = Path(__file__).resolve().parents[1] / "visit_window_scenarios.py"
spec = importlib.util.spec_from_file_location("visit_scenario_tests_runner", PATH)
runner = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = runner
spec.loader.exec_module(runner)


class ScenarioTests(unittest.TestCase):
    def setUp(self):
        self.config = json.loads(runner.CONFIG.read_text())

    def test_five_scenarios_have_real_time_and_complete_followup(self):
        self.assertEqual(len(self.config["scenarios"]), 5)
        for name in self.config["scenarios"]:
            plan = runner.make_plan(name, self.config, 1)
            self.assertEqual(plan["time_scale"], 1)
            self.assertGreaterEqual(plan["followup_s"], 180)
            self.assertEqual(set(a["app"] for a in plan["warmup"]), set(plan["apps"]))
            self.assertTrue(all(0 <= a["at_s"] < plan["score_s"] for a in plan["actions"]))
            self.assertEqual(len(plan["apps"]) - 1, 5)
            self.assertEqual(plan["kernel_sink"], "off")

    def test_medium_returns_and_active_set_change(self):
        medium = runner.score_actions("v3_medium_return", self.config, 1)
        self.assertEqual([a["at_s"] for a in medium if a["app"] == "THUNDERBIRD"], [90, 330])
        shift = runner.score_actions("v5_active_set_shift", self.config, 1)
        self.assertEqual({a["app"] for a in shift if a["at_s"] < 180}, {"FIREFOX", "LIBREOFFICE"})
        self.assertEqual({a["app"] for a in shift if a["at_s"] >= 180}, {"GIMP", "EVINCE"})


class LabelTests(unittest.TestCase):
    vocab = {"<PAD>": 0, "A": 1, "B": 2, "C": 3}

    def evaluate(self, entries, observed_until=280):
        rows = [{"app_id": i, "app": app, "p_visit_30s": .01, "p_visit_180s": .1,
                 "thermal_state": "foreground" if app == "A" else "cold"}
                for app, i in self.vocab.items() if not app.startswith("<")]
        call = {"monotonic_s": 100, "result": {"status": "success", "all_probabilities": rows,
                "hot_apps": [], "cold_apps": ["B", "C"], "predict_latency_ms": 1.}}
        return runner.evaluate_calls([call], entries, observed_until, 100, 101, self.vocab)

    def test_strict_future_and_inclusive_right_boundaries(self):
        _, a = self.evaluate([(100, "A"), (130, "B"), (280, "C")])
        np.testing.assert_array_equal(a["labels"][0], [[0, 0], [0, 0], [1, 1], [0, 1]])
        np.testing.assert_array_equal(a["valid"][0], [[0, 0], [1, 1], [1, 1], [1, 1]])
        np.testing.assert_array_equal(a["eligible"][0], [False, False, True, True])

    def test_return_to_current_counts_but_continuing_dwell_does_not(self):
        _, a = self.evaluate([(100, "A"), (110, "B"), (120, "A"), (140, "A")])
        np.testing.assert_array_equal(a["labels"][0, 1], [1, 1])
        _, a = self.evaluate([(100, "A")])
        self.assertEqual(a["labels"].sum(), 0)

    def test_partial_observation_keeps_positives_masks_unknown_negatives(self):
        _, a = self.evaluate([(130.001, "B"), (281, "C")], observed_until=150)
        np.testing.assert_array_equal(a["labels"][0, 2], [0, 1])
        np.testing.assert_array_equal(a["valid"][0, 2], [1, 1])
        np.testing.assert_array_equal(a["valid"][0, 3], [1, 0])
        self.assertEqual(a["labels"][0, 3].sum(), 0)  # no reading past observation boundary

    def test_cold_rate_denominator_is_app_prediction_pairs(self):
        report, _ = self.evaluate([(110, "B")])
        self.assertEqual(report["thermal"]["cold_actual_visit_rate"], .5)
        self.assertEqual(report["cold_count_mean"], 2)
        self.assertIsNone(report["thermal"]["hot_precision"])


class OwnershipTests(unittest.TestCase):
    def test_recovery_dialog_is_not_a_content_window(self):
        main = SimpleNamespace(net_wm_name="[image-test] imported – GIMP")
        dialog = SimpleNamespace(net_wm_name="Image Recovery")
        found = runner.content_windows("GIMP", {"GIMP": [dialog, main]}, Path("/private/round"))
        self.assertEqual(found, [main])

    def test_service_migration_does_not_lose_private_app(self):
        windows = runner.Windows.__new__(runner.Windows)
        windows.units, windows.run_dir = {"FIREFOX": "original.scope"}, Path("/private/round")
        with patch.object(Path, "read_text", return_value="0::/parp/firefox/new.scope\n"), \
                patch.object(runner, "owned_root_app", return_value="FIREFOX"):
            self.assertEqual(windows.app_for_pid(101), "FIREFOX")
        with patch.object(Path, "read_text", return_value="0::/parp/firefox/new.scope\n"), \
                patch.object(runner, "owned_root_app", return_value=""):
            self.assertEqual(windows.app_for_pid(202), "")

    def test_cleanup_does_not_kill_reused_pid(self):
        calls = {101: 0, 102: 0}
        def identity(pid):
            calls[pid] += 1
            return (1, 100 if calls[pid] == 1 or pid == 101 else 200)
        with patch.object(Path, "iterdir", return_value=iter([Path("/proc/101"), Path("/proc/102")])), \
                patch.object(runner, "process_identity", side_effect=identity), \
                patch.object(runner, "owned_root_app", return_value="FIREFOX"), \
                patch.object(runner.os, "kill") as kill, patch.object(runner.time, "sleep"):
            runner.cleanup_owned(Path("/private/round"))
        self.assertEqual([c.args[0] for c in kill.call_args_list], [101, 101])


if __name__ == "__main__":
    unittest.main()
