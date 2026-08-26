import tempfile
import unittest
from pathlib import Path

from memsched_exp.frames import analyze_frame_times


class FramesTest(unittest.TestCase):
    def test_fps_stddev_and_jank(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "frames.csv"
            path.write_text(
                "timestamp_ns,duration_ms\n"
                "0,10\n"
                "500000000,20\n"
                "1000000000,10\n",
                encoding="utf-8",
            )
            result = analyze_frame_times(path, budget_ms=16.7)
        self.assertEqual(result["frames"], 3)
        self.assertEqual(result["jank_count"], 1)
        self.assertAlmostEqual(result["jank_ratio"], 1 / 3)
        self.assertAlmostEqual(result["fps_per_second_mean"], 1.5)
        self.assertAlmostEqual(result["fps_per_second_stddev"], 0.5)

    def test_zero_frame_seconds_are_included(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "frames.csv"
            path.write_text(
                "timestamp_ns,duration_ms\n"
                "0,10\n"
                "2000000000,10\n",
                encoding="utf-8",
            )
            result = analyze_frame_times(path)
        self.assertEqual(result["fps_bucket_counts"], [1, 0, 1])
        self.assertEqual(result["zero_frame_seconds"], 1)
        self.assertGreater(result["fps_per_second_stddev"], 0)


if __name__ == "__main__":
    unittest.main()
