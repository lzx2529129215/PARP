from __future__ import annotations

import csv
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import mock_open, patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.app_memory_activity import AppActivity
from core.test4b_ballast import BallastStatus
from core.test4b_reclaim_controller import MIB, Test4BReclaimConfig, Test4BReclaimController


def system(available: int = 1024 * MIB) -> dict[str, object]:
    return {"available": available, "psi_some": "some avg10=0.00", "psi_full": "full avg10=0.00", "vmstat": {}, "root_memory_events": {"oom": 0, "oom_kill": 0}}


def activity(app: str) -> AppActivity:
    return AppActivity(app, 1, f"/sys/fs/cgroup/u/test4b-experiment.slice/automation-{app.lower()}.scope", "BACKGROUND", "RUNNING_BACKGROUND", 1, 64*MIB, 32*MIB, 64*MIB, 0, 128*MIB, 1.0, 1.0, 0, "OK", "", 1)


def ballast(app: str = "B", state: str = "BACKGROUND_IDLE", allocated: bool = True) -> BallastStatus:
    return BallastStatus(app_key=app, state=state, allocated=allocated, pid=1, background_since_ns=1,
                         expected_cgroup=f"/sys/fs/cgroup/u/test4b-experiment.slice/automation-{app.lower()}.scope",
                         actual_cgroup=f"/sys/fs/cgroup/u/test4b-experiment.slice/automation-{app.lower()}.scope")


