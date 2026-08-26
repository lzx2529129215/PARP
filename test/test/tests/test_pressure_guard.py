import tempfile
import unittest
from pathlib import Path

from memsched_exp.pressure_guard import memory_environment


class PressureGuardTest(unittest.TestCase):
    def test_enclosing_cgroup_limit_is_effective_memory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            proc = root / "proc"
            cgroup = root / "cgroup"
            (proc / "self").mkdir(parents=True)
            (proc / "pressure").mkdir()
            (cgroup / "slice" / "test").mkdir(parents=True)
            (proc / "meminfo").write_text(
                "MemTotal: 33554432 kB\nMemAvailable: 16777216 kB\n", encoding="utf-8"
            )
            (proc / "pressure" / "memory").write_text(
                "some avg10=0.00 avg60=0.00 avg300=0.00 total=0\n", encoding="utf-8"
            )
            (proc / "self" / "cgroup").write_text("0::/slice/test\n", encoding="utf-8")
            (cgroup / "memory.max").write_text("max\n", encoding="utf-8")
            (cgroup / "slice" / "memory.max").write_text(str(8 * 1024**3), encoding="utf-8")
            (cgroup / "slice" / "test" / "memory.max").write_text("max\n", encoding="utf-8")
            result = memory_environment(proc, cgroup)
        self.assertEqual(result["effective_memory_gib"], 8)
        self.assertEqual(len(result["cgroup_memory_limits"]), 1)


if __name__ == "__main__":
    unittest.main()
