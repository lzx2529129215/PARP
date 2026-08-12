#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("parp_acceptance_lzx", ROOT / "parp-acceptance-lzx.py")
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)
REPORT_SPEC = importlib.util.spec_from_file_location("baseline_report_lzx", ROOT / "baseline-report-lzx.py")  #lzx
assert REPORT_SPEC is not None and REPORT_SPEC.loader is not None  #lzx
REPORT = importlib.util.module_from_spec(REPORT_SPEC)  #lzx
sys.modules[REPORT_SPEC.name] = REPORT  #lzx
REPORT_SPEC.loader.exec_module(REPORT)  #lzx


class AcceptanceConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = json.loads((ROOT / "parp-acceptance-config-lzx.json").read_text(encoding="utf-8"))

    def test_full_acceptance_ratios_and_counts(self) -> None:
        full = self.config["profiles"]["full"]
        self.assertGreaterEqual(full["hotcold_logical_ratio"], 1.50)
        self.assertLessEqual(full["hotcold_logical_ratio"], 2.00)
        self.assertEqual(full["hotcold_repeats"], 10)
        self.assertGreaterEqual(full["peak_steps"], 100)
        self.assertEqual(full["peak_rounds"], 3)
        peak = self.config["peak"]
        self.assertLessEqual(sum(peak["normal_ratio_by_app"].values()), 1.0)
        self.assertGreaterEqual(sum(peak["peak_ratio_by_app"].values()), 1.2)
        self.assertTrue(all(value <= 1.0 for value in peak["peak_ratio_by_app"].values()))

    def test_safety_boundary_is_host_conservative(self) -> None:
        safety = self.config["safety"]
        self.assertLess(safety["memory_high_ratio"], safety["memory_max_ratio"])
        self.assertLess(safety["memory_max_ratio"], 1.0)
        self.assertGreaterEqual(safety["min_memavailable_bytes"], 2 * 1024**3)
        self.assertLessEqual(safety["psi_full_avg10_abort"], 0.20)
        self.assertGreaterEqual(safety["psi_memavailable_guard_bytes"], 4 * 1024**3)
        self.assertLessEqual(safety["trace_buffer_kb_per_cpu"], 4096)
        self.assertGreaterEqual(safety["min_inotify_watch_headroom"], 1024)

    def test_generated_full_scenarios_are_safe_and_counted(self) -> None:
        forbidden = ("drop_caches", "memory.reclaim", "swapoff", "swapon", "sysctl -w")
        with tempfile.TemporaryDirectory() as temporary:
            session = Path(temporary) / "session"
            for suite in ("hotcold", "peak"):
                scenario = MODULE.generate_scenario(
                    self.config, suite=suite, profile="full", round_index=1,
                    seed=20260809, session_dir=session / suite,
                    trace_instance="parp-accept-unit",
                )
                expected = 24 if suite == "hotcold" else 100
                starts = [item for item in scenario["actions"] if item.get("event_type") == f"{suite.upper()}_CASE_START"]
                dones = [item for item in scenario["actions"] if item.get("event_type") == f"{suite.upper()}_CASE_DONE"]
                self.assertEqual(len(starts), expected)
                self.assertEqual(len(dones), expected)
                commands = "\n".join(str(item.get("command", "")) for item in scenario["actions"])
                self.assertFalse(any(token in commands for token in forbidden))
                if suite == "peak":
                    labels = [str(item.get("label", "")) for item in scenario["actions"]]
                    last_prepare = max(index for index, label in enumerate(labels) if label.startswith("FIXTURE_PREPARE_"))
                    first_launch = min(index for index, label in enumerate(labels) if label.startswith("LAUNCH_"))
                    self.assertLess(last_prepare, first_launch)

    def test_all_tracked_test_source_names_have_lzx_suffix(self) -> None:
        for path in ROOT.iterdir():
            if not path.is_file():
                continue
            self.assertTrue(path.stem.endswith("-lzx"), path.name)

    def test_refault_and_oom_deltas_are_reported_without_filling_missing_with_zero(self) -> None:  #lzx
        with tempfile.TemporaryDirectory() as temporary:  #lzx
            root = Path(temporary)  #lzx
            round_dir = root / "round-01"  #lzx
            round_dir.mkdir()  #lzx
            monitor = round_dir / "monitor.csv"  #lzx
            monitor.write_text(  #lzx
                "refault_file,refault_anon,events_oom,events_oom_kill,vm_oom_kill\n"  #lzx
                "10,20,1,0,4\n"  #lzx
                "110,70,3,1,4\n",  #lzx
                encoding="utf-8",  #lzx
            )  #lzx
            self.assertEqual(REPORT.monitor_round_values(root, "refault_file"), [100.0])  #lzx
            self.assertEqual(REPORT.monitor_round_values(root, "refault_anon"), [50.0])  #lzx
            self.assertEqual(REPORT.monitor_round_values(root, "events_oom"), [2.0])  #lzx
            self.assertEqual(REPORT.monitor_round_values(root, "events_oom_kill"), [1.0])  #lzx
            self.assertEqual(REPORT.monitor_round_values(root, "activate_file"), [None])  #lzx
            self.assertIsNone(REPORT.metric_stats_values([None])["mean"])  #lzx

    def test_trace_reclaim_latency_pairs_are_calculated(self) -> None:  #lzx
        with tempfile.TemporaryDirectory() as temporary:  #lzx
            trace = Path(temporary) / "trace.txt"  #lzx
            trace.write_text(  #lzx
                "python3-123 (  123) [000] ..... 10.000000: mm_vmscan_direct_reclaim_begin: order=0\n"  #lzx
                "python3-123 (  123) [000] ..... 10.002000: mm_vmscan_direct_reclaim_end: nr_reclaimed=8\n",  #lzx
                encoding="utf-8",  #lzx
            )  #lzx
            metrics = MODULE.count_trace_events(trace)  #lzx
            self.assertEqual(metrics["direct_reclaim_pairs"], 1)  #lzx
            self.assertEqual(metrics["direct_reclaim_latency_ns_p95"], 2_000_000)  #lzx
            self.assertEqual(metrics["direct_reclaim_pages_reclaimed"], 8)  #lzx

    def test_metric_table_explains_what_each_metric_means(self) -> None:  #lzx
        stats = REPORT.metric_stats_values([10.0, 20.0])  #lzx
        table = "\n".join(REPORT.metric_table(  #lzx
            "测试表", {"workingset_refault_file": stats}, "page_fault_user"  #lzx
        ))  #lzx
        self.assertIn("指标作用（能说明什么）", table)  #lzx
        self.assertIn("用于识别文件页误回收和工作集抖动", table)  #lzx


if __name__ == "__main__":
    unittest.main()