class Test4BControllerTests(unittest.TestCase):
    def make(self, directory: str, cfg: Test4BReclaimConfig) -> Test4BReclaimController:
        return Test4BReclaimController(session_id="s", output_dir=Path(directory), config=cfg, app_ids={"A": 1, "B": 2})

    def observe(self, ctrl: Test4BReclaimController, cfg: Test4BReclaimConfig, *, app: str = "B", current: str = "A", state: str = "BACKGROUND_IDLE", allocated: bool = True, probability: float = .01, now: int = 10_000_000_000) -> None:
        result = {"all_probabilities": [{"app_key": "A", "probability": .9}, {"app_key": "B", "probability": probability}]}
        with patch("core.test4b_reclaim_controller.system_state", return_value=system()), \
             patch.object(Test4BReclaimController, "_safe_path", return_value=True), \
             patch("core.test4b_reclaim_controller._stat", return_value={"memcg_current_bytes": 128*MIB}):
            ctrl.observe_prediction(prediction_id="p", trigger_event_id="e", trigger_type="direct_app_switch", current_app=current,
                                    result=result, ballast={app: ballast(app, state, allocated)}, activities={app: activity(app)}, bind_ready={app}, now_ns=now)

    @staticmethod
    def decisions(directory: str) -> list[dict[str, str]]:
        with (Path(directory) / "reclaim/test4b_reclaim_decisions.csv").open(encoding="utf-8", newline="") as stream:
            return list(csv.DictReader(stream))

    def test_foreground_never_eligible(self) -> None:
        cfg = Test4BReclaimConfig(required_low_probability_batches=1)
        with tempfile.TemporaryDirectory() as directory:
            ctrl = self.make(directory, cfg); self.observe(ctrl, cfg, app="B", current="B"); ctrl.close()
            self.assertIn("FOREGROUND_PROTECTED", self.decisions(directory)[0]["skip_reason"])

    def test_background_idle_and_two_low_batches_shadow_without_write(self) -> None:
        cfg = Test4BReclaimConfig(required_low_probability_batches=2, mode="shadow")
        with tempfile.TemporaryDirectory() as directory:
            ctrl = self.make(directory, cfg)
            with patch("pathlib.Path.open") as opened:
                self.observe(ctrl, cfg); self.observe(ctrl, cfg, now=11_000_000_000)
                opened.assert_not_called()
            ctrl.close(); rows = self.decisions(directory)
            self.assertIn("LOW_PROBABILITY_NOT_STABLE", rows[0]["skip_reason"])
            self.assertEqual(rows[1]["decision"], "WOULD_RECLAIM")
            self.assertEqual(rows[1]["apply_label"], "")

    def test_ground_truth_admits_despite_high_observed_referenced_ratio(self) -> None:
        cfg = Test4BReclaimConfig(required_low_probability_batches=1, mode="shadow")
        with tempfile.TemporaryDirectory() as directory:
            ctrl = self.make(directory, cfg); self.observe(ctrl, cfg); ctrl.close()
            row = self.decisions(directory)[0]
            self.assertEqual(row["decision"], "WOULD_RECLAIM")
            self.assertEqual(row["observed_referenced_rss_ema"], "1.0")
            self.assertEqual(row["ground_truth_inactive"], "true")

    def test_non_idle_or_unallocated_ballast_is_rejected(self) -> None:
        cfg = Test4BReclaimConfig(required_low_probability_batches=1)
        with tempfile.TemporaryDirectory() as directory:
            ctrl = self.make(directory, cfg); self.observe(ctrl, cfg, state="FOREGROUND_ACTIVE"); ctrl.close()
            self.assertIn("BALLAST_NOT_BACKGROUND_IDLE", self.decisions(directory)[0]["skip_reason"])

    def test_base_candidate_without_headroom_is_if_needed_not_a_reclaim(self) -> None:
        cfg = Test4BReclaimConfig(required_low_probability_batches=1, mode="shadow", target_headroom_bytes=512*MIB)
        with tempfile.TemporaryDirectory() as directory:
            ctrl = self.make(directory, cfg); self.observe(ctrl, cfg); ctrl.close()
            row = self.decisions(directory)[0]
            self.assertEqual(row["decision"], "WOULD_RECLAIM_IF_NEEDED")
            self.assertEqual(row["candidate_level"], "WOULD_RECLAIM_IF_NEEDED")
            self.assertEqual(row["requested_reclaim_bytes"], "0")
            self.assertEqual(row["skip_reason"], "NO_MEMORY_NEED")

    def test_base_candidate_with_safety_gate_remains_visible(self) -> None:
        cfg = Test4BReclaimConfig(required_low_probability_batches=1, mode="shadow", hard_min_available_bytes=2*1024*MIB)
        with tempfile.TemporaryDirectory() as directory:
            ctrl = self.make(directory, cfg); self.observe(ctrl, cfg); ctrl.close()
            row = self.decisions(directory)[0]
            self.assertEqual(row["decision"], "RECLAIM_CANDIDATE")
            self.assertEqual(row["candidate_level"], "RECLAIM_CANDIDATE")
            self.assertEqual(row["skip_reason"], "MEM_AVAILABLE_BELOW_HARD_FLOOR")

    def test_v3_vocab_name_probability_is_mapped_before_candidate_filtering(self) -> None:
        cfg = Test4BReclaimConfig(required_low_probability_batches=1, mode="shadow", target_headroom_bytes=512*MIB)
        with tempfile.TemporaryDirectory() as directory:
            ctrl = Test4BReclaimController(session_id="s", output_dir=Path(directory), config=cfg,
                                           app_ids={"A": 1, "B": 2}, app_name_to_key={"Beta": "B"})
            with patch("core.test4b_reclaim_controller.system_state", return_value=system()), \
                 patch.object(Test4BReclaimController, "_safe_path", return_value=True), \
                 patch("core.test4b_reclaim_controller._stat", return_value={"memcg_current_bytes": 128*MIB}):
                ctrl.observe_prediction(prediction_id="p", trigger_event_id="e", trigger_type="x", current_app="A",
                                        result={"all_probabilities": [{"app": "Beta", "probability": .01}]},
                                        ballast={"B": ballast()}, activities={"B": activity("B")}, bind_ready={"B"}, now_ns=10_000_000_000)
            ctrl.close()
            self.assertEqual(self.decisions(directory)[0]["decision"], "WOULD_RECLAIM_IF_NEEDED")

    def test_v3_canonical_vocab_name_fallback_is_limited_to_whitelist(self) -> None:
        cfg = Test4BReclaimConfig(required_low_probability_batches=1, mode="shadow", target_headroom_bytes=512*MIB)
        with tempfile.TemporaryDirectory() as directory:
            ctrl = self.make(directory, cfg)
            self.assertEqual(ctrl._probabilities({"all_probabilities": [{"app": "Beta", "probability": .01}]}), {})
            self.assertEqual(ctrl._probabilities({"all_probabilities": [{"app": "B", "probability": .01}]}), {"B": .01})
            ctrl.close()

    def test_apply_is_one_shot_and_labeled(self) -> None:
        cfg = Test4BReclaimConfig(required_low_probability_batches=1, mode="apply-bounded", preflight_ready=True)
        with tempfile.TemporaryDirectory() as directory:
            ctrl = self.make(directory, cfg)
            target_open = mock_open()
            target_open.return_value.__enter__.return_value.write.return_value = len(f"{cfg.step_bytes}\n")
            with patch.object(Test4BReclaimController, "_safe_path", return_value=True), \
                 patch("core.test4b_reclaim_controller.system_state", return_value=system()), \
                 patch("core.test4b_reclaim_controller._stat", return_value={"memcg_current_bytes": 128*MIB}), \
                 patch.object(Test4BReclaimController, "_sample"), \
                 patch("core.test4b_reclaim_controller.Path.open", target_open) as opened:
                ctrl.observe_prediction(prediction_id="p", trigger_event_id="e", trigger_type="x", current_app="A",
                                        result={"all_probabilities": [{"app_key":"A","probability":.9},{"app_key":"B","probability":.01}]},
                                        ballast={"B": ballast()}, activities={"B": activity("B")}, bind_ready={"B"}, now_ns=10_000_000_000)
                ctrl.observe_prediction(prediction_id="p2", trigger_event_id="e2", trigger_type="x", current_app="A",
                                        result={"all_probabilities": [{"app_key":"A","probability":.9},{"app_key":"B","probability":.01}]},
                                        ballast={"B": ballast()}, activities={"B": activity("B")}, bind_ready={"B"}, now_ns=30_000_000_000)
                self.assertEqual(opened.call_count, 1)
            ctrl.close(); rows = self.decisions(directory)
            self.assertEqual(rows[0]["apply_label"], "SYNTHETIC_GROUND_TRUTH_APPLY")
            self.assertIn("SESSION_BUDGET_EXHAUSTED", rows[1]["skip_reason"])


if __name__ == "__main__":
    unittest.main()
