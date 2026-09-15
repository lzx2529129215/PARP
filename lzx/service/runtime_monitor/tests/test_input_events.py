from __future__ import annotations

import json
import os
import queue
import socket
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

from runtime_monitor.collectors.input_events import INPUT_HANDLERS, InputEventCollector
from runtime_monitor.helpers.input_event_helper import InputPublisher, LibinputSource
from runtime_monitor.monitor import RuntimeMonitorV0, parse_args


class FakeAPI:
    """用同一份事件数据模拟 C API；检查解码而不依赖真实用户输入。"""
    def __init__(self):
        self.data = {}
        self.calls = []

    def __getattr__(self, name):
        def call(*args):
            self.calls.append(name)
            if name == "libinput_event_get_type":
                return self.data["type"]
            if name.endswith("get_sysname"):
                return self.data.get("device", "event1").encode()
            if name.endswith("get_name"):
                return b"test input"
            if name.endswith("_time_usec"):
                return self.data.get("time_usec", 1000)
            if name.endswith("has_axis"):
                return args[1] in self.data.get("axes", {})
            if name.endswith("get_scroll_value_v120"):
                return self.data["v120"][args[1]]
            if name.endswith("get_scroll_value"):
                return self.data["axes"][args[1]]
            return self.data.get(name.removeprefix("libinput_"), 0)
        return call


class InputDecoderTests(unittest.TestCase):
    def setUp(self):
        self.source = LibinputSource.__new__(LibinputSource)
        self.api = self.source.li = FakeAPI()
        self.source.devices = {}
        self.source.keys = {}
        self.source.buttons = set()

    def decode(self, kind, **data):
        self.api.data = {"type": kind, **data}
        return self.source.decode(123)

    def test_click_move_drag_release_and_device_removal(self):
        self.decode(1)
        pressed = self.decode(402, event_pointer_get_button=272, event_pointer_get_button_state=1)
        self.assertEqual((pressed["event_type"], pressed["button"], pressed["state"]), ("MOUSE_CLICK", "left", "pressed"))
        motion = self.decode(400, event_pointer_get_dx=3.5, event_pointer_get_dy=-2)
        self.assertTrue(motion["dragging"])
        self.assertEqual(motion["buttons_down"], [272])
        self.assertEqual((motion["dx"], motion["dy"]), (3.5, -2))
        self.decode(402, event_pointer_get_button=272, event_pointer_get_button_state=0)
        self.assertFalse(self.decode(400)["dragging"])
        self.decode(402, event_pointer_get_button=273, event_pointer_get_button_state=1)
        self.decode(2)
        self.assertEqual(self.source.devices, {})
        self.assertFalse(self.decode(400)["dragging"])

    def test_absolute_coordinates_are_not_fabricated_screen_pixels(self):
        event = self.decode(401, event_pointer_get_absolute_x_transformed=0.3, event_pointer_get_absolute_y_transformed=0.6)
        self.assertEqual((event["x_normalized"], event["y_normalized"]), (0.3, 0.6))
        self.assertNotIn("dx", event)

    def test_scroll_new_protocol_only_and_zero_finger_end(self):
        self.assertIsNone(self.decode(403))
        event = self.decode(404, axes={0: 15.0, 1: -15.0}, v120={0: 120, 1: -120})
        self.assertEqual(event["event_type"], "MOUSE_SCROLL")
        self.assertEqual(event["vertical_v120"], 120)
        self.assertEqual(event["horizontal_v120"], -120)
        self.assertEqual(self.decode(405, axes={0: 0.0})["vertical"], 0.0)
        self.assertNotIn("libinput_event_pointer_get_axis_value", self.api.calls)

    def test_keyboard_press_release_and_hold_duration(self):
        event = self.decode(300, event_keyboard_get_key=30, event_keyboard_get_key_state=1, time_usec=1000)
        self.assertEqual((event["key_code"], event["state"]), (30, "pressed"))
        release = self.decode(300, event_keyboard_get_key=30, event_keyboard_get_key_state=0, time_usec=3500)
        self.assertEqual(release["held_ns"], 2_500_000)
        self.assertEqual(release["monotonic_ns"], 3_500_000)
        self.assertNotIn("text", release)
        self.assertIsNone(self.decode(300, event_keyboard_get_key=30)["held_ns"])

    def test_swipe_phases_and_cancelled(self):
        begin = self.decode(800, event_gesture_get_finger_count=3)
        self.assertEqual((begin["event_type"], begin["motion_kind"], begin["phase"], begin["fingers"]),
                         ("MOUSE_MOVE", "swipe", "begin", 3))
        update = self.decode(801, event_gesture_get_dx=2.5, event_gesture_get_dy=1.25)
        self.assertEqual((update["dx"], update["dy"]), (2.5, 1.25))
        self.assertTrue(self.decode(802, event_gesture_get_cancelled=1)["cancelled"])


