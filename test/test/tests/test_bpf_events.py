import tempfile
import unittest
from pathlib import Path

from memsched_exp.bpf_events import parse_events


class BpfEventsTest(unittest.TestCase):
    def test_parse(self):
        content = "\n".join(
            [
                '{"type":"collector_start","ts_ns":1}',
                '{"type":"direct_reclaim_begin","comm":"qq","tid":7}',
                '{"type":"direct_reclaim_end","comm":"qq","tid":7,"duration_ns":2500000}',
                '{"type":"kswapd_wake","comm":"kswapd0"}',
                '{"type":"oom_mark_victim","comm":"worker"}',
                '{"type":"collector_stop","ts_ns":2}',
            ]
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            path.write_text(content, encoding="utf-8")
            result = parse_events(path)
        self.assertEqual(result["direct_reclaim_count"], 1)
        self.assertEqual(result["direct_reclaim_total_duration_ms"], 2.5)
        self.assertEqual(result["oom_mark_victim_count"], 1)
        self.assertTrue(result["valid"])

    def test_unpaired_reclaim_is_invalid(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            path.write_text(
                '{"type":"collector_start","ts_ns":1}\n'
                '{"type":"direct_reclaim_begin","ts_ns":2,"tid":7}\n'
                '{"type":"collector_stop","ts_ns":3}\n',
                encoding="utf-8",
            )
            result = parse_events(path)
        self.assertFalse(result["valid"])
        self.assertIsNone(result["direct_reclaim_count"])

    def test_equal_counts_with_different_tids_are_invalid(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            path.write_text(
                '{"type":"collector_start","ts_ns":1}\n'
                '{"type":"direct_reclaim_begin","ts_ns":2,"tid":7}\n'
                '{"type":"direct_reclaim_end","ts_ns":3,"tid":8,"duration_ns":1}\n'
                '{"type":"collector_stop","ts_ns":4}\n',
                encoding="utf-8",
            )
            result = parse_events(path)
        self.assertFalse(result["valid"])
        self.assertTrue(result["pairing_errors"])

    def test_window_boundary_pairs_remain_valid_and_are_clipped(self):
        content = "\n".join(
            [
                '{"type":"collector_start","ts_ns":1}',
                '{"type":"direct_reclaim_begin","ts_ns":150,"tid":7}',
                '{"type":"direct_reclaim_end","ts_ns":250,"tid":7,"duration_ns":100}',
                '{"type":"direct_reclaim_begin","ts_ns":250,"tid":8}',
                '{"type":"direct_reclaim_end","ts_ns":350,"tid":8,"duration_ns":100}',
                '{"type":"collector_stop","ts_ns":400}',
            ]
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            path.write_text(content, encoding="utf-8")
            result = parse_events(path, window_start_ns=200, window_stop_ns=300)
        self.assertTrue(result["valid"])
        self.assertEqual(result["direct_reclaim_count"], 1)
        self.assertEqual(result["direct_reclaim_boundary_spanning_count"], 2)
        self.assertAlmostEqual(result["direct_reclaim_total_duration_ms"], 0.0001)


if __name__ == "__main__":
    unittest.main()
