import tempfile
import unittest
from pathlib import Path

from memsched_exp.workload_summary import summarize_fleet


class WorkloadSummaryTest(unittest.TestCase):
    def test_fleet_capacity_and_java_ratio(self):
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory)
            (run / "caching-capacity.jsonl").write_text(
                '{"launched":1,"alive":1,"new_app_ready":1}\n'
                '{"launched":2,"alive":2,"new_app_ready":1}\n'
                '{"launched":2,"alive":2,"phase":"final"}\n',
                encoding="utf-8",
            )
            for index in range(2):
                (run / f"app-{index}.jsonl").write_text(
                    '{"event":"hot_launch","latency_ms":5,"object_reaccess_ratio":0.5,"java_heap_used_bytes":50}\n',
                    encoding="utf-8",
                )
            (run / "app-rss.jsonl").write_text(
                '{"app_index":0,"rss_bytes":100}\n{"app_index":1,"rss_bytes":100}\n',
                encoding="utf-8",
            )
            result = summarize_fleet(run)
        self.assertEqual(result["maximum_cached_apps_without_loss"], 2)
        self.assertEqual(result["background_app_survival_ratio"], 1)
        self.assertEqual(result["java_heap_ratio"], 0.5)


if __name__ == "__main__":
    unittest.main()
