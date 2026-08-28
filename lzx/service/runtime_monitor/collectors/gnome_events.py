"""GNOME Shell Wayland window-event subscriber for the resident monitor."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from typing import Any


INTERFACE = "org.huawei.RuntimeAppMonitor"
OBJECT_PATH = "/org/huawei/RuntimeAppMonitor"
SIGNAL = "WindowEvent"
EVENT_NAMES = {
    "Opened": "GNOME_WINDOW_OPENED",
    "Closed": "GNOME_WINDOW_CLOSED",
    "Minimized": "GNOME_WINDOW_MINIMIZED",
    "Switched": "GNOME_WINDOW_SWITCHED",
}


class GnomeEventCollector:
    """Subscribe without polling; malformed extension messages are isolated."""

    def __init__(
        self,
        callback: Callable[[dict[str, Any]], None],
        resolver: Callable[[dict[str, Any]], Any],
    ) -> None:
        self.callback = callback
        self.resolver = resolver
        self._thread: threading.Thread | None = None
        self._loop: Any | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, name="runtime-monitor-gnome-events", daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        if self._loop is not None:
            try:
                self._loop.quit()
            except Exception:
                pass
        if self._thread is not None:
            self._thread.join(timeout=3.0)
        self._thread = None
        self._loop = None

    def _run(self) -> None:
        try:
            from gi.repository import Gio, GLib
        except (ImportError, ValueError) as exc:
            self._emit_error(f"python3-gi unavailable: {exc}")
            return
        context = GLib.MainContext.new()
        self._loop = GLib.MainLoop.new(context, False)
        context.push_thread_default()
        try:
            connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)

            def callback(
                _connection: Any, _sender: str, _path: str, _iface: str,
                _signal: str, parameters: Any,
            ) -> None:
                try:
                    payload = json.loads(parameters.unpack()[0])
                    event_name = EVENT_NAMES.get(str(payload.get("event_type", "")))
                    if not event_name:
                        return
                    window = self.resolver(payload)
                    timestamp_ms = int(payload.get("timestamp_ms", 0) or 0)
                    self.callback({
                        "event_type": event_name,
                        "timestamp_ns": timestamp_ms * 1_000_000 if timestamp_ms else time.time_ns(),
                        "window_id": str(payload.get("window_id", "")),
                        "app": str(getattr(window, "app", "")),
                        "pid": int(getattr(window, "pid", 0) or 0),
                        "title": str(getattr(window, "window_title", "")),
                        "hidden": bool(getattr(window, "is_hidden", False)),
                        "source": "gnome-shell-dbus",
                    })
                except Exception as exc:
                    self._emit_error(f"invalid GNOME event: {type(exc).__name__}: {exc}")

            subscription = connection.signal_subscribe(
                None, INTERFACE, SIGNAL, OBJECT_PATH, None,
                Gio.DBusSignalFlags.NONE, callback,
            )
            self._loop.run()
            connection.signal_unsubscribe(subscription)
        except Exception as exc:
            self._emit_error(f"GNOME event collector stopped: {type(exc).__name__}: {exc}")
        finally:
            context.pop_thread_default()

    def _emit_error(self, message: str) -> None:
        try:
            self.callback({
                "event_type": "COLLECTOR_ERROR",
                "timestamp_ns": time.time_ns(),
                "source": "gnome-shell-dbus",
                "error": message,
            })
        except Exception:
            pass
