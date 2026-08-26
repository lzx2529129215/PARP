import tempfile
import unittest
from pathlib import Path

from memsched_exp.process_lifecycle import classify_transition, process_identity, survival_summary


class ProcessLifecycleTest(unittest.TestCase):
    def _proc(self, root: Path, pid: int, start_ticks: int, state: str = "S") -> None:
        process = root / str(pid)
        process.mkdir(parents=True)
        remainder = [state] + ["0"] * 49
        remainder[19] = str(start_ticks)
        (process / "stat").write_text(f"{pid} (demo app) " + " ".join(remainder), encoding="utf-8")
        (process / "cgroup").write_text("0::/demo.slice\n", encoding="utf-8")

    def test_pid_start_time_distinguishes_hot_resume_from_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            proc = Path(directory)
            (proc / "sys/kernel/random").mkdir(parents=True)
            (proc / "sys/kernel/random/boot_id").write_text("boot-a", encoding="utf-8")
            self._proc(proc, 123, 100)
            before = process_identity(123, proc)
            after = process_identity(123, proc)
            self.assertEqual(classify_transition(before, after), "hot_resume")
            after["start_ticks"] = 101
            self.assertEqual(classify_transition(before, after), "cold_restart")

    def test_survival_summary(self):
        started = [
            {"pid": 1, "alive": True, "start_ticks": 10, "boot_id": "a"},
            {"pid": 2, "alive": True, "start_ticks": 20, "boot_id": "a"},
        ]
        current = [{"pid": 1, "alive": True, "start_ticks": 10, "boot_id": "a"}]
        result = survival_summary(started, current)
        self.assertEqual(result["background_apps_alive"], 1)
        self.assertEqual(result["cold_restart_count"], 1)


if __name__ == "__main__":
    unittest.main()
