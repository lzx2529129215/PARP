"""Foreground application collection."""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
import datetime as dt
from pathlib import Path
from typing import Any


@dataclass
class ForegroundState:
    foreground_app: str = ""
    foreground_pid: int = 0
    window_id: str = ""
    window_title: str = ""
    foreground_duration: float = 0.0
    is_hidden: bool = False
    source: str = "manual"


@dataclass
class WindowState:
    window_id: str = ""
    app: str = ""
    pid: int = 0
    window_title: str = ""
    is_hidden: bool = False
    source: str = "x11"


@dataclass
class ForegroundDebugState:
    session_id: str = ""
    feature_window_id: int = 0
    ts_ns: int = 0
    timestamp: str = ""
    active_window_id_xdotool: str = ""
    active_window_id_xprop_root: str = ""
    chosen_window_id: str = ""
    xdotool_window_name: str = ""
    xprop_net_wm_name: str = ""
    xprop_wm_name: str = ""
    wm_class: str = ""
    net_wm_pid: str = ""
    xdotool_pid: str = ""
    pid_comm: str = ""
    pid_cmdline: str = ""
    mapped_app: str = ""
    previous_foreground_app: str = ""
    foreground_app: str = ""
    window_title: str = ""
    error: str = ""


@dataclass
class X11WindowProperties:
    """一次进程内 X11 属性读取的稳定结果。

    该结构只保存普通 Python 类型，避免把 python-xlib 的 Window、Atom 或 array
    对象泄漏到业务层。窗口在读取期间被销毁时，reader 返回 ``None``，调用方按
    一次事件时刻的元数据缺失处理，不会启动外部命令重试。
    """

    window_id: str
    wm_classes: list[str]
    net_wm_name: str
    wm_name: str
    pid: int
    is_hidden: bool
    is_normal_window: bool


