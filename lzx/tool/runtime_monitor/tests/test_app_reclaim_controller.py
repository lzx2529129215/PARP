from __future__ import annotations

import csv
import errno
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.app_memory_activity import AppActivity
from core.app_reclaim_controller import AppReclaimController, ReclaimConfig
from core.x11_event_state import DIRECT_PREDICTION_EVENTS


GIB = 1024 * 1024 * 1024
MIB = 1024 * 1024


def state(*, available: int = GIB, psi: str = "full avg10=0.00 avg60=0.00 avg300=0.00 total=0", oom: int = 0) -> dict[str, object]:
    return {
        "available": available,
        "vmstat": {"pgscan_direct": 0, "pgsteal_direct": 0, "pswpout": 0},
        "psi_full": psi,
        "psi_some": "some avg10=0.00 avg60=0.00 avg300=0.00 total=0",
        "root_memory_events": {"oom": oom, "oom_kill": 0},
    }


def activity(
    app: str = "B", *, foreground: str = "BACKGROUND", running: str = "RUNNING_BACKGROUND",
    ema: float = 0.01, low_windows: int = 3, current: int = 256 * MIB,
    status: str = "OK", path: str | None = None,
) -> AppActivity:
    return AppActivity(
        app, {"A": 1, "B": 2, "C": 3}.get(app, 0),
        path or f"/sys/fs/cgroup/user.slice/u/huawei-test.slice/automation-{app.lower()}.scope",
        foreground, running, 1, 128 * MIB, 64 * MIB, MIB, 0, current,
        0.01, ema, low_windows, status, "", 1,
    )


