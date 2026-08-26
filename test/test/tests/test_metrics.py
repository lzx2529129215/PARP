import unittest

from memsched_exp.metrics import memory_metrics, summarize


class MetricsTest(unittest.TestCase):
    def test_vmstat_metrics_do_not_double_count_aggregate(self):
        before = {
            "workingset_refault_anon": 10,
            "workingset_refault_file": 20,
            "pgsteal": 1000,
            "pgsteal_anon": 400,
            "pgsteal_file": 600,
            "pgscan_direct": 10,
            "pgscan_direct_anon": 4,
            "pgscan_direct_file": 6,
            "pgscan_kswapd": 90,
            "pgscan_kswapd_anon": 40,
            "pgscan_kswapd_file": 50,
            "allocstall_normal": 2,
        }
        after = {
            "workingset_refault_anon": 14,
            "workingset_refault_file": 26,
            "pgsteal": 1100,
            "pgsteal_anon": 440,
            "pgsteal_file": 660,
            "pgscan_direct": 30,
            "pgscan_direct_anon": 14,
            "pgscan_direct_file": 16,
            "pgscan_kswapd": 170,
            "pgscan_kswapd_anon": 80,
            "pgscan_kswapd_file": 90,
            "allocstall_normal": 5,
        }
        result = memory_metrics(before, after)
        self.assertEqual(result["page_refault_count"], 10)
        self.assertEqual(result["evicted_pages"], 100)
        self.assertAlmostEqual(result["page_refault_ratio"], 0.1)
        self.assertEqual(result["direct_reclaim_allocstall_count"], 3)
        self.assertAlmostEqual(result["direct_reclaim_page_ratio"], 0.2)

    def test_cgroup_cpu_io_and_oom(self):
        before = {
            "monotonic_ns": 1_000_000_000,
            "vmstat": {},
            "cgroup": {"memory_stat": {}, "memory_events": {"oom_kill": 0}, "cpu_stat": {"usage_usec": 0}, "io_stat": {"rbytes": 0}},
        }
        after = {
            "monotonic_ns": 3_000_000_000,
            "vmstat": {},
            "cgroup": {"memory_stat": {}, "memory_events": {"oom_kill": 1}, "cpu_stat": {"usage_usec": 1_000_000}, "io_stat": {"rbytes": 2 * 1024 * 1024}},
        }
        result = summarize(before, after, cpu_count=4)["cgroup"]
        self.assertAlmostEqual(result["cpu_one_core_percent"], 50.0)
        self.assertAlmostEqual(result["cpu_machine_percent"], 12.5)
        self.assertAlmostEqual(result["io_read_throughput_mb_s"], 1.0)
        self.assertEqual(result["oom_kill_count"], 1)

    def test_missing_cgroup_endpoint_is_invalid_not_zero(self):
        before = {
            "monotonic_ns": 1_000_000_000,
            "vmstat": {},
            "cgroup": {
                "path": "/sys/fs/cgroup/test",
                "identity": {"device": 1, "inode": 2},
                "read_status": {name: {"ok": True, "error": None} for name in ("memory_stat", "memory_events", "cpu_stat", "io_stat")},
            },
        }
        after = {
            "monotonic_ns": 2_000_000_000,
            "vmstat": {},
            "cgroup": {
                "path": "/sys/fs/cgroup/test",
                "identity": None,
                "read_status": {name: {"ok": False, "error": "missing"} for name in ("memory_stat", "memory_events", "cpu_stat", "io_stat")},
            },
        }
        result = summarize(before, after)["cgroup"]
        self.assertFalse(result["valid"])
        self.assertNotIn("page_refault_count", result)
        self.assertTrue(result["invalid_reasons"])

    def test_system_cpu_percent(self):
        before = {
            "monotonic_ns": 1_000_000_000,
            "vmstat": {},
            "cpu_stat": {"user": 100, "system": 50, "idle": 850, "iowait": 0},
        }
        after = {
            "monotonic_ns": 2_000_000_000,
            "vmstat": {},
            "cpu_stat": {"user": 140, "system": 60, "idle": 900, "iowait": 0},
        }
        result = summarize(before, after, cpu_count=4)["system"]
        self.assertAlmostEqual(result["cpu_machine_percent"], 50.0)
        self.assertAlmostEqual(result["cpu_one_core_percent"], 200.0)


if __name__ == "__main__":
    unittest.main()
