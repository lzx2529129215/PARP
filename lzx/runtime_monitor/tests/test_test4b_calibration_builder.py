from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "runtime_monitor" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from build_test4b_calibration import CHUNK, phase_plan  # noqa: E402


class Test4BCalibrationPlanTests(unittest.TestCase):
    def test_file_probe_reaches_the_three_requested_cold_milestones(self) -> None:
        plan = phase_plan("A_FILE")
        self.assertEqual(set(plan), {"FIREFOX"})
        self.assertEqual(plan["FIREFOX"].count("FILE_COLD"), 12)
        self.assertEqual(plan["FIREFOX"].count("FILE_HOT"), 1)
        self.assertEqual(12 * CHUNK, 48 * 1024 * 1024)

    def test_anon_probe_and_mixed_plan_keep_hot_regions_bounded(self) -> None:
        anon = phase_plan("B_ANON")
        self.assertEqual(anon["FIREFOX"].count("ANON_COLD"), 8)
        self.assertEqual(anon["FIREFOX"].count("ANON_HOT"), 1)
        mixed = phase_plan("C_MIXED")
        self.assertEqual(set(mixed), {"FIREFOX", "THUNDERBIRD", "TELEGRAM"})
        for regions in mixed.values():
            self.assertEqual(regions.count("FILE_COLD") * CHUNK, 24 * 1024 * 1024)
            self.assertEqual(regions.count("ANON_COLD") * CHUNK, 8 * 1024 * 1024)
            self.assertEqual((regions.count("FILE_HOT") + regions.count("ANON_HOT")) * CHUNK, 8 * 1024 * 1024)

    def test_reduced_mixed_probe_has_three_app_baseline_and_20_mib_budget(self) -> None:
        baseline = phase_plan("C_BASELINE_STABLE")
        self.assertEqual({app: [] for app in ("FIREFOX", "THUNDERBIRD", "TELEGRAM")}, baseline)
        reduced = phase_plan("C_MIXED_20")
        for regions in reduced.values():
            self.assertEqual(len(regions) * CHUNK, 20 * 1024 * 1024)
            self.assertEqual(regions.count("FILE_COLD") * CHUNK, 8 * 1024 * 1024)


if __name__ == "__main__":
    unittest.main()
