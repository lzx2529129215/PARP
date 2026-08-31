from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

from runtime_monitor.monitor import RuntimeMonitorV0, main, parse_args


ROOT = Path(__file__).resolve().parents[1]


class PageHotsetMonitorIntegrationTests(unittest.TestCase):
    def test_shadow_flag_rejects_non_ebpf_source(self) -> None:
        with redirect_stderr(StringIO()):
            self.assertEqual(main(["--enable-page-hotset-shadow"]), 2)

    def test_page_hotset_event_profile_requires_shadow_mode(self) -> None:
        with redirect_stderr(StringIO()):
            self.assertEqual(main([
                "--file-event-source", "ebpf",
                "--file-event-profile", "page-hotset",
            ]), 2)

    def test_page_access_hook_reaches_observe_only_shadow_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = parse_args([
                "--config", str(ROOT / "config.yaml"),
                "--app-scope-config", "",
                "--target-app", "WPS",
                "--output-dir", tmp,
                "--session-id", "page-hotset-integration",
                "--foreground-backend", "manual",
                "--file-event-source", "ebpf",
                "--enable-page-hotset-shadow",
                "--page-hotset-lateness-ms", "0",
                "--page-hotset-warmup-windows", "40",
                "--page-hotset-history-windows", "100",
            ])
            monitor = RuntimeMonitorV0(args)
            assert monitor.page_hotset_shadow is not None
            start = 50_000_000_000
            monitor.page_hotset_shadow.observe_foreground(start, "WPS")
            monitor._write_file_source_status(
                "READY",
                event={"timestamp_ns": start, "source_instance_id": "test-helper"},
            )
            monitor._write_file_source_status(
                "WORKLOAD_PERF_LOST",
                event={
                    "timestamp_ns": start + 50,
                    "source_instance_id": "test-helper",
                    "perf_lost": 0,
                    "workload_perf_lost": 123,
                },
            )
            with redirect_stdout(StringIO()):
                monitor._handle_ebpf_file_hook({
                    "event": "page_access",
                    "ts_ns": start + 100,
                    "app": "WPS",
                    "process_role": "gui",
                    "pid": 100,
                    "tid": 101,
                    "device_major": 8,
                    "device_minor": 1,
                    "inode": 77,
                    "offset": 4096,
                    "size": 4096,
                    "page_order": 0,
                })
            monitor.page_hotset_shadow.advance(start + 1_000_000_000)
            monitor.page_hotset_shadow.close(start + 1_000_000_000)
            monitor._close_writers()

            snapshot_path = (
                Path(tmp) / "page-hotset-integration" / "model" / "page_snapshots.jsonl"
            )
            rows = [json.loads(line) for line in snapshot_path.read_text().splitlines()]
            snapshot = next(row for row in rows if row["record_type"] == "SNAPSHOT")
            self.assertEqual(snapshot["app_id"], "WPS")
            self.assertEqual(snapshot["page_count"], 1)
            self.assertTrue(snapshot["model_eligible"])
            self.assertIsNone(monitor.parp_bridge)
            self.assertFalse(monitor.mglru_markov_writer.enabled)


if __name__ == "__main__":
    unittest.main()