class _X11PropertyReader:
    """通过一个可重连的 python-xlib 连接读取根窗口和客户端属性。

    X11EventCollector 负责阻塞等待事件；本类只在事件需要解析窗口元数据时执行
    X11 请求。连接在进程内长期复用，因此不会像 xprop/xdotool 那样为每次查询
    fork/exec 新进程。连接断开后下一次读取会自动重新发现 DISPLAY/XAUTHORITY。
    """

    def __init__(self, display_name: str | None = None) -> None:
        self.display_name = display_name
        self._display: Any | None = None
        self._root: Any | None = None
        self._x: Any | None = None
        self._atoms: dict[str, int] = {}
        self._lock = threading.RLock()
        self.last_error = ""

    def close(self) -> None:
        """关闭持久 X11 连接；重复调用安全。"""
        with self._lock:
            self._reset_connection()

    def active_window_id(self) -> str:
        """读取根窗口的 _NET_ACTIVE_WINDOW，不产生子进程。"""
        with self._lock:
            try:
                self._ensure_connection()
                prop = self._property(self._root, "_NET_ACTIVE_WINDOW")
                value = _first_int_property_value(prop)
                self.last_error = ""
                return _format_x11_window_id(value)
            except Exception as exc:
                self._record_connection_error(exc)
                return ""

    def client_window_ids(self, *, stacking: bool = False) -> list[str]:
        """读取 EWMH 客户端列表；stacking=True 时保持从底到顶的顺序。"""
        with self._lock:
            try:
                self._ensure_connection()
                name = "_NET_CLIENT_LIST_STACKING" if stacking else "_NET_CLIENT_LIST"
                prop = self._property(self._root, name)
                values = [] if prop is None else [int(value) for value in prop.value]
                self.last_error = ""
                return [_format_x11_window_id(value) for value in values if value]
            except Exception as exc:
                self._record_connection_error(exc)
                return []

    def window_properties(self, window_id: str) -> X11WindowProperties | None:
        """在当前连接上读取一个窗口映射 App 所需的全部 EWMH/ICCCM 属性。"""
        with self._lock:
            try:
                self._ensure_connection()
                numeric_id = int(str(window_id), 0)
                window = self._display.create_resource_object("window", numeric_id)
                # get_attributes 是一个同步请求，可在窗口已销毁时尽早得到 BadWindow。
                window.get_attributes()
                wm_classes = _decode_wm_class(self._property(window, "WM_CLASS"))
                net_wm_name = _decode_text_property(
                    self._property(window, "_NET_WM_NAME")
                )
                wm_name = _decode_text_property(self._property(window, "WM_NAME"))
                pid = _first_int_property_value(self._property(window, "_NET_WM_PID"))
                state_values = _int_property_values(self._property(window, "_NET_WM_STATE"))
                type_values = _int_property_values(
                    self._property(window, "_NET_WM_WINDOW_TYPE")
                )
                hidden_atom = self._atom("_NET_WM_STATE_HIDDEN")
                normal_atom = self._atom("_NET_WM_WINDOW_TYPE_NORMAL")
                self.last_error = ""
                return X11WindowProperties(
                    window_id=_format_x11_window_id(numeric_id),
                    wm_classes=wm_classes,
                    net_wm_name=net_wm_name,
                    wm_name=wm_name,
                    pid=pid,
                    is_hidden=hidden_atom in state_values,
                    # 缺失窗口类型时保持旧实现的宽容策略；无 PID 的辅助窗口仍会
                    # 在 ForegroundCollector 中被排除。
                    is_normal_window=not type_values or normal_atom in type_values,
                )
            except Exception as exc:
                self._record_connection_error(exc)
                return None

    def _ensure_connection(self) -> None:
        if self._display is not None and self._root is not None:
            return
        _configure_x11_env()
        from Xlib import X, display

        self._x = X
        self._display = display.Display(self.display_name)
        # 窗口可能在通知与属性读取之间消失；同步请求仍会由上层 try/except 兜底。
        self._display.set_error_handler(lambda *_args: None)
        self._root = self._display.screen().root
        self._atoms.clear()

    def _atom(self, name: str) -> int:
        atom = self._atoms.get(name)
        if atom is None:
            atom = int(self._display.intern_atom(name, only_if_exists=False))
            self._atoms[name] = atom
        return atom

    def _property(self, window: Any, name: str) -> Any | None:
        return window.get_full_property(self._atom(name), self._x.AnyPropertyType)

    def _record_connection_error(self, exc: Exception) -> None:
        self.last_error = f"python-xlib {type(exc).__name__}: {exc}"
        # BadWindow 之外的连接错误无法可靠区分；关闭后让下一条真实事件重连。
        self._reset_connection()

    def _reset_connection(self) -> None:
        display_connection = self._display
        self._display = None
        self._root = None
        self._x = None
        self._atoms.clear()
        if display_connection is not None:
            try:
                display_connection.close()
            except Exception:
                pass


DEFAULT_WINDOW_KEYWORDS = {
    "WPS": ["wps", "wpsoffice", "kingsoft"],
    "QQ": ["linuxqq", "tencent", "腾讯", "qq"],
    "FILES": ["org.gnome.nautilus", "nautilus", "files", "文件", "home", "主文件夹"],
}


