from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from collectors.foreground import WindowState
from core.x11_event_state import X11EventState


class X11EventStateTests(unittest.TestCase):
    def test_native_lifecycle_and_focus_edges(self) -> None:
        windows = {
            "0x1": WindowState(window_id="0x1", app="FILES", pid=11, window_title="Files"),
            "0x2": WindowState(window_id="0x2", app="VLC", pid=22, window_title="VLC"),
        }
        state = X11EventState(lambda window_id: windows.get(window_id, WindowState(window_id=window_id)))

        state.handle({"event_type": "WINDOW_INITIAL", "window_id": "0x1", "timestamp_ns": 1_000_000_000})
        self.assertEqual(state.handle({"event_type": "FOCUS_CHANGED", "window_id": "0x1", "timestamp_ns": 2_000_000_000}), [])
        opened = state.handle({"event_type": "WINDOW_CREATED", "window_id": "0x2", "timestamp_ns": 3_000_000_000})
        self.assertEqual([row["event_type"] for row in opened], ["APP_OPEN"])

        switched = state.handle({"event_type": "FOCUS_CHANGED", "window_id": "0x2", "timestamp_ns": 4_000_000_000})
        self.assertEqual([row["event_type"] for row in switched], ["APP_FOCUS_OUT", "APP_SWITCH", "APP_FOCUS_IN"])

        windows["0x2"].is_hidden = True
        self.assertEqual(
            [row["event_type"] for row in state.handle({"event_type": "WINDOW_PROPERTY", "window_id": "0x2", "timestamp_ns": 5_000_000_000})],
            ["APP_MINIMIZE"],
        )
        windows["0x2"].is_hidden = False
        self.assertEqual(
            [row["event_type"] for row in state.handle({"event_type": "WINDOW_PROPERTY", "window_id": "0x2", "timestamp_ns": 6_000_000_000})],
            ["APP_RESTORE"],
        )
        self.assertEqual(
            [row["event_type"] for row in state.handle({"event_type": "WINDOW_DESTROYED", "window_id": "0x2", "timestamp_ns": 7_000_000_000})],
            ["APP_CLOSE"],
        )

    def test_same_app_window_focus_is_not_app_switch(self) -> None:
        windows = {
            "0x1": WindowState(window_id="0x1", app="FILES"),
            "0x2": WindowState(window_id="0x2", app="FILES"),
        }
        state = X11EventState(lambda window_id: windows[window_id])
        state.handle({"event_type": "WINDOW_INITIAL", "window_id": "0x1", "timestamp_ns": 1})
        state.handle({"event_type": "WINDOW_INITIAL", "window_id": "0x2", "timestamp_ns": 1})
        state.handle({"event_type": "FOCUS_CHANGED", "window_id": "0x1", "timestamp_ns": 2})
        self.assertEqual(state.handle({"event_type": "FOCUS_CHANGED", "window_id": "0x2", "timestamp_ns": 3}), [])

    def test_property_update_promotes_initially_unknown_active_window(self) -> None:
        windows = {
            "0x1": WindowState(window_id="0x1", app="FILES", pid=11, window_title="Files"),
            "0x2": WindowState(window_id="0x2", app="UNKNOWN", pid=22, window_title=""),
        }
        state = X11EventState(lambda window_id: windows[window_id])
        state.handle({"event_type": "WINDOW_INITIAL", "window_id": "0x1", "timestamp_ns": 1})
        state.handle({"event_type": "FOCUS_CHANGED", "window_id": "0x1", "timestamp_ns": 2})
        state.handle({"event_type": "WINDOW_CREATED", "window_id": "0x2", "timestamp_ns": 3})
        state.handle({"event_type": "FOCUS_CHANGED", "window_id": "0x2", "timestamp_ns": 4})

        windows["0x2"] = WindowState(window_id="0x2", app="VLC", pid=22, window_title="VLC")
        promoted = state.handle({"event_type": "WINDOW_PROPERTY", "window_id": "0x2", "timestamp_ns": 5})
        self.assertEqual(
            [row["event_type"] for row in promoted],
            ["APP_OPEN", "APP_FOCUS_OUT", "APP_SWITCH", "APP_FOCUS_IN"],
        )

    def test_cgroup_empty_emits_one_close_and_clears_windows(self) -> None:
        windows = {"0x1": WindowState(window_id="0x1", app="FILES", pid=11, window_title="Files")}
        state = X11EventState(lambda window_id: windows[window_id])
        state.handle({"event_type": "WINDOW_CREATED", "window_id": "0x1", "timestamp_ns": 1})
        closed = state.handle({
            "event_type": "CGROUP_APP_EMPTY", "app": "FILES", "timestamp_ns": 2,
            "source": "procfs-cgroup",
        })
        self.assertEqual([row["event_type"] for row in closed], ["APP_CLOSE"])
        self.assertEqual(closed[0]["source"], "procfs-cgroup")
        self.assertEqual(state.open_apps, [])


if __name__ == "__main__":
    unittest.main()