def bare_monitor():
    monitor = RuntimeMonitorV0.__new__(RuntimeMonitorV0)
    monitor.args = SimpleNamespace(suppress_event_trigger_logs=False)
    monitor._input_app_ids = {"WPS": 1, "DESKTOP": 16}
    monitor._input_event_queue = queue.Queue(maxsize=4096)
    monitor._input_event_wake_r = monitor._input_event_wake_w = -1
    monitor._input_source_instance = ""
    monitor._input_source_sequence = 0
    monitor._input_source_health = None
    monitor._input_last_status = {}
    monitor._input_started_monotonic = 100.0
    monitor._last_mouse_click_monotonic_ns = None
    monitor._input_event_drops = 0
    monitor._input_foreground_context = {"app": "WPS", "pid": 123, "window_id": "1"}
    return monitor


class InputHookTests(unittest.TestCase):
    def test_every_event_reaches_hook_known_app_only_logs_no_prediction(self):
        monitor = bare_monitor()
        monitor._run_app_event_prediction = Mock()
        for event_type, handler in INPUT_HANDLERS.items():
            original = getattr(monitor, handler)
            setattr(monitor, handler, Mock(wraps=original))
            output = StringIO()
            with redirect_stdout(output):
                for index, app in enumerate(("WPS", "UNDEFINED", "DESKTOP"), start=1):
                    monitor._handle_input_event({"event_type": event_type, "source": "libinput",
                                                 "monotonic_ns": index * 1_000_000_000,
                                                 "foreground_context": {"app": app}})
            self.assertEqual(getattr(monitor, handler).call_count, 3)
            rows = [json.loads(line) for line in output.getvalue().splitlines()]
            self.assertEqual([row["app_id"] for row in rows], [1, 16])
            self.assertTrue(all(row["handler"] == handler for row in rows))
            self.assertTrue(all(row["attribution"] == "FOREGROUND_SNAPSHOT" for row in rows))
        monitor._run_app_event_prediction.assert_not_called()

    def test_click_one_second_boundary_and_ignored_events_do_not_extend_wait(self):
        monitor = bare_monitor()
        monitor._log_input_hook = Mock()
        events = [
            {"monotonic_ns": 10_000_000_000, "state": "pressed"},
            {"monotonic_ns": 10_100_000_000, "state": "released"},
            {"monotonic_ns": 10_999_999_999, "state": "pressed"},
            {"monotonic_ns": 11_000_000_000, "state": "pressed"},
        ]
        # 即使主线程稍后一次性处理这些事件，也按来源时刻判断真实间隔。
        for event in events:
            monitor.mouseClick(event)
        self.assertEqual(monitor._log_input_hook.call_args_list,
                         [call("mouseClick", events[0]), call("mouseClick", events[3])])
        self.assertEqual(monitor._last_mouse_click_monotonic_ns, 11_000_000_000)

    def test_click_throttle_is_shared_across_apps_and_does_not_affect_other_hooks(self):
        monitor = bare_monitor()
        monitor._log_input_hook = Mock()
        monitor.mouseClick({"monotonic_ns": 1_000_000_000, "button_code": 272, "foreground_context": {"app": "WPS"}})
        monitor.mouseClick({"monotonic_ns": 1_500_000_000, "button_code": 273, "foreground_context": {"app": "DESKTOP"}})
        for handler in (monitor.mouseScroll, monitor.mouseMove, monitor.keyPress):
            handler({"monotonic_ns": 1_500_000_000})
        self.assertEqual([call.args[0] for call in monitor._log_input_hook.call_args_list],
                         ["mouseClick", "mouseScroll", "mouseMove", "keyPress"])

    def test_click_without_source_time_uses_monotonic_clock(self):
        monitor = bare_monitor()
        monitor._log_input_hook = Mock()
        with patch('time.monotonic_ns', side_effect=[100, 500_000_100, 1_000_000_100]):
            for _ in range(3):
                monitor.mouseClick({})
        self.assertEqual(monitor._log_input_hook.call_count, 2)

    def test_capture_foreground_when_enqueued_and_drain_budget(self):
        monitor = bare_monitor()
        received = []
        monitor.mouseMove = received.append
        for index in range(300):
            monitor._enqueue_input_event({"kind": "INPUT_EVENTS", "source_instance_id": "a", "events": [
                {"event_type": "MOUSE_MOVE", "source_seq": index + 1},
            ]})
        monitor._input_foreground_context = {"app": "DESKTOP"}
        monitor._drain_input_events()
        self.assertEqual(len(received), 256)
        self.assertEqual(monitor._input_event_queue.qsize(), 44)
        monitor._drain_input_events()
        self.assertEqual(len(received), 300)
        self.assertTrue(all(e["foreground_context"]["app"] == "WPS" for e in received))

    def test_queue_overflow_is_counted_and_gap_reported(self):
        monitor = bare_monitor()
        monitor._input_event_queue = queue.Queue(maxsize=1)
        monitor._enqueue_input_event({"kind": "INPUT_EVENTS", "events": [{"event_type": "MOUSE_MOVE"}] * 2})
        self.assertEqual(monitor._input_event_drops, 1)
        monitor._input_source_sequence = 7
        monitor._input_source_instance = "a"
        with redirect_stdout(StringIO()) as output:
            monitor._handle_input_event({"event_type": "MOUSE_MOVE", "source_seq": 9, "source_instance_id": "a"})
        self.assertEqual(json.loads(output.getvalue())["status"], "SEQUENCE_GAP")

    def test_unchanged_heartbeat_does_not_repeat_log(self):
        monitor = bare_monitor()
        with redirect_stdout(StringIO()) as output:
            for _ in range(5):
                monitor._report_input_source({"status": "READY", "source_instance_id": "a"})
        self.assertEqual(len(output.getvalue().splitlines()), 1)

    def test_no_devices_is_not_heartbeat_timeout(self):
        monitor = bare_monitor()
        monitor.input_event_collector = SimpleNamespace(last_message_monotonic=100.0)
        with redirect_stdout(StringIO()) as output, patch('time.monotonic', return_value=101.0):
            monitor._handle_input_event({"kind": "SOURCE_STATUS", "status": "NO_DEVICES"})
            monitor._check_input_event_health()
            monitor._handle_input_event({"kind": "SOURCE_STATUS", "status": "NO_DEVICES"})
        self.assertEqual(len(output.getvalue().splitlines()), 1)
        with redirect_stdout(StringIO()) as output, patch('time.monotonic', return_value=111.0):
            monitor._check_input_event_health()
            monitor._check_input_event_health()
        self.assertEqual(json.loads(output.getvalue())["status"], "STALE")

    def test_hotplug_changes_device_status_once(self):
        monitor = bare_monitor()
        with redirect_stdout(StringIO()) as output:
            for devices in ({"event0": "keyboard"}, {"event0": "keyboard", "event1": "mouse"}):
                monitor._report_input_source({"status": "READY", "devices": devices})
                monitor._report_input_source({"status": "READY", "devices": devices})
        self.assertEqual(len(output.getvalue().splitlines()), 2)

    def test_standalone_defaults_to_off(self):
        self.assertEqual(parse_args([]).input_event_source, "off")


