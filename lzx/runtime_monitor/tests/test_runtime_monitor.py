from __future__ import annotations

import csv
import os
import tempfile
import unittest
from pathlib import Path

import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from runtime_monitor.collectors.file_events import path_for_mode
from runtime_monitor.collectors.foreground import ForegroundState
from runtime_monitor.core.app_mapper import AppMapper, ProcessIdentity, load_config
from runtime_monitor.core.lifecycle import LifecycleEventBuilder
from runtime_monitor.core.schema import (
    APP_LIFECYCLE_EVENT_FIELDS,
    APP_STATE_1S_FIELDS,
    EVENT_FIELDS,
    FOREGROUND_DEBUG_FIELDS,
    FOREGROUND_EVENT_FIELDS,
    GLOBAL_STATE_1S_FIELDS,
    PROCESS_EVENT_FIELDS,
)
from runtime_monitor.monitor import RuntimeMonitorV0, parse_args


class RuntimeMonitorV0Tests(unittest.TestCase):
    def test_wps_process_mapping_is_configurable(self) -> None:
        config = load_config(ROOT / "config.yaml")
        mapper = AppMapper(config, target_app="WPS")
        cases = [
            ProcessIdentity(pid=1, tgid=1, comm="wps", exe_path="/opt/kingsoft/wps-office/wps"),
            ProcessIdentity(pid=2, tgid=2, comm="et", exe_path="/opt/apps/office/et"),
            ProcessIdentity(pid=3, tgid=3, comm="wpp", exe_path="/usr/bin/wpp"),
            ProcessIdentity(pid=4, tgid=4, comm="wpspdf", exe_path="/usr/bin/wpspdf"),
        ]
        for identity in cases:
            self.assertEqual(mapper.map_process(identity), "WPS")
        other = ProcessIdentity(pid=5, tgid=5, comm="firefox", exe_path="/usr/bin/firefox")
        self.assertEqual(mapper.map_process(other), "")
        background = ProcessIdentity(
            pid=6,
            tgid=6,
            comm="wpscloudsvr",
            exe_path="/opt/kingsoft/wps-office/office6/wpscloudsvr",
        )
        self.assertEqual(mapper.map_process(background), "")

    def test_schemas_match_required_columns(self) -> None:
        # Legacy EVENT_FIELDS still present
        self.assertEqual(
            EVENT_FIELDS,
            ["ts_ns", "pid", "tgid", "app", "comm", "event", "path", "ext", "inode", "offset", "size"],
        )
        # New foreground events match old APP_EVENT_FIELDS shape
        # (foreground_app replaces the generic 'app' field)
        for field in ("ts_ns", "event_type", "pid", "tgid", "window_id",
                       "window_title", "old_app", "new_app", "foreground_app",
                       "duration_ms", "source"):
            self.assertIn(field, FOREGROUND_EVENT_FIELDS)
        # Global state fields
        for field in (
            "timestamp",
            "foreground_app",
            "foreground_duration_ms",
            "observed_apps",
            "open_apps",
            "closed_apps",
            "newly_opened_apps",
            "newly_closed_apps",
            "app_history",
            "duration_history_ms",
            "global_mem_available_kb",
            "global_pgmajfault_delta",
            "global_pswpin_delta",
            "global_pswpout_delta",
            "global_pgscan_delta",
            "global_pgsteal_delta",
            "manual_label",
            "state_label",
            "current_operation_label",
            "test_mem_current",
        ):
            self.assertIn(field, GLOBAL_STATE_1S_FIELDS)
        self.assertFalse(any(field.startswith("wps_") for field in GLOBAL_STATE_1S_FIELDS))

    def test_path_privacy_modes(self) -> None:
        path = "/home/user/secret/test.docx"
        self.assertEqual(path_for_mode(path, "raw"), path)
        self.assertEqual(path_for_mode(path, "basename"), "test.docx")
        hashed = path_for_mode(path, "hash")
        self.assertNotEqual(hashed, path)
        self.assertEqual(len(hashed), 64)

    def test_monitor_generates_csv_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = parse_args(
                [
                    "--config",
                    str(ROOT / "config.yaml"),
                    "--target-app",
                    "WPS",
                    "--target-pid",
                    str(os.getpid()),
                    "--sample-interval",
                    "1",
                    "--duration",
                    "0.01",
                    "--output-dir",
                    tmp,
                    "--path-mode",
                    "basename",
                    "--foreground-backend",
                    "manual",
                    "--label",
                    "IDLE",
                    "--session-id",
                    "test001",
                ]
            )
            monitor = RuntimeMonitorV0(args)
            monitor.sample_once()
            monitor._close_writers()

            model_dir = Path(tmp) / "test001" / "model"
            global_state = model_dir / "global_state_1s.csv"
            app_state = model_dir / "app_state_1s.csv"
            foreground_events = model_dir / "foreground_events.csv"
            process_events = model_dir / "process_events.csv"
            app_lifecycle = model_dir / "app_lifecycle_events.csv"
            foreground_debug = model_dir / "foreground_debug.csv"

            self.assertTrue(global_state.exists(), f"missing {global_state}")
            self.assertTrue(app_state.exists(), f"missing {app_state}")
            self.assertTrue(foreground_events.exists(), f"missing {foreground_events}")
            self.assertTrue(process_events.exists(), f"missing {process_events}")
            self.assertTrue(app_lifecycle.exists(), f"missing {app_lifecycle}")
            self.assertTrue(foreground_debug.exists(), f"missing {foreground_debug}")

            # Verify headers
            with foreground_events.open("r", encoding="utf-8", newline="") as f:
                self.assertEqual(next(csv.reader(f)), FOREGROUND_EVENT_FIELDS)
            with app_state.open("r", encoding="utf-8", newline="") as f:
                self.assertEqual(next(csv.reader(f)), APP_STATE_1S_FIELDS)
            with global_state.open("r", encoding="utf-8", newline="") as f:
                rows = list(csv.DictReader(f))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["manual_label"], "IDLE")
            self.assertIn("global_mem_available_kb", rows[0])

    def test_lifecycle_waits_for_all_app_pids_before_close(self) -> None:
        class Sample:
            def __init__(self, pid: int, tgid: int = 1) -> None:
                self.app_id = "WPS"
                self.identity = ProcessIdentity(
                    pid=pid, tgid=tgid, comm="wps", exe_path="/usr/bin/wps",
                )

        builder = LifecycleEventBuilder(target_app="WPS", close_grace_windows=1)
        first = builder.build_all([Sample(100), Sample(101)], ForegroundState(foreground_app="WPS", source="manual"))
        process_types = [e["event_type"] for e in first.process_events]
        self.assertIn("PROCESS_START", process_types)

        second = builder.build_all([Sample(100)], ForegroundState(foreground_app="WPS", source="manual"))
        second_types = [e["event_type"] for e in second.process_events]
        self.assertIn("PROCESS_EXIT", second_types)
        lifecycle_types = [e["event_type"] for e in second.app_lifecycle]
        self.assertNotIn("APP_CLOSE", lifecycle_types)

        third = builder.build_all([], ForegroundState(source="manual"))
        third_proc = [e["event_type"] for e in third.process_events]
        self.assertIn("PROCESS_EXIT", third_proc)
        third_life = [e["event_type"] for e in third.app_lifecycle]
        self.assertIn("APP_CLOSE", third_life)


if __name__ == "__main__":
    unittest.main()
