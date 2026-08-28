from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from runtime_monitor.core.working_set_predictor import WorkingSetPredictor


class WorkingSetPredictorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.policy = self.root / "policy"
        self.app1 = self.policy / "app1.scope"
        self.app2 = self.policy / "app2.scope"
        self.app1.mkdir(parents=True)
        self.app2.mkdir()
        (self.policy / "memory.tier2_enabled").write_text("1\n", encoding="utf-8")
        (self.policy / "memory.max").write_text(str(8 << 30), encoding="utf-8")
        self._write_memory(self.app1, anon=256 << 20, active_file=128 << 20)
        self._write_memory(self.app2, anon=128 << 20, active_file=128 << 20)
        self.predictor = WorkingSetPredictor(output_dir=self.root / "output")

    def tearDown(self) -> None:
        self.predictor.close()
        self.temp.cleanup()

    @staticmethod
    def _write_memory(path: Path, *, anon: int, active_file: int) -> None:
        inactive_file = 64 << 20
        file_bytes = active_file + inactive_file
        (path / "memory.stat").write_text(
            f"anon {anon}\nfile {file_bytes}\n"
            f"active_file {active_file}\ninactive_file {inactive_file}\n",
            encoding="utf-8",
        )
        (path / "memory.current").write_text(
            str(anon + file_bytes), encoding="utf-8"
        )

    def _mature(self) -> None:
        bindings = {
            self.app1.stat().st_ino: (1, "APP1", self.app1),
            self.app2.stat().st_ino: (2, "APP2", self.app2),
        }
        for generation in range(4):
            self.predictor.observe(bindings, generation + 1)

    def test_resident_predicted_working_set_relaxes_reclaim(self) -> None:
        self._mature()
        result = self.predictor.predict(
            [(1, 32767, 1, 1), (2, 32767, 2, 0)],
            prediction_id="resident",
            timestamp_ns=10,
            foreground_flag=1,
        )
        self.assertTrue(result.valid)
        self.assertEqual(result.policy_domain_id, self.policy.stat().st_ino)
        self.assertEqual(result.predicted_growth_bytes, 0)
        self.assertEqual(result.action_hint, "RELAX")

    def test_closed_large_candidate_strengthens_reclaim(self) -> None:
        self._mature()
        self.predictor.observe(
            {self.app1.stat().st_ino: (1, "APP1", self.app1)}, 8
        )
        result = self.predictor.predict(
            [(1, 32767, 1, 1), (2, 32767, 2, 0)],
            prediction_id="growth",
            timestamp_ns=11,
            foreground_flag=1,
        )
        self.assertTrue(result.valid)
        self.assertGreater(result.predicted_growth_bytes, 0)
        self.assertEqual(result.action_hint, "STRENGTHEN")

    def test_immature_estimate_is_fail_closed(self) -> None:
        self.predictor.observe(
            {self.app1.stat().st_ino: (1, "APP1", self.app1)}, 1
        )
        result = self.predictor.predict(
            [(1, 32767, 1, 1), (2, 32767, 2, 0)],
            prediction_id="immature",
            timestamp_ns=2,
            foreground_flag=1,
        )
        self.assertFalse(result.valid)
        self.assertEqual(result.action_hint, "FALLBACK")


if __name__ == "__main__":
    unittest.main()
