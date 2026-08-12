import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from phase210 import build_qq_positive_support as builder
from phase210 import positive_support_audit as support


class Phase210PositiveSupportContracts(unittest.TestCase):
    def test_scenario_is_bounded_repeated_and_no_send(self):
        scenario = builder.scenario()
        waits = sum(action.get("seconds", 0) for action in scenario["actions"])
        starts = [action for action in scenario["actions"] if action.get("event_type") == "OP_START"]
        self.assertEqual(len(starts), 18)
        self.assertGreaterEqual(waits, 8 * 60)
        self.assertLessEqual(waits, 12 * 60)
        self.assertIn("send_message", scenario["forbidden"])
        self.assertFalse(any(action.get("type") == "send_message" for action in scenario["actions"]))

    def test_all_window_actions_use_strict_profile_match(self):
        window_actions = [action for action in builder.scenario()["actions"] if action.get("class") == builder.QQ_CLASS]
        self.assertTrue(window_actions)
        self.assertTrue(all(action.get("strict_window_match") for action in window_actions))
        self.assertTrue(all(action.get("pid_cmdline_contains") == "${QQ_PROFILE}/chromium" for action in window_actions))

    def test_only_nonsecret_fixed_search_text_is_typed(self):
        typed = [action["text"] for action in builder.scenario()["actions"] if action.get("type") == "type"]
        self.assertEqual(set(typed), {"PARP_PHASE210_PILOT"})

    def test_builder_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / (builder.SESSION + ".json")
            path.write_text(json.dumps(builder.scenario()) + "\n")
            loaded = json.loads(path.read_text())
        self.assertEqual(loaded["pilot"], "POSITIVE_SUPPORT_ONLY")

    def test_audit_excludes_censored_60_second_tail(self):
        decisions = []
        for index in range(8):
            positive = index == 0
            decisions.append({
                "window_start_ns": index * 10_000_000_000,
                "candidates": [
                    {"future": {"60": 10 if positive else None}},
                    {"future": {"60": None}},
                ],
            })
        conversion = {"decoded_file_rows": 123, "decision_count": 8}
        with mock.patch.object(support, "build_qq_decisions", return_value=(decisions, conversion)):
            result = support.audit("unused", builder.SESSION)
        self.assertEqual(result["complete_horizon_decisions"], 2)
        self.assertEqual(result["positive_candidates_60s"], 1)
        self.assertEqual(result["pairwise_evaluable_decisions_60s"], 1)
        self.assertTrue(result["censored_tail_excluded"])

    def test_gate_requires_both_support_thresholds(self):
        candidates = [{"future": {"60": 10}} for _ in range(20)] + [{"future": {"60": None}}]
        decisions = [{"window_start_ns": index * 10_000_000_000, "candidates": candidates} for index in range(17)]
        conversion = {"decoded_file_rows": 1000, "decision_count": len(decisions)}
        with mock.patch.object(support, "build_qq_decisions", return_value=(decisions, conversion)):
            result = support.audit("unused", builder.SESSION)
        self.assertGreaterEqual(result["positive_candidates_60s"], 20)
        self.assertGreaterEqual(result["pairwise_evaluable_decisions_60s"], 10)
        self.assertTrue(result["passed"])

    def test_root_pilot_gate_uses_wall_clock_not_loop_count(self):
        root = Path(__file__).resolve().parents[1] / "qq_collection_root.sh"
        script = root.read_text()
        self.assertIn("SECONDS - pilot_gate_start_seconds", script)
        self.assertNotIn("$elapsed -eq 300", script)


if __name__ == "__main__":
    unittest.main()
