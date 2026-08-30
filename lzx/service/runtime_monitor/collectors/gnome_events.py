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
    """订阅 GNOME Shell 扩展的窗口信号，不做活动窗口轮询。

    GNOME Shell 扩展把 Opened/Closed/Minimized/Switched 编码为 JSON 后通过
    session D-Bus 发出。本采集器只完成协议解析和窗口元数据归一化，再把原始
    ``GNOME_WINDOW_*`` 事件投递给 monitor；APP_* 语义、去重和预测均由主线程
    中的 ``X11EventState`` 负责。单条畸形消息只生成 COLLECTOR_ERROR，不会杀死
    订阅线程或常驻服务。
    """

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
        """在独立 GLib MainContext 中运行 D-Bus 订阅循环。"""
        try:
            from gi.repository import Gio, GLib
        except (ImportError, ValueError) as exc:
            self._emit_error(f"python3-gi unavailable: {exc}")
            return
        # 使用线程私有 context，避免把回调挂到进程默认 main loop；monitor 主线程
        # 有自己的 select/采样循环，不能被 GLib.run() 占用。
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
                    # 扩展信号只提供桌面协议字段；resolver 复用 ForegroundCollector
                    # 的映射规则，把窗口标题、PID 等统一成 runtime app_key。
                    payload = json.loads(parameters.unpack()[0])
                    event_name = EVENT_NAMES.get(str(payload.get("event_type", "")))
                    if not event_name:
                        return
                    window = self.resolver(payload)
                    timestamp_ms = int(payload.get("timestamp_ms", 0) or 0)
                    self.callback({
                        # 扩展时间戳是毫秒；缺失时用本机 wall-clock ns，保证状态机
                        # 仍能计算上一前台 App 的停留时长。
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