class ControllerTests(unittest.TestCase):
    def make(self, directory: str, cfg: ReclaimConfig | None = None) -> AppReclaimController:
        return AppReclaimController(
            session_id="s", output_dir=Path(directory), config=cfg or ReclaimConfig(),
            app_ids={"A": 1, "B": 2, "C": 3},
        )

    @staticmethod
    def prediction(**probabilities: float) -> dict[str, object]:
        return {"all_probabilities": [{"app_key": app, "probability": value} for app, value in probabilities.items()]}

    @staticmethod
    def read_rows(directory: str, name: str) -> list[dict[str, str]]:
        with (Path(directory) / "reclaim" / name).open(encoding="utf-8", newline="") as stream:
            return list(csv.DictReader(stream))

    def observe(
        self, controller: AppReclaimController, *, activities: dict[str, AppActivity],
        probabilities: dict[str, float], bind: set[str] | None = None, now: int = 10 * 10**9,
        observed_state: dict[str, object] | None = None,
    ) -> None:
        with patch("core.app_reclaim_controller.system_state", return_value=observed_state or state()), \
             patch("core.app_reclaim_controller.Path.is_dir", return_value=True), \
             patch("core.app_reclaim_controller.Path.is_file", return_value=True):
            controller.observe_prediction(
                prediction_id="p", trigger_event_id="e", trigger_type="direct_app_switch",
                current_app="A", result=self.prediction(**probabilities), activities=activities,
                bind_ready=bind if bind is not None else {"B", "C"}, now_ns=now,
            )

    def one_row(self, cfg: ReclaimConfig, activities: dict[str, AppActivity], probabilities: dict[str, float]) -> dict[str, str]:
        with tempfile.TemporaryDirectory() as directory:
            controller = self.make(directory, cfg)
            self.observe(controller, activities=activities, probabilities=probabilities)
            controller.close()
            return self.read_rows(directory, "app_reclaim_decisions.csv")[0]

    # 1, 2, 3, 4, 5, 6, 7: mandatory candidate gates.
    def test_foreground_app_never_selected(self) -> None:
        row = self.one_row(ReclaimConfig(required_low_probability_batches=1), {"A": activity("A", foreground="FOREGROUND", running="FOREGROUND")}, {"A": 0.01})
        self.assertIn("FOREGROUND_PROTECTED", row["skip_reason"])

    def test_high_probability_app_never_selected(self) -> None:
        row = self.one_row(ReclaimConfig(required_low_probability_batches=1), {"B": activity()}, {"B": 0.9})
        self.assertIn("LOW_PROBABILITY_NOT_STABLE", row["skip_reason"])

    def test_high_referenced_ratio_is_not_selected(self) -> None:
        row = self.one_row(ReclaimConfig(required_low_probability_batches=1), {"B": activity(ema=0.9, low_windows=0)}, {"B": 0.01})
        self.assertIn("ACTIVITY_NOT_LOW_STABLE", row["skip_reason"])

    def test_no_headroom_deficit_skips(self) -> None:
        cfg = ReclaimConfig(required_low_probability_batches=1, target_headroom_bytes=0)
        row = self.one_row(cfg, {"B": activity()}, {"B": 0.01})
        self.assertIn("NO_MEMORY_NEED", row["skip_reason"])

    def test_shadow_creates_would_reclaim_without_write(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = self.make(directory, ReclaimConfig(required_low_probability_batches=1, mode="shadow"))
            with patch("core.app_reclaim_controller.Path.open") as opened:
                self.observe(controller, activities={"B": activity()}, probabilities={"B": 0.01})
            controller.close()
            row = self.read_rows(directory, "app_reclaim_decisions.csv")[0]
            self.assertEqual(row["decision"], "WOULD_RECLAIM")
            self.assertLessEqual(int(row["requested_reclaim_bytes"]), 16 * MIB)
            opened.assert_not_called()

    def test_low_probability_requires_consecutive_batches(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = self.make(directory, ReclaimConfig(required_low_probability_batches=2))
            self.observe(controller, activities={"B": activity()}, probabilities={"B": 0.01})
            self.observe(controller, activities={"B": activity()}, probabilities={"B": 0.01}, now=11 * 10**9)
            controller.close()
            rows = self.read_rows(directory, "app_reclaim_decisions.csv")
            self.assertIn("LOW_PROBABILITY_NOT_STABLE", rows[0]["skip_reason"])
            self.assertEqual(rows[1]["decision"], "WOULD_RECLAIM")

    def test_low_activity_requires_consecutive_windows(self) -> None:
        row = self.one_row(ReclaimConfig(required_low_probability_batches=1, required_low_activity_windows=3), {"B": activity(low_windows=2)}, {"B": 0.01})
        self.assertIn("ACTIVITY_NOT_LOW_STABLE", row["skip_reason"])

    # 8, 9, 10, 11, 12: rate and budget bounds.
    def test_cooldown_is_enforced(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = self.make(directory, ReclaimConfig(required_low_probability_batches=1))
            controller.last_app_reclaim_ns["B"] = 9 * 10**9
            self.observe(controller, activities={"B": activity()}, probabilities={"B": 0.01})
            controller.close()
            self.assertIn("COOLDOWN_OR_RATE_LIMIT", self.read_rows(directory, "app_reclaim_decisions.csv")[0]["skip_reason"])

    def test_one_candidate_only_per_decision_batch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = self.make(directory, ReclaimConfig(required_low_probability_batches=1))
            self.observe(controller, activities={"B": activity("B"), "C": activity("C")}, probabilities={"B": 0.02, "C": 0.01})
            controller.close()
            rows = self.read_rows(directory, "app_reclaim_decisions.csv")
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["candidate_app"], "C")

    def test_reclaim_step_is_upper_bound(self) -> None:
        row = self.one_row(ReclaimConfig(required_low_probability_batches=1, step_bytes=16 * MIB), {"B": activity(current=1024 * MIB)}, {"B": 0.01})
        self.assertLessEqual(int(row["requested_reclaim_bytes"]), 16 * MIB)

    def test_minimum_resident_memory_is_protected(self) -> None:
        row = self.one_row(ReclaimConfig(required_low_probability_batches=1), {"B": activity(current=64 * MIB)}, {"B": 0.01})
        self.assertIn("MINIMUM_RESIDENT_PROTECTED", row["skip_reason"])

    def test_session_budget_is_enforced(self) -> None:
        row = self.one_row(ReclaimConfig(required_low_probability_batches=1, max_per_app_session=0), {"B": activity()}, {"B": 0.01})
        self.assertIn("SESSION_BUDGET_EXHAUSTED", row["skip_reason"])

    # 13, 14, 15: binding and cgroup validation.
    def test_missing_app_bind_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = self.make(directory, ReclaimConfig(required_low_probability_batches=1))
            self.observe(controller, activities={"B": activity()}, probabilities={"B": 0.01}, bind=set())
            controller.close()
            self.assertIn("APP_BIND_INVALID", self.read_rows(directory, "app_reclaim_decisions.csv")[0]["skip_reason"])

    def test_missing_cgroup_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = self.make(directory, ReclaimConfig(required_low_probability_batches=1))
            with patch("core.app_reclaim_controller.system_state", return_value=state()):
                controller.observe_prediction(prediction_id="p", trigger_event_id="e", trigger_type="x", current_app="A", result=self.prediction(B=0.01), activities={"B": activity()}, bind_ready={"B"}, now_ns=10**9)
            controller.close()
            self.assertIn("CGROUP_UNAVAILABLE", self.read_rows(directory, "app_reclaim_decisions.csv")[0]["skip_reason"])

    def test_missing_memory_reclaim_interface_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = self.make(directory, ReclaimConfig(required_low_probability_batches=1))
            with patch("core.app_reclaim_controller.system_state", return_value=state()), \
                 patch("core.app_reclaim_controller.Path.is_dir", return_value=True), \
                 patch("core.app_reclaim_controller.Path.is_file", return_value=False):
                controller.observe_prediction(prediction_id="p", trigger_event_id="e", trigger_type="x", current_app="A", result=self.prediction(B=0.01), activities={"B": activity()}, bind_ready={"B"}, now_ns=10**9)
            controller.close()
            self.assertIn("MEMORY_RECLAIM_UNAVAILABLE", self.read_rows(directory, "app_reclaim_decisions.csv")[0]["skip_reason"])

    # 16, 17, 18: bounded write error auditing.  These call _apply directly
    # with a mocked safe target; they never touch the real cgroup filesystem.
    def _apply_error(self, open_result: object) -> dict[str, str]:
        with tempfile.TemporaryDirectory() as directory:
            controller = self.make(directory, ReclaimConfig(mode="apply-bounded", preflight_ready=True))
            with patch.object(controller, "_safe_target", return_value=True), \
                 patch("core.app_reclaim_controller.Path.open", open_result), \
                 patch("core.app_reclaim_controller.system_state", return_value=state()):
                controller._apply("d", 10**9, "B", activity(), 16 * MIB, state())
                controller.close()
            return self.read_rows(directory, "memory_reclaim_attempts.csv")[0]

    def test_partial_write_is_recorded(self) -> None:
        class Partial(io.StringIO):
            def write(self, value: str) -> int:
                super().write(value)
                return 1
        row = self._apply_error(lambda *args, **kwargs: Partial())
        self.assertEqual(row["errno"], "PARTIAL_WRITE")
        self.assertEqual(row["write_success"], "false")

    def test_eagain_is_recorded_without_retry(self) -> None:
        row = self._apply_error(lambda *args, **kwargs: (_ for _ in ()).throw(OSError(errno.EAGAIN, "again")))
        self.assertEqual(row["errno"], "EAGAIN")
        self.assertEqual(row["write_success"], "false")

    def test_permission_failure_is_recorded_without_retry(self) -> None:
        row = self._apply_error(lambda *args, **kwargs: (_ for _ in ()).throw(OSError(errno.EACCES, "denied")))
        self.assertEqual(row["errno"], "EACCES")

    # 19, 20, 21, 22: safety and mode boundaries.
    def test_psi_abort_prevents_candidate_selection(self) -> None:
        row = self.one_row(
            ReclaimConfig(required_low_probability_batches=1), {"B": activity()}, {"B": 0.01},
        )
        self.assertIn(row["decision"], {"WOULD_RECLAIM", "SKIP"})
        with tempfile.TemporaryDirectory() as directory:
            controller = self.make(directory, ReclaimConfig(required_low_probability_batches=1))
            self.observe(controller, activities={"B": activity()}, probabilities={"B": 0.01}, observed_state=state(psi="full avg10=0.20 avg60=0 avg300=0 total=0"))
            controller.close()
            self.assertIn("MEMORY_PSI_FULL_ABORT_THRESHOLD", self.read_rows(directory, "app_reclaim_decisions.csv")[0]["skip_reason"])

    def test_monitor_has_controller_exception_boundary(self) -> None:
        source = (ROOT / "monitor.py").read_text(encoding="utf-8")
        self.assertIn("Test4 reclaim controller event handling failed", source)

    def test_apply_requires_preflight_ready(self) -> None:
        row = self.one_row(ReclaimConfig(mode="apply-bounded", preflight_ready=False, required_low_probability_batches=1), {"B": activity()}, {"B": 0.01})
        self.assertEqual(row["decision"], "SKIP")
        self.assertIn("APPLY_PREFLIGHT_NOT_READY", row["skip_reason"])

    # 23, 24, 25: input/trigger contract guards.
    def test_validation_conversion_coverage_is_contiguous_and_preserves_dwell(self) -> None:
        import json
        coverage = json.loads((ROOT.parent / "configs/automation/test4_validation_sequence_108_4_60.coverage.json").read_text(encoding="utf-8"))
        self.assertEqual(coverage["status"], "PASS")
        self.assertEqual(coverage["sequence_order"], ["Firefox", "Telegram", "Firefox", "Telegram", "Firefox", "Telegram", "Firefox", "Telegram", "Firefox", "Telegram"])
        self.assertEqual(coverage["source_dwell_s"], [1, 5, 1, 5, 1, 3, 1, 2, 1])

    def test_process_events_do_not_directly_trigger_prediction(self) -> None:
        self.assertNotIn("PROCESS_START", DIRECT_PREDICTION_EVENTS)
        self.assertNotIn("PROCESS_EXIT", DIRECT_PREDICTION_EVENTS)

    def test_minimize_restore_do_not_directly_trigger_prediction(self) -> None:
        self.assertNotIn("APP_MINIMIZE", DIRECT_PREDICTION_EVENTS)
        self.assertNotIn("APP_RESTORE", DIRECT_PREDICTION_EVENTS)
        self.assertEqual(DIRECT_PREDICTION_EVENTS, {"APP_OPEN", "APP_SWITCH"})


if __name__ == "__main__":
    unittest.main()