class ForegroundCollector:
    """解析当前活动窗口，并把窗口元数据映射为 runtime app_key。

    生产 direct-event 模式不再调用 ``sample``：前台切换和最小化完全来自
    GNOME/X11 常驻监听器。``resolve_window`` 在事件到达时通过持久 python-xlib
    连接读取属性；``resolve_desktop_event`` 处理 GNOME 已随信号携带的元数据。
    """
    def __init__(
        self,
        backend: str = "manual",
        manual_app: str = "",
        manual_pid: int = 0,
        app_window_keywords: dict[str, list[str]] | None = None,
        x11_reader: _X11PropertyReader | None = None,
    ) -> None:
        self.backend = backend
        self.manual_app = manual_app
        self.manual_pid = manual_pid
        self.app_window_keywords = app_window_keywords or DEFAULT_WINDOW_KEYWORDS
        self._last_key = ""
        self._last_since = time.monotonic()
        self._last_state = ForegroundState(foreground_app=manual_app, foreground_pid=manual_pid, source="manual")
        self.last_debug = ForegroundDebugState()
        if backend in {"x11", "desktop"}:  # lzx-note
            _configure_x11_env()
        self._x11_reader = x11_reader or _X11PropertyReader()

    def close(self) -> None:
        """释放进程内 X11 属性连接；手动 backend 下该调用同样安全。"""
        self._x11_reader.close()

    def sample(self) -> ForegroundState:
        """返回当前前台快照，并按 App+window_id 维护连续前台时长。"""
        previous_app = self._last_state.foreground_app
        if self.backend in {"x11", "desktop"}:
            state, debug = self._sample_x11(previous_app)
            self.last_debug = debug
        else:
            state = ForegroundState(foreground_app=self.manual_app, foreground_pid=self.manual_pid, source="manual")
            self.last_debug = ForegroundDebugState(
                previous_foreground_app=previous_app,
                mapped_app=state.foreground_app,
                foreground_app=state.foreground_app,
                window_title=state.window_title,
            )
        if not state.foreground_app:
            state.foreground_app = "UNKNOWN"
        key = f"{state.foreground_app}:{state.window_id}"
        now = time.monotonic()
        if key != self._last_key:
            self._last_key = key
            self._last_since = now
        state.foreground_duration = max(0.0, now - self._last_since)
        self._last_state = state
        return state

    def sample_windows(self) -> list[WindowState]:
        if self.backend not in {"x11", "desktop"}:
            return []
        return self._sample_x11_windows()

    def resolve_window(self, window_id: str) -> WindowState:
        """在原生事件给出 window_id 时解析一次 X11 窗口。

        这是事件时刻的进程内 X11 属性读取，不是活动窗口轮询，也不会 fork/exec
        xprop 或 xdotool。方法保持公开，以便状态机复用同一套 App 映射规则。
        """
        if str(window_id).lower() == "desktop":
            # ``desktop`` 是 monitor 在 GNOME 没有及时给出焦点信号时，为 X11
            # “活动窗口为空”生成的语义窗口 ID。它不是一个真实 X11 resource，
            # 因而不能传给 int(window_id, 0)/python-xlib；直接返回运行时 DESKTOP
            # App，PID=0 表示这个状态属于 Shell，而不是某个用户进程。
            app = "DESKTOP" if "DESKTOP" in self.app_window_keywords else "UNKNOWN"
            return WindowState(
                window_id="desktop",
                app=app,
                pid=0,
                window_title="Desktop",
                is_hidden=False,
                source="x11-event",
            )
        if self.backend not in {"x11", "desktop"} or not window_id:
            return WindowState(window_id=window_id, source="x11-event")
        window = self._read_x11_window(window_id)
        if window.app != "UNKNOWN":
            return window
        # Openbox can expose the active frame before the client window's
        # metadata.  This fallback runs only while resolving an X11 event;
        # it is not part of the periodic sample path.
        if self._is_active_window(window_id):
            stacking_window = self._top_stacking_window()
            if stacking_window.window_id and stacking_window.app != "UNKNOWN":
                return stacking_window
        return window

    def resolve_desktop_event(self, payload: dict[str, object]) -> WindowState:
        """Map GNOME Wayland metadata with the same 15-app rules. lzx-note"""
        pid = int(payload.get("pid", 0) or 0)
        title = str(payload.get("title", "") or "")
        classes = [
            str(payload.get("wm_class", "") or ""),
            str(payload.get("gtk_app_id", "") or ""),
        ]
        window = WindowState(
            window_id=str(payload.get("window_id", "") or ""),
            app=_map_foreground_app(classes, title, pid, self.app_window_keywords),
            pid=pid,
            window_title=title,
            is_hidden=bool(payload.get("is_minimized", False)),
            source="gnome-shell-dbus",
        )
        now_ns = time.time_ns()
        self.last_debug = ForegroundDebugState(
            ts_ns=now_ns,
            timestamp=dt.datetime.fromtimestamp(
                now_ns / 1_000_000_000
            ).strftime("%Y-%m-%d %H:%M:%S"),
            chosen_window_id=window.window_id,
            wm_class="|".join(item for item in classes if item),
            net_wm_pid=str(pid) if pid else "",
            pid_comm=_read_proc_text(pid, "comm").strip() if pid else "",
            pid_cmdline=(
                _read_proc_text(pid, "cmdline").replace("\x00", " ").strip()
                if pid else ""
            ),
            mapped_app=window.app,
            foreground_app=window.app,
            window_title=window.window_title,
        )
        return window

    def _is_active_window(self, window_id: str) -> bool:
        return _same_x11_window_id(window_id, self._x11_reader.active_window_id())

    def _sample_x11(self, previous_app: str) -> tuple[ForegroundState, ForegroundDebugState]:
        ts_ns = time.time_ns()
        debug = ForegroundDebugState(
            ts_ns=ts_ns,
            timestamp=dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            previous_foreground_app=previous_app,
        )
        errors: list[str] = []

        # 字段名为兼容既有 CSV schema 保留；值已由 python-xlib 直接读取根窗口
        # _NET_ACTIVE_WINDOW，不再来自 xprop 命令。
        debug.active_window_id_xprop_root = self._x11_reader.active_window_id()
        if not debug.active_window_id_xprop_root and self._x11_reader.last_error:
            errors.append(self._x11_reader.last_error)

        chosen = debug.active_window_id_xprop_root
        if not chosen or chosen == "0x0":
            stacking_window = self._top_stacking_window()
            if stacking_window.window_id and stacking_window.app != "UNKNOWN":
                errors.append(f"active_window_missing_stacking_fallback={stacking_window.window_id}")
                debug.chosen_window_id = stacking_window.window_id
                window = self._read_x11_window_debug(stacking_window.window_id, debug)
                debug.error = "; ".join(item for item in errors if item)
                debug.mapped_app = window.app or "UNKNOWN"
                debug.foreground_app = debug.mapped_app
                debug.window_title = window.window_title
                return (
                    ForegroundState(
                        foreground_app=debug.foreground_app,
                        foreground_pid=window.pid,
                        window_id=window.window_id,
                        window_title=window.window_title,
                        is_hidden=window.is_hidden,
                        source="x11",
                    ),
                    debug,
                )
            debug.error = "; ".join(item for item in errors if item) or "no_active_window"
            debug.mapped_app = "UNKNOWN"
            debug.foreground_app = "UNKNOWN"
            return ForegroundState(foreground_app="UNKNOWN", source="x11"), debug

        debug.chosen_window_id = chosen
        window = self._read_x11_window_debug(chosen, debug)
        stacking_window = self._top_stacking_window()
        if (
            stacking_window.window_id
            and stacking_window.app != "UNKNOWN"
            and stacking_window.window_id != window.window_id
        ):
            errors.append(f"stacking_override={stacking_window.window_id}")
            debug = ForegroundDebugState(
                ts_ns=debug.ts_ns,
                timestamp=debug.timestamp,
                active_window_id_xdotool=debug.active_window_id_xdotool,
                active_window_id_xprop_root=debug.active_window_id_xprop_root,
                chosen_window_id=stacking_window.window_id,
                previous_foreground_app=debug.previous_foreground_app,
            )
            window = self._read_x11_window_debug(stacking_window.window_id, debug)
        if window.app == "UNKNOWN" and debug.error:
            errors.append(debug.error)
        debug.error = "; ".join(item for item in errors if item)
        debug.mapped_app = window.app or "UNKNOWN"
        debug.foreground_app = debug.mapped_app
        debug.window_title = window.window_title
        return (
            ForegroundState(
                foreground_app=debug.foreground_app,
                foreground_pid=window.pid,
                window_id=window.window_id,
                window_title=window.window_title,
                is_hidden=window.is_hidden,
                source="x11",
            ),
            debug,
        )

    def _sample_x11_windows(self) -> list[WindowState]:
        window_ids = self._x11_reader.client_window_ids()
        windows: list[WindowState] = []
        for window_id in window_ids:
            window = self._read_x11_window(window_id)
            if window.window_id:
                windows.append(window)
        return windows

    def _top_stacking_window(self) -> WindowState:
        window_ids = self._x11_reader.client_window_ids(stacking=True)
        for window_id in reversed(window_ids):
            debug = ForegroundDebugState(chosen_window_id=window_id)
            window = self._read_x11_window_debug(window_id, debug)
            if window.window_id and window.app != "UNKNOWN" and not window.is_hidden:
                return window
        return WindowState()

    def _read_x11_window(self, window_id: str) -> WindowState:
        now_ns = time.time_ns()
        debug = ForegroundDebugState(
            ts_ns=now_ns,
            timestamp=dt.datetime.fromtimestamp(
                now_ns / 1_000_000_000
            ).strftime("%Y-%m-%d %H:%M:%S"),
            chosen_window_id=window_id,
            previous_foreground_app=self._last_state.foreground_app,
        )
        window = self._read_x11_window_debug(window_id, debug)
        debug.mapped_app = window.app or "UNKNOWN"
        debug.foreground_app = debug.mapped_app
        debug.window_title = window.window_title
        # 直接事件模式没有秒级 foreground sample；保留最近一次事件解析信息，
        # 让既有 foreground_debug.csv 仍能反映窗口映射状态。
        self.last_debug = debug
        return window

    def _read_x11_window_debug(self, window_id: str, debug: ForegroundDebugState) -> WindowState:
        props = self._x11_reader.window_properties(window_id)
        if props is None:
            debug.error = self._x11_reader.last_error or "x11_window_unavailable"
            return WindowState(window_id=window_id, source="x11")

        pid = props.pid
        wm_class_values = props.wm_classes
        title = props.net_wm_name or props.wm_name
        debug.xprop_net_wm_name = props.net_wm_name
        debug.xprop_wm_name = props.wm_name
        debug.wm_class = "|".join(wm_class_values)
        debug.net_wm_pid = str(pid) if pid else ""
        if pid:
            debug.pid_comm = _read_proc_text(pid, "comm").strip()
            debug.pid_cmdline = _read_proc_text(pid, "cmdline").replace("\x00", " ").strip()
        if not props.is_normal_window or not pid:
            return WindowState(
                window_id=window_id,
                pid=pid,
                window_title=title,
                is_hidden=props.is_hidden,
                source="x11",
            )
        app = _map_foreground_app(wm_class_values, title, pid, self.app_window_keywords)
        return WindowState(
            window_id=window_id,
            app=app,
            pid=pid,
            window_title=title,
            is_hidden=props.is_hidden,
            source="x11",
        )

    def debug_row(self, session_id: str, feature_window_id: int) -> dict[str, object]:
        debug = self.last_debug
        return {
            "session_id": session_id,
            "feature_window_id": feature_window_id,
            "ts_ns": debug.ts_ns,
            "timestamp": debug.timestamp,
            "active_window_id_xdotool": debug.active_window_id_xdotool,
            "active_window_id_xprop_root": debug.active_window_id_xprop_root,
            "chosen_window_id": debug.chosen_window_id,
            "xdotool_window_name": debug.xdotool_window_name,
            "xprop_net_wm_name": debug.xprop_net_wm_name,
            "xprop_wm_name": debug.xprop_wm_name,
            "wm_class": debug.wm_class,
            "net_wm_pid": debug.net_wm_pid,
            "xdotool_pid": debug.xdotool_pid,
            "pid_comm": debug.pid_comm,
            "pid_cmdline": debug.pid_cmdline,
            "mapped_app": debug.mapped_app,
            "previous_foreground_app": debug.previous_foreground_app,
            "foreground_app": debug.foreground_app,
            "window_title": debug.window_title,
            "error": debug.error,
        }


