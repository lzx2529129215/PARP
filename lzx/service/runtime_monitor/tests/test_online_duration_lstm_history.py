from __future__ import annotations

import datetime as dt
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from runtime_monitor.online_duration_lstm import OnlineDurationLSTMRunner


class OnlineDurationLSTMHistoryTest(unittest.TestCase):
    def test_unmapped_transient_does_not_replace_known_segment(self) -> None:
        runner = OnlineDurationLSTMRunner.__new__(OnlineDurationLSTMRunner)
        started = dt.datetime(2026, 8, 30, 13, 0, 0)
        runner.completed_segments = []
        runner.current_segment = {
            "raw_app": "FIREFOX",
            "mapped_app": "Firefox",
            "start_time": started,
            "last_time": started,
        }

        runner._update_segments(
            {}, started + dt.timedelta(seconds=1), "UNKNOWN", "<UNKNOWN>"
        )
        self.assertEqual(runner.completed_segments, [])
        self.assertEqual(runner.current_segment["mapped_app"], "Firefox")

        runner._update_segments(
            {}, started + dt.timedelta(seconds=2), "THUNDERBIRD", "Thunderbird"
        )
        self.assertEqual(
            [item["mapped_app"] for item in runner.completed_segments], ["Firefox"]
        )
        self.assertEqual(runner.current_segment["mapped_app"], "Thunderbird")


if __name__ == "__main__":
    unittest.main()
