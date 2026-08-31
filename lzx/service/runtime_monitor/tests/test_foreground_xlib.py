from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from collectors.foreground import ForegroundCollector, X11WindowProperties
from collectors.x11_events import X11EventCollector


class FakeX11Reader:
    """只返回普通 Python 数据，用于验证 collector 不依赖外部 X11 命令。"""

    def __init__(self) -> None:
        self.last_error = ""
        self.closed = False
        self.active_calls = 0
        self.stacking_calls = 0
        self.window_calls: list[str] = []

    def active_window_id(self) -> str:
        self.active_calls += 1
        return "0x20"

    def client_window_ids(self, *, stacking: bool = False) -> list[str]:
        if stacking:
            self.stacking_calls += 1
        return ["0x10", "0x20"]

    def window_properties(self, window_id: str) -> X11WindowProperties:
        self.window_calls.append(window_id)
        return X11WindowProperties(
            window_id=window_id,
            wm_classes=["wps", "Wps"],
            net_wm_name="WPS 文档",
            wm_name="",
            pid=os.getpid(),
            is_hidden=False,
            is_normal_window=True,
        )

    def close(self) -> None:
        self.closed = True


class ForegroundXlibTests(unittest.TestCase):
    def test_x11_sample_reads_properties_through_in_process_reader(self) -> None:
        reader = FakeX11Reader()
        collector = ForegroundCollector(
            backend="x11",
            app_window_keywords={"WPS": ["wps"]},
            x11_reader=reader,
        )

        state = collector.sample()

        self.assertEqual(state.foreground_app, "WPS")
        self.assertEqual(state.window_id, "0x20")
        self.assertEqual(state.window_title, "WPS 文档")
        self.assertEqual(state.foreground_pid, os.getpid())
        self.assertEqual(reader.active_calls, 1)
        self.assertEqual(reader.stacking_calls, 1)
        self.assertTrue(reader.window_calls)
        # 旧字段名为 CSV 兼容而保留；xdotool 字段不再产生数据。
        self.assertEqual(collector.last_debug.active_window_id_xprop_root, "0x20")
        self.assertEqual(collector.last_debug.active_window_id_xdotool, "")
        self.assertEqual(collector.last_debug.xdotool_pid, "")

        collector.close()
        self.assertTrue(reader.closed)

    def test_event_window_resolution_uses_same_reader_and_mapping(self) -> None:
        reader = FakeX11Reader()
        collector = ForegroundCollector(
            backend="desktop",
            app_window_keywords={"WPS": ["wps"]},
            x11_reader=reader,
        )

        window = collector.resolve_window("0x20")

        self.assertEqual(window.app, "WPS")
        self.assertEqual(window.pid, os.getpid())
        self.assertEqual(reader.window_calls, ["0x20"])
        self.assertEqual(collector.last_debug.wm_class, "wps|Wps")

    def test_desktop_synthetic_window_does_not_query_x11(self) -> None:
        """X11 空活动窗口的 fallback 必须直接解析为 DESKTOP。"""
        reader = FakeX11Reader()
        collector = ForegroundCollector(
            backend="desktop",
            app_window_keywords={"DESKTOP": ["gnome-shell"]},
            x11_reader=reader,
        )

        window = collector.resolve_window("desktop")

        self.assertEqual(window.app, "DESKTOP")
        self.assertEqual(window.window_id, "desktop")
        self.assertEqual(window.pid, 0)
        self.assertEqual(window.window_title, "Desktop")
        self.assertEqual(reader.window_calls, [])

    def test_empty_focus_invalidates_stale_x11_recheck(self) -> None:
        """旧窗口的延迟复查不能越过后续的空焦点事件重新生效。"""
        emitted: list[dict[str, object]] = []
        timers: list[object] = []

        class FakeTimer:
            def __init__(self, _delay: float, callback) -> None:
                self.callback = callback
                self.daemon = False

            def start(self) -> None:
                timers.append(self)

        collector = X11EventCollector(emitted.append)
        try:
            with patch("collectors.x11_events.threading.Timer", FakeTimer):
                collector._schedule_focus_recheck("0x20")
                stale_timer = timers[-1]
                collector._schedule_focus_recheck("")
                stale_timer.callback()
                self.assertEqual(emitted, [])

                collector._schedule_focus_recheck("0x30")
                current_timer = timers[-1]
                current_timer.callback()
                self.assertEqual(len(emitted), 1)
                self.assertEqual(emitted[0]["event_type"], "FOCUS_RECHECK")
                self.assertEqual(emitted[0]["window_id"], "0x30")
        finally:
            collector.stop()


if __name__ == "__main__":
    unittest.main()