def _read_proc_text(pid: int, name: str) -> str:
    try:
        return (Path("/proc") / str(pid) / name).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _map_foreground_app(
    wm_classes: list[str],
    title: str,
    pid: int,
    app_window_keywords: dict[str, list[str]] | None = None,
) -> str:
    comm = _read_proc_text(pid, "comm").strip()
    cmdline = _read_proc_text(pid, "cmdline").replace("\x00", " ")
    identity_text = " ".join(wm_classes + [comm]).lower()
    fallback_text = " ".join([title, cmdline]).lower()
    # Several desktop names intentionally overlap (for example, LibreOffice
    # Calc versus GNOME Calculator; Mozilla Firefox versus Thunderbird).  The
    # LSAPP fixtures also intentionally share filenames: both VLC/Audacity use
    # audio-test and GIMP/Shotwell/Image Viewer use image-test.  WM_CLASS and
    # /proc/<pid>/comm identify the application, so rank their matches before
    # title/cmdline fallback; within one source retain longest-keyword and
    # configuration-order tie breaking.
    keywords_by_app = app_window_keywords or DEFAULT_WINDOW_KEYWORDS

    def best_match(text: str) -> str:
        matches: list[tuple[int, int, str]] = []
        for app_index, (app_key, keywords) in enumerate(keywords_by_app.items()):
            for keyword in keywords:
                normalized = str(keyword).lower()
                if normalized and normalized in text:
                    matches.append((len(normalized), -app_index, app_key))
        return max(matches)[2] if matches else ""

    identity_match = best_match(identity_text)
    if identity_match:
        return identity_match
    fallback_match = best_match(fallback_text)
    if fallback_match:
        return fallback_match
    return "UNKNOWN"


