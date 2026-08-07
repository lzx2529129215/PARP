import csv
import tempfile
import unittest
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "runtime_monitor/scripts"))
from build_pipeline_timeline import build_timeline  # noqa: E402


class PipelineTimelineTest(unittest.TestCase):
    def test_timeline_has_origin_and_no_duplicate_write_source(self):
        session = ROOT / "outputs/runtime_monitor/session_unified_pipeline_20260713_115505"
        work = ROOT / "outputs/mglru/unified_pipeline_run_20260713_115505"
        if not session.is_dir():
            self.skipTest("实验原始数据未提供")
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "timeline.csv"
            build_timeline([session, work], output)
            with output.open(encoding="utf-8", newline="") as stream:
                rows = list(csv.DictReader(stream))
            self.assertTrue(rows)
            self.assertTrue({row["event_origin"] for row in rows} <= {"RAW_RUNTIME", "RAW_KERNEL_SNAPSHOT", "DERIVED_AUDIT"})
            self.assertEqual([int(row["event_time_ns"] or 0) for row in rows], sorted(int(row["event_time_ns"] or 0) for row in rows))
            writes = [row for row in rows if row["event_type"] == "APP_PROBABILITY_WRITE"]
            self.assertLessEqual(len(writes), 60)
