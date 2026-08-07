"""Foreground application collection."""

from __future__ import annotations

import subprocess
import time
import os
from dataclasses import dataclass
import datetime as dt
from pathlib import Path


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


DEFAULT_WINDOW_KEYWORDS = {
    "WPS": ["wps", "wpsoffice", "kingsoft"],
    "QQ": ["linuxqq", "tencent", "腾讯", "qq"],
    "FILES": ["org.gnome.nautilus", "nautilus", "files", "文件", "home", "主文件夹"],
}


class ForegroundCollector:
    def __init__(
        self,
        backend: str = "manual",
        manual_app: str = "",
        manual_pid: int = 0,
        app_window_keywords: dict[str, list[str]] | None = None,
    ) -> None:
        self.backend = backend
        self.manual_app = manual_app
        self.manual_pid = manual_pid
        self.app_window_keywords = app_window_keywords or DEFAULT_WINDOW_KEYWORDS
        self._last_key = ""
        self._last_since = time.monotonic()
        self._last_state = ForegroundState(foreground_app=manual_app, foreground_pid=manual_pid, source="manual")
        self.last_debug = ForegroundDebugState()
        if backend == "x11":
            _configure_x11_env()

    def sample(self) -> ForegroundState:
        previous_app = self._last_state.foreground_app
        if self.backend == "x11":
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
        if self.backend != "x11":
            return []
        return self._sample_x11_windows()

    def resolve_window(self, window_id: str) -> WindowState:
        """Resolve one X11 window when a native event names it.

        This is metadata lookup at event time, not an active-window poll.  It
        is intentionally public so the native event collector can share the
        existing application mapping rules.
        """
        if self.backend != "x11" or not window_id:
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

    def _is_active_window(self, window_id: str) -> bool:
        active = _run_text(["xprop", "-root", "_NET_ACTIVE_WINDOW"])
        if active.returncode != 0:
            return False
        return _same_x11_window_id(window_id, _parse_active_window_from_xprop(active.stdout))

    def _sample_x11(self, previous_app: str) -> tuple[ForegroundState, ForegroundDebugState]:
        ts_ns = time.time_ns()
        debug = ForegroundDebugState(
            ts_ns=ts_ns,
            timestamp=dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            previous_foreground_app=previous_app,
        )
        errors: list[str] = []

        xdotool_active = _run_text(["xdotool", "getactivewindow"])
        if xdotool_active.returncode == 0:
            debug.active_window_id_xdotool = xdotool_active.stdout.strip()
        else:
            errors.append(f"xdotool_getactivewindow={xdotool_active.stderr.strip()}")

        xprop_root = _run_text(["xprop", "-root", "_NET_ACTIVE_WINDOW"])
        if xprop_root.returncode == 0:
            debug.active_window_id_xprop_root = _parse_active_window_from_xprop(xprop_root.stdout)
        else:
            errors.append(f"xprop_root_active={xprop_root.stderr.strip()}")

        chosen = debug.active_window_id_xprop_root or debug.active_window_id_xdotool
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
        try:
            clients = subprocess.run(
                ["xprop", "-root", "_NET_CLIENT_LIST"],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                check=False,
            ).stdout
        except OSError:
            return []
        if "#" not in clients:
            return []
        window_ids = [item.strip() for item in clients.split("#", 1)[-1].split(",") if item.strip()]
        windows: list[WindowState] = []
        for window_id in window_ids:
            window = self._read_x11_window(window_id)
            if window.window_id:
                windows.append(window)
        return windows

    def _top_stacking_window(self) -> WindowState:
        result = _run_text(["xprop", "-root", "_NET_CLIENT_LIST_STACKING"])
        if result.returncode != 0 or "#" not in result.stdout:
            return WindowState()
        window_ids = [item.strip() for item in result.stdout.split("#", 1)[-1].split(",") if item.strip()]
        for window_id in reversed(window_ids):
            debug = ForegroundDebugState(chosen_window_id=window_id)
            window = self._read_x11_window_debug(window_id, debug)
            if window.window_id and window.app != "UNKNOWN" and not window.is_hidden:
                return window
        return WindowState()

    def _read_x11_window(self, window_id: str) -> WindowState:
        debug = ForegroundDebugState(chosen_window_id=window_id)
        return self._read_x11_window_debug(window_id, debug)

    def _read_x11_window_debug(self, window_id: str, debug: ForegroundDebugState) -> WindowState:
        title = ""
        pid = 0
        errors: list[str] = []
        try:
            pid_text = subprocess.run(
                ["xdotool", "getwindowpid", window_id],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                check=False,
            ).stdout.strip()
            if pid_text:
                debug.xdotool_pid = pid_text
                pid = int(pid_text)
        except (OSError, ValueError):
            pid = 0
        try:
            title = subprocess.run(
                ["xdotool", "getwindowname", window_id],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                check=False,
            ).stdout.strip()
            debug.xdotool_window_name = title
        except OSError:
            title = ""
        try:
            props = subprocess.run(
                [
                    "xprop", "-id", window_id, "WM_CLASS", "_NET_WM_NAME", "WM_NAME",
                    "_NET_WM_PID", "_NET_WM_STATE", "_NET_WM_WINDOW_TYPE",
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
        except OSError:
            debug.error = "xprop_id_failed"
            return WindowState()
        if props.returncode != 0:
            errors.append(f"xprop_id={props.stderr.strip()}")
        props_text = props.stdout
        app = ""
        is_hidden = False
        wm_class_values: list[str] = []
        net_wm_name = ""
        wm_name = ""
        is_normal_window = True
        for line in props_text.splitlines():
            if line.startswith("_NET_WM_NAME"):
                net_wm_name = _clean_xprop_value(line)
                debug.xprop_net_wm_name = net_wm_name
            elif line.startswith("WM_NAME"):
                wm_name = _clean_xprop_value(line)
                debug.xprop_wm_name = wm_name
            elif line.startswith("_NET_WM_PID") and not pid:
                debug.net_wm_pid = line.split("=", 1)[-1].strip()
                try:
                    pid = int(debug.net_wm_pid)
                except ValueError:
                    pid = 0
            elif line.startswith("WM_CLASS"):
                wm_class_values = [part.strip().strip('"') for part in line.split("=", 1)[-1].split(",")]
                debug.wm_class = "|".join(wm_class_values)
            elif line.startswith("_NET_WM_STATE"):
                is_hidden = "_NET_WM_STATE_HIDDEN" in line
            elif line.startswith("_NET_WM_WINDOW_TYPE"):
                is_normal_window = "_NET_WM_WINDOW_TYPE_NORMAL" in line
        if not debug.net_wm_pid:
            for line in props_text.splitlines():
                if line.startswith("_NET_WM_PID"):
                    debug.net_wm_pid = line.split("=", 1)[-1].strip()
                    break
        title = title or net_wm_name or wm_name
        if pid:
            debug.pid_comm = _read_proc_text(pid, "comm").strip()
            debug.pid_cmdline = _read_proc_text(pid, "cmdline").replace("\x00", " ").strip()
        if not is_normal_window or not pid:
            return WindowState(
                window_id=window_id,
                pid=pid,
                window_title=title,
                is_hidden=is_hidden,
                source="x11",
            )
        app = _map_foreground_app(wm_class_values, title, pid, self.app_window_keywords)
        debug.error = "; ".join(errors)
        return WindowState(
            window_id=window_id,
            app=app,
            pid=pid,
            window_title=title,
            is_hidden=is_hidden,
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
    text = " ".join(wm_classes + [title, comm, cmdline]).lower()
    # Several desktop names intentionally overlap (for example, LibreOffice
    # Calc versus GNOME Calculator; Mozilla Firefox versus Thunderbird).  A
    # first-match policy makes the outcome depend on JSON order.  Prefer the
    # most specific matched keyword and retain configuration order only as a
    # deterministic tie-breaker.
    matches: list[tuple[int, int, str]] = []
    for app_index, (app_key, keywords) in enumerate((app_window_keywords or DEFAULT_WINDOW_KEYWORDS).items()):
        for keyword in keywords:
            normalized = str(keyword).lower()
            if normalized and normalized in text:
                matches.append((len(normalized), -app_index, app_key))
    if matches:
        return max(matches)[2]
    return "UNKNOWN"


def _run_text(command: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    except OSError as exc:
        return subprocess.CompletedProcess(command, 127, "", str(exc))


def _parse_active_window_from_xprop(output: str) -> str:
    if "#" in output:
        value = output.split("#", 1)[-1].strip().split()[0]
        return value.rstrip(",")
    if "window id" in output.lower():
        return output.rsplit(" ", 1)[-1].strip()
    return ""


def _same_x11_window_id(left: str, right: str) -> bool:
    try:
        return int(str(left), 0) == int(str(right), 0)
    except ValueError:
        return str(left).lower() == str(right).lower()


def _clean_xprop_value(line: str) -> str:
    if "=" not in line:
        return ""
    value = line.split("=", 1)[-1].strip()
    if value.startswith('"') and value.endswith('"'):
        return value[1:-1]
    return value.strip('"')


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
