import tempfile
import unittest
from pathlib import Path

from memsched_exp.report import discover_runs, row_for_run


class ReportTest(unittest.TestCase):
    def test_invalid_cgroup_and_extended_artifacts_are_aggregated(self):
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory)
            (run / "metadata.json").write_text('{"name":"demo"}', encoding="utf-8")
            (run / "summary.json").write_text(
                '{"elapsed_s":1,"system":{},"cgroup":{"valid":false,"invalid_reasons":["gone"]}}',
                encoding="utf-8",
            )
            (run / "frames-summary.json").write_text(
                '{"average_fps":58,"fps_per_second_stddev":2,"jank_ratio":0.1}',
                encoding="utf-8",
            )
            (run / "workload-summary.json").write_text(
                '{"background_apps_alive":3,"maximum_cached_apps_without_loss":3}',
                encoding="utf-8",
            )
            row = row_for_run(run)
        self.assertFalse(row["measurement_valid"])
        self.assertIn("gone", row["invalid_reasons"])
        self.assertEqual(row["average_fps"], 58)
        self.assertEqual(row["maximum_cached_apps_without_loss"], 3)

    def test_nested_system_and_cgroup_are_one_logical_run(self):
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory) / "qq-cold-r01"
            (run / "system").mkdir(parents=True)
            (run / "cgroup").mkdir()
            (run / "system" / "metadata.json").write_text('{"name":"qq"}', encoding="utf-8")
            (run / "system" / "summary.json").write_text(
                '{"elapsed_s":1,"system":{"page_refault_count":10}}', encoding="utf-8"
            )
            (run / "cgroup" / "metadata.json").write_text('{"name":"qq-cgroup"}', encoding="utf-8")
            (run / "cgroup" / "summary.json").write_text(
                '{"elapsed_s":1,"system":{},"cgroup":{"valid":true,"page_refault_count":2}}', encoding="utf-8"
            )
            runs = discover_runs(Path(directory))
            row = row_for_run(runs[0])
        self.assertEqual(runs, [run])
        self.assertEqual(row["name"], "qq")
        self.assertEqual(row["page_refault_count"], 10)
        self.assertEqual(row["foreground_page_refault_count"], 2)

    def test_appflow_requires_cold_cache_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory)
            (run / "metadata.json").write_text('{"name":"appflow","scenario":"appflow"}', encoding="utf-8")
            (run / "summary.json").write_text('{"elapsed_s":1,"system":{}}', encoding="utf-8")
            row = row_for_run(run)
        self.assertFalse(row["measurement_valid"])
        self.assertIn("cold-cache evidence is missing", row["invalid_reasons"])


if __name__ == "__main__":
    unittest.main()
