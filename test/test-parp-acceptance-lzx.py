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

    def test_trace_unmatched_reclaim_is_an_explicit_pairing_error(self) -> None:  #lzx
        with tempfile.TemporaryDirectory() as temporary:  #lzx
            trace = Path(temporary) / "trace.txt"  #lzx
            trace.write_text(  #lzx
                "python3-123 (  123) [000] ..... 10.000000: mm_vmscan_direct_reclaim_begin: order=0\n",  #lzx
                encoding="utf-8",  #lzx
            )  #lzx
            metrics = MODULE.count_trace_events(trace)  #lzx
            self.assertEqual(metrics["pairing_error_count"], 1)  #lzx
            self.assertIn("left open", metrics["pairing_errors"][0])  #lzx

    def test_cgroup_endpoint_identity_and_required_reads_are_strict(self) -> None:  #lzx
        required_status = {name: {"ok": True, "error": None} for name in MODULE.REQUIRED_CGROUP_FILES}  #lzx
        memory_stat = {  #lzx
            "pgfault": 1, "pgmajfault": 0, "workingset_refault_file": 0, "workingset_refault_anon": 0,  #lzx
            "pgscan": 0, "pgsteal": 0, "pgscan_direct": 0, "pgsteal_direct": 0, "pgscan_kswapd": 0, "pgsteal_kswapd": 0,  #lzx
        }  #lzx
        endpoint = {  #lzx
            "path": "/sys/fs/cgroup/test", "identity": {"device": 1, "inode": 2},  #lzx
            "read_status": required_status, "memory_stat": memory_stat,  #lzx
            "memory_events": {"oom_kill": 0}, "cpu_stat": {"usage_usec": 1},  #lzx
        }  #lzx
        before = {"cgroup": endpoint}  #lzx
        after = {"cgroup": {**endpoint, "identity": {"device": 1, "inode": 3}}}  #lzx
        valid, reasons = MODULE.cgroup_endpoint_validity(before, after)  #lzx
        self.assertFalse(valid)  #lzx
        self.assertIn("cgroup was recreated during collection", reasons)  #lzx
        missing_cpu = {**endpoint, "read_status": {**required_status, "cpu_stat": {"ok": False, "error": "missing"}}}  #lzx
        valid, reasons = MODULE.cgroup_endpoint_validity({"cgroup": missing_cpu}, {"cgroup": missing_cpu})  #lzx
        self.assertFalse(valid)  #lzx
        self.assertTrue(any("cpu_stat unavailable" in value for value in reasons))  #lzx

    def test_cgroup_cpu_io_and_reclaim_ratios_use_endpoint_deltas(self) -> None:  #lzx
        before_cgroup = {  #lzx
            "pgfault": 10, "pgmajfault": 1, "workingset_refault_file": 10, "workingset_refault_anon": 5,  #lzx
            "workingset_activate_file": 0, "workingset_activate_anon": 0, "workingset_restore_file": 0, "workingset_restore_anon": 0,  #lzx
            "pgscan": 100, "pgsteal": 100, "pgscan_direct": 20, "pgsteal_direct": 10, "pgscan_kswapd": 80, "pgsteal_kswapd": 90,  #lzx
            "cgroup_pswpin": 0, "cgroup_pswpout": 0, "events_high": 0, "events_max": 0, "events_oom": 0, "events_oom_kill": 0,  #lzx
            "cpu_usage_usec": 1_000_000, "cpu_user_usec": 800_000, "cpu_system_usec": 200_000,  #lzx
            "io_rbytes": 0, "io_wbytes": 0, "io_rios": 0, "io_wios": 0,  #lzx
        }  #lzx
        after_cgroup = {key: value for key, value in before_cgroup.items()}  #lzx
        after_cgroup.update({  #lzx
            "workingset_refault_file": 30, "workingset_refault_anon": 15, "pgscan": 300, "pgsteal": 200,  #lzx
            "pgscan_direct": 70, "pgscan_kswapd": 230, "cpu_usage_usec": 3_000_000,  #lzx
            "io_rbytes": 20 * 1024**2, "io_wbytes": 10 * 1024**2,  #lzx
        })  #lzx
        metrics = MODULE.cgroup_endpoint_metrics(  #lzx
            {"monotonic_ns": 0, "cgroup": before_cgroup},  #lzx
            {"monotonic_ns": 2_000_000_000, "cgroup": after_cgroup},  #lzx
        )  #lzx
        self.assertEqual(metrics["cpu_usage_usec_delta"], 2_000_000)  #lzx
        self.assertEqual(metrics["cpu_one_core_percent"], 100.0)  #lzx
        self.assertEqual(metrics["io_read_mib_per_second"], 10.0)  #lzx
        self.assertEqual(metrics["page_refault_ratio_percent"], 30.0)  #lzx
        self.assertEqual(metrics["direct_reclaim_scan_ratio_percent"], 25.0)  #lzx

    def test_launch_latency_pairs_launch_with_verified_window(self) -> None:  #lzx
        with tempfile.TemporaryDirectory() as temporary:  #lzx
            trace = Path(temporary) / "automation_trace.csv"  #lzx
            trace.write_text(  #lzx
                "ts_ns,phase,action,label,status,app_key\n"  #lzx
                "1000000000,start,launch,LAUNCH_WPS,running,WPS\n"  #lzx
                "1250000000,end,launch,LAUNCH_WPS,success,WPS\n"  #lzx
                "1800000000,end,wait_window,WAIT_WPS,success,WPS\n",  #lzx
                encoding="utf-8",  #lzx
            )  #lzx
            metrics = MODULE.launch_latency_metrics(trace)  #lzx
            self.assertEqual(metrics["count"], 1)  #lzx
            self.assertEqual(metrics["mean_ms"], 800.0)  #lzx
            self.assertEqual(metrics["invalid_reasons"], [])  #lzx

    def test_metric_table_explains_what_each_metric_means(self) -> None:  #lzx
        stats = REPORT.metric_stats_values([10.0, 20.0])  #lzx
        table = "\n".join(REPORT.metric_table(  #lzx
            "测试表", {"workingset_refault_file": stats}, "page_fault_user"  #lzx
        ))  #lzx
        self.assertIn("指标作用（能说明什么）", table)  #lzx
        self.assertIn("用于识别文件页误回收和工作集抖动", table)  #lzx


if __name__ == "__main__":
    unittest.main()
