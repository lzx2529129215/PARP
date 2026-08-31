from __future__ import annotations

import csv
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import Mock, patch

import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from runtime_monitor.collectors.file_events import path_for_mode
from runtime_monitor.collectors.foreground import ForegroundState
from runtime_monitor.core.app_mapper import AppMapper, ProcessIdentity, load_config
from runtime_monitor.core.app_registry import AppRegistry
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
from runtime_monitor import monitor as monitor_module


class RuntimeMonitorV0Tests(unittest.TestCase):
    def test_registry_history_is_bounded_for_resident_service(self) -> None:
        registry = AppRegistry(target_apps=["A", "B"], history_window=8)
        for index in range(1000):
            app = "A" if index % 2 == 0 else "B"
            registry.update([], ForegroundState(
                foreground_app=app,
                foreground_duration=float(index),
                source="manual",
            ))

        summary = registry.summary()
        self.assertEqual(len(summary["app_history"].split("|")), 8)
        self.assertEqual(len(summary["duration_history"].split("|")), 8)
        self.assertLess(len(summary["app_history"]), 32)
        self.assertLess(len(summary["duration_history"]), 128)

    def test_registry_history_can_be_disabled(self) -> None:
        registry = AppRegistry(target_apps=["A"], history_window=0)
        registry.update([], ForegroundState(
            foreground_app="A", foreground_duration=1.0, source="manual",
        ))
        summary = registry.summary()
        self.assertEqual(summary["app_history"], "")
        self.assertEqual(summary["duration_history"], "")

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

    def test_process_keywords_do_not_cross_match_executable_substrings(self) -> None:
        config = {
            "apps": {
                "LIBREOFFICE": {"keywords": ["soffice.bin", "soffice", "lowriter"]},
                "WPS": {"keywords": ["wps", "et", "wpp", "wpspdf", "wpsoffice"]},
            }
        }
        wps = ProcessIdentity(
            pid=7,
            tgid=7,
            comm="wpsoffice",
            exe_path="/opt/kingsoft/wps-office/office6/wpsoffice",
        )
        libreoffice = ProcessIdentity(
            pid=8,
            tgid=8,
            comm="soffice.bin",
            exe_path="/usr/lib/libreoffice/program/soffice.bin",
        )

        both = AppMapper(
            config,
            target_app="LIBREOFFICE",
            target_apps=["LIBREOFFICE", "WPS"],
        )
        resident_without_wps = AppMapper(
            config,
            target_app="LIBREOFFICE",
            target_apps=["LIBREOFFICE"],
        )
        self.assertEqual(both.map_process(wps), "WPS")
        self.assertEqual(both.map_process(libreoffice), "LIBREOFFICE")
        self.assertEqual(resident_without_wps.map_process(wps), "")

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

    def test_storage_guard_requests_clean_rotation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = parse_args([
                "--config", str(ROOT / "config.yaml"),
                "--target-app", "WPS",
                "--target-pid", str(os.getpid()),
                "--output-dir", tmp,
                "--foreground-backend", "manual",
                "--session-id", "storage-guard",
                "--max-output-root-bytes", "1",
                "--storage-check-interval", "1",
            ])
            monitor = RuntimeMonitorV0(args)
            monitor.sample_once()
            monitor._close_writers()
            self.assertTrue(monitor.stop_requested)

    def test_direct_event_sample_reads_state_without_foreground_poll(self) -> None:
        """直接事件模式的秒级采样不得再主动查询一次活动窗口。"""
        with tempfile.TemporaryDirectory() as tmp:
            args = parse_args([
                "--config", str(ROOT / "config.yaml"),
                "--app-scope-config", "",
                "--target-app", "WPS",
                "--output-dir", tmp,
                "--session-id", "event-only-foreground",
                "--foreground-backend", "desktop",
                "--direct-x11-events",
            ])
            monitor = RuntimeMonitorV0(args)
            monitor.foreground_collector.sample = Mock(
                side_effect=AssertionError("direct mode must not poll foreground")
            )
            monitor._direct_event_state.foreground_app = "WPS"
            monitor._direct_event_state.foreground_pid = os.getpid()
            monitor._direct_event_state.foreground_window_id = "0x20"
            monitor._direct_event_state.foreground_title = "WPS 文档"
            monitor._direct_event_state.foreground_since_ns = 1

            try:
                monitor.sample_once()
            finally:
                monitor._close_writers()

            monitor.foreground_collector.sample.assert_not_called()

    def test_connector_steady_state_does_not_poll_all_pids_or_duplicate_lifecycle(self) -> None:
        """生产组合的秒级 tick 只能定向采样统一索引，不再运行 PID 差分。"""
        with tempfile.TemporaryDirectory() as tmp:
            args = parse_args([
                "--config", str(ROOT / "config.yaml"),
                "--app-scope-config", "",
                "--target-app", "WPS",
                "--output-dir", tmp,
                "--session-id", "index-no-full-poll",
                "--foreground-backend", "desktop",
                "--direct-x11-events",
                "--process-event-source", "connector",
            ])
            monitor = RuntimeMonitorV0(args)
            # 模拟 run() 已经完成一次启动基线；steady-state 的任意 tick 如果又
            # 做全量发现或 LifecycleEventBuilder PID 差分，测试立即失败。
            monitor._app_process_index_bootstrapped = True
            monitor.process_collector._all_pids = Mock(  # type: ignore[method-assign]
                side_effect=AssertionError("must not enumerate all /proc pids")
            )
            monitor.process_collector.discover_identities = Mock(
                side_effect=AssertionError("must not rebuild without an audited gap")
            )
            monitor.lifecycle_builder.build_all = Mock(
                side_effect=AssertionError("must not maintain a second pid-set table")
            )
            try:
                monitor.sample_once()
            finally:
                monitor._stop_global_process_events()
                monitor._close_writers()

            monitor.process_collector._all_pids.assert_not_called()
            monitor.process_collector.discover_identities.assert_not_called()
            monitor.lifecycle_builder.build_all.assert_not_called()

    def test_switch_and_minimize_reach_named_handlers(self) -> None:
        """高层切换和最小化事件必须进入对应的显式业务入口。"""
        with tempfile.TemporaryDirectory() as tmp:
            args = parse_args([
                "--config", str(ROOT / "config.yaml"),
                "--app-scope-config", "",
                "--target-app", "WPS",
                "--output-dir", tmp,
                "--session-id", "named-app-handlers",
                "--foreground-backend", "manual",
            ])
            monitor = RuntimeMonitorV0(args)
            switch_app = Mock(wraps=monitor.switchApp)
            minimize_app = Mock(wraps=monitor.minimizeApp)
            monitor.switchApp = switch_app
            monitor.minimizeApp = minimize_app
            # 用轻量 Mock 锁定 LSTM 的真实调用位置：两次 process_event 必须分别
            # 发生在 switchApp/minimizeApp 包装的方法执行期间。
            online_lstm = Mock()
            online_lstm.process_event.side_effect = [
                {
                    "status": "success",
                    "prediction_id": "switch-prediction",
                    "inference_executed": True,
                    "trigger_type": "APP_SWITCH",
                },
                {
                    "status": "success",
                    "prediction_id": "minimize-prediction",
                    "inference_executed": True,
                    "trigger_type": "APP_MINIMIZE",
                },
            ]
            monitor.online_lstm = online_lstm
            events = [
                {
                    "event_type": "APP_SWITCH",
                    "ts_ns": 1_000_000_000,
                    "timestamp": "1970-01-01T00:00:01.000",
                    "app": "WPS",
                    "old_app": "FIREFOX",
                    "new_app": "WPS",
                    "source": "test",
                },
                {
                    "event_type": "APP_MINIMIZE",
                    "ts_ns": 2_000_000_000,
                    "timestamp": "1970-01-01T00:00:02.000",
                    "app": "WPS",
                    "source": "test",
                },
            ]
            monitor._direct_event_state.handle = Mock(
                side_effect=[[events[0]], [events[1]]]
            )
            trigger_log = StringIO()
            with redirect_stdout(trigger_log):
                try:
                    monitor._handle_direct_x11_event({"event_type": "TEST_SWITCH"})
                    monitor._handle_direct_x11_event({"event_type": "TEST_MINIMIZE"})
                finally:
                    monitor._close_writers()

            switch_app.assert_called_once()
            minimize_app.assert_called_once()
            self.assertEqual(switch_app.call_args.args[0]["event_type"], "APP_SWITCH")
            self.assertEqual(
                minimize_app.call_args.args[0]["event_type"], "APP_MINIMIZE"
            )
            self.assertEqual(online_lstm.process_event.call_count, 2)
            self.assertEqual(
                [call.args[1] for call in online_lstm.process_event.call_args_list],
                ["APP_SWITCH", "APP_MINIMIZE"],
            )
            self.assertEqual(
                trigger_log.getvalue().count('"handler":"switchApp"'), 1
            )
            self.assertEqual(
                trigger_log.getvalue().count('"handler":"minimizeApp"'), 1
            )
            foreground_log = (
                Path(tmp) / "named-app-handlers" / "model" / "foreground_events.csv"
            )
            with foreground_log.open(encoding="utf-8", newline="") as f:
                rows = list(csv.DictReader(f))
            self.assertEqual(
                [row["event_type"] for row in rows],
                ["APP_SWITCH", "APP_MINIMIZE"],
            )

    def test_desktop_focus_arbiter_prefers_gnome_and_delays_x11_fallback(self) -> None:
        """Wayland 的 GNOME 结果不能再被 X11 空活动窗口覆盖成 UNKNOWN。"""
        monitor = RuntimeMonitorV0.__new__(RuntimeMonitorV0)
        monitor.args = monitor_module.argparse.Namespace(foreground_backend="desktop")
        monitor._direct_event_queue = monitor_module.queue.Queue()
        monitor._direct_event_wake_w = -1
        monitor._direct_focus_lock = monitor_module.threading.Lock()
        monitor._direct_focus_generation = 0
        monitor._last_gnome_focus_monotonic = float("-inf")
        timers: list[object] = []

        class FakeTimer:
            def __init__(self, delay: float, callback) -> None:
                self.delay = delay
                self.callback = callback
                self.daemon = False

            def start(self) -> None:
                timers.append(self)

        x11_empty = {
            "event_type": "FOCUS_CHANGED",
            "timestamp_ns": 1,
            "source": "x11-event",
            "window_id": "",
        }
        gnome_files = {
            "event_type": "GNOME_WINDOW_SWITCHED",
            "timestamp_ns": 2,
            "source": "gnome-shell-dbus",
            "window_id": "14",
            "app": "FILES",
        }

        with patch.object(monitor_module.threading, "Timer", FakeTimer):
            # X11 先到时只建立 fallback 候选，不立即生成 UNKNOWN/DESKTOP。
            monitor._enqueue_direct_x11_event(x11_empty)
            self.assertTrue(monitor._direct_event_queue.empty())
            self.assertEqual(timers[-1].delay, 0.5)

            # 500 ms 内到达 GNOME 的 FILES 后立即入队，并使旧候选失效。
            monitor._enqueue_direct_x11_event(gnome_files)
            timers[0].callback()
            queued = monitor._direct_event_queue.get_nowait()
            self.assertEqual(queued["app"], "FILES")
            self.assertTrue(monitor._direct_event_queue.empty())

            # 没有 GNOME 来源时，X11 空窗口才在延迟后兜底为 DESKTOP。
            monitor._last_gnome_focus_monotonic = float("-inf")
            monitor._enqueue_direct_x11_event(x11_empty)
            timers[-1].callback()
            fallback = monitor._direct_event_queue.get_nowait()
            self.assertEqual(fallback["window_id"], "desktop")
            self.assertEqual(fallback["fallback_reason"], "x11-active-window-empty")

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
