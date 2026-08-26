import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from memsched_exp.protocol import read_marker, wait_for_markers, write_marker


class CliProtocolTest(unittest.TestCase):
    def test_before_and_after_snapshots_enclose_workload_markers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            proc = root / "proc"
            (proc / "pressure").mkdir(parents=True)
            (proc / "vmstat").write_text("workingset_refault_anon 1\npgsteal 1\n", encoding="utf-8")
            (proc / "meminfo").write_text("MemTotal: 1024 kB\n", encoding="utf-8")
            (proc / "pressure/memory").write_text(
                "some avg10=0.00 avg60=0.00 avg300=0.00 total=0\n", encoding="utf-8"
            )
            (proc / "stat").write_text("cpu 1 0 1 10 0 0 0 0 0 0\n", encoding="utf-8")
            output = root / "run"
            ready = root / "ready.json"
            start = root / "start.json"
            stop = root / "stop.json"
            done = root / "done.json"
            command = [
                sys.executable,
                "-m",
                "memsched_exp.cli",
                "collect",
                "--name",
                "test",
                "--duration",
                "1",
                "--interval",
                "0.01",
                "--proc-root",
                str(proc),
                "--output",
                str(output),
                "--ready-file",
                str(ready),
                "--start-file",
                str(start),
                "--stop-file",
                str(stop),
                "--done-file",
                str(done),
            ]
            process = subprocess.Popen(command, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
            wait_for_markers([ready], 5)
            write_marker(start, "workload_start")
            write_marker(stop, "workload_stop")
            self.assertEqual(process.wait(timeout=5), 0)
            before = json.loads((output / "before.json").read_text(encoding="utf-8"))
            after = json.loads((output / "after.json").read_text(encoding="utf-8"))
            markers = [read_marker(path)["monotonic_ns"] for path in (ready, start, stop, done)]
        self.assertLess(before["monotonic_ns"], markers[0])
        self.assertLessEqual(markers[0], markers[1])
        self.assertLess(markers[1], markers[2])
        self.assertLessEqual(markers[2], after["monotonic_ns"])
        self.assertLessEqual(after["monotonic_ns"], markers[3])


if __name__ == "__main__":
    unittest.main()
