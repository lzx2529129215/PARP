import json
import tempfile
import unittest
from pathlib import Path

from memsched_exp.protocol import ProtocolError, read_marker, wait_for_markers, write_marker


class ProtocolTest(unittest.TestCase):
    def test_atomic_marker_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ready.json"
            written = write_marker(path, "collector_ready", answer=42)
            loaded = read_marker(path)
        self.assertEqual(loaded["event"], "collector_ready")
        self.assertEqual(loaded["answer"], 42)
        self.assertEqual(loaded["monotonic_ns"], written["monotonic_ns"])

    def test_stale_marker_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "start.json"
            path.write_text(json.dumps({"monotonic_ns": 10}), encoding="utf-8")
            with self.assertRaises(ProtocolError):
                wait_for_markers([path], timeout_s=0.1, minimum_monotonic_ns=11)


if __name__ == "__main__":
    unittest.main()