class InputTransportTests(unittest.TestCase):
    def test_accepts_input_not_process_protocol_or_bad_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "input.sock"
            received = []
            collector = InputEventCollector(received.append, socket_path=path, expected_uid=os.getuid())
            collector.start()
            sender = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            try:
                for source, version in (("proc-connector", 1), ("libinput", "bad")):
                    sender.sendto(json.dumps({"source": source, "protocol_version": version}).encode(), str(path))
                sender.sendto(json.dumps({"source": "libinput", "protocol_version": 1, "kind": "SOURCE_STATUS", "status": "READY"}).encode(), str(path))
                self.assertTrue(collector.wait_ready(1))
                deadline = time.monotonic() + 1
                while len(received) < 1 and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertEqual(len(received), 1)
                self.assertEqual(collector.rejected_datagrams, 2)
                collector.expected_uid = os.getuid() + 10000
                sender.sendto(b'{}', str(path))
                deadline = time.monotonic() + 1
                while collector.rejected_datagrams < 3 and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertEqual(collector.rejected_datagrams, 3)
            finally:
                sender.close()
                collector.stop()
            self.assertFalse(path.exists())

    def test_publisher_missing_receiver_counts_events(self):
        publisher = InputPublisher(1000, Path('/run/user/1000/parp-test-absent-input.sock'))
        try:
            publisher.send({"kind": "INPUT_EVENTS", "events": [{"event_type": "KEY_PRESS"}] * 3})
            self.assertEqual(publisher.sequence, 3)
            self.assertEqual(publisher.delivery_drops, 3)
        finally:
            publisher.sock.close()


if __name__ == "__main__":
    unittest.main()
