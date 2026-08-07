import csv
import tempfile
import unittest
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "runtime_monitor/scripts"))
from inspect_pipeline_intermediates import inspect_rows  # noqa: E402


class InspectPipelineTest(unittest.TestCase):
    def test_event_type_column_is_supported(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "markov/workload_markov_online_debugfs_writes.csv"
            path.parent.mkdir(parents=True)
            with path.open("w", encoding="utf-8", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=["event_type", "status"])
                writer.writeheader(); writer.writerow({"event_type": "workload_update", "status": "ok"}); writer.writerow({"event_type": "markov_set", "status": "ok"})
            text, ok = inspect_rows(root)
            self.assertTrue(ok)
            self.assertIn("workload_update", text)

    def test_missing_columns_is_inconclusive(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "markov/workload_markov_online_debugfs_writes.csv"
            path.parent.mkdir(parents=True)
            path.write_text("status\nok\n", encoding="utf-8")
            text, ok = inspect_rows(root)
            self.assertFalse(ok)
            self.assertIn("COLUMN_NOT_FOUND", text)