def _int_property_values(prop: Any | None) -> list[int]:
    """把 python-xlib property 的数组值转换为普通整数列表。"""
    if prop is None:
        return []
    try:
        return [int(value) for value in prop.value]
    except (TypeError, ValueError):
        return []


def _first_int_property_value(prop: Any | None) -> int:
    values = _int_property_values(prop)
    return values[0] if values else 0


def _property_bytes(prop: Any | None) -> bytes:
    if prop is None:
        return b""
    value = prop.value
    if isinstance(value, bytes):
        return value
    if isinstance(value, str):
        return value.encode("utf-8", errors="replace")
    try:
        return value.tobytes()
    except AttributeError:
        try:
            return bytes(value)
        except (TypeError, ValueError):
            return b""


def _decode_x11_bytes(value: bytes) -> str:
    try:
        return value.decode("utf-8")
    except UnicodeDecodeError:
        # 传统 WM_NAME/WM_CLASS 可能仍使用 Latin-1，而 _NET_WM_NAME 通常是 UTF-8。
        return value.decode("latin-1", errors="replace")


def _decode_text_property(prop: Any | None) -> str:
    raw = _property_bytes(prop)
    return _decode_x11_bytes(raw.split(b"\x00", 1)[0]).strip() if raw else ""


def _decode_wm_class(prop: Any | None) -> list[str]:
    raw = _property_bytes(prop)
    if not raw:
        return []
    return [
        _decode_x11_bytes(part).strip()
        for part in raw.split(b"\x00")
        if part.strip()
    ]


def _format_x11_window_id(window_id: int) -> str:
    return f"0x{int(window_id):x}" if int(window_id) else ""


def _same_x11_window_id(left: str, right: str) -> bool:
    try:
        return int(str(left), 0) == int(str(right), 0)
    except ValueError:
        return str(left).lower() == str(right).lower()


def _configure_x11_env() -> None:
    if not os.environ.get("DISPLAY"):
        x11_dir = Path("/tmp/.X11-unix")
        if x11_dir.is_dir():
            for item in sorted(x11_dir.glob("X*")):
                suffix = item.name[1:]
                if suffix.isdigit():
                    os.environ["DISPLAY"] = f":{suffix}"
                    break
    if os.environ.get("XAUTHORITY"):
        return
    runtime_dir = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))
    auths = sorted(runtime_dir.glob(".mutter-Xwaylandauth.*"))
    if auths:
        os.environ["XAUTHORITY"] = str(auths[-1])
        return
    home_auth = Path.home() / ".Xauthority"
    if home_auth.exists():
        os.environ["XAUTHORITY"] = str(home_auth)
