#!/usr/bin/env python3
"""Linux desktop application automation runner.

The runner executes a JSON scenario with actions such as launching apps,
switching windows, sending keys, clicking, typing, waiting, and closing apps.
It is intentionally thin: Linux desktop automation still depends on the
session's window system permissions and tools such as xdotool/wmctrl.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import mimetypes
import os
import re
import signal
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class AutomationError(RuntimeError):
    pass


@dataclass
class WindowInfo:
    window_id: str
    title: str = ""
    wm_class: str = ""
    pid: int = 0
    pid_comm: str = ""
    cgroup_path: str = ""
    cgroup_unit: str = ""
    mapped_app: str = "UNKNOWN"
    width: int = 0
    height: int = 0
    visible: bool = True


@dataclass
class Context:
    dry_run: bool
    processes: dict[str, subprocess.Popen[Any] | str] = field(default_factory=dict)
    """Tracked processes. Value is either a Popen object or a systemd unit name (str)."""
    clipboard_process: subprocess.Popen[bytes] | None = None
    trace: "TraceWriter | None" = None
    session_id: str = ""
    scenario_id: str = ""
    test_slice: str = ""
    validation_mode: bool = False
    calibration_only: bool = False
    calibration_output_dir: Path | None = None
    screenshot_output_dir: Path | None = None


@dataclass(frozen=True)
class Scenario:
    actions: list[dict[str, Any]]
    keep_alive_after_s: float = 0.0
    validation_mode: bool = False


TRACE_FIELDS = [
    "session_id",
    "ts_ns",
    "ts_iso",
    "timestamp",
    "scenario_id",
    "step_id",
    "phase",
    "action",
    "op_type",
    "event_type",
    "app",
    "app_key",
    "label",
    "status",
    "validation_mode",
    "optional",
    "command",
    "window_match",
    "pid",
    "tgid",
    "cgroup_path",
    "window_id",
    "window_title",
    "error",
    "phase_id",
    "action_id",
    "foreground_epoch_id",
    "operation_domain",
    "operation_id",
    "operation_name",
    "requested_operation",
    "start_time_ns",
    "end_time_ns",
    "duration_ms",
    "side_effect_level",
    "scope_name",
    "command_or_action",
    "error_type",
    "error_message",
    "screenshot_path",
    "metadata_json",
]


class TraceWriter:
    def __init__(self, path: str | Path, session_id: str, scenario_id: str) -> None:
        self.path = Path(path)
        self.session_id = session_id
        self.scenario_id = scenario_id
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._file = self.path.open("w", encoding="utf-8", newline="")
        self._writer = csv.DictWriter(self._file, fieldnames=TRACE_FIELDS)
        self._writer.writeheader()
        self._file.flush()

    def write(self, row: dict[str, Any]) -> None:
        now_ns = time.time_ns()
        payload = {
            "session_id": self.session_id,
            "ts_ns": now_ns,
            "ts_iso": dt.datetime.now().isoformat(timespec="microseconds"),
            "scenario_id": self.scenario_id,
            **row,
        }
        payload.setdefault("timestamp", payload["ts_iso"])
        payload.setdefault("start_time_ns", now_ns)
        payload.setdefault("end_time_ns", now_ns)
        payload.setdefault("duration_ms", "0")
        metadata = payload.get("metadata_json", "")
        if isinstance(metadata, (dict, list)):
            payload["metadata_json"] = json.dumps(metadata, ensure_ascii=False, sort_keys=True)
        self._writer.writerow({field: payload.get(field, "") for field in TRACE_FIELDS})
        self._file.flush()

    def close(self) -> None:
        self._file.flush()
        self._file.close()


# ---------------------------------------------------------------------------
# cgroup helpers (systemd-run --user)
# ---------------------------------------------------------------------------

_SANITIZE_RE = re.compile(r"[^a-zA-Z0-9._-]")


def _sanitize_unit_name(name: str) -> str:
    sanitized = _SANITIZE_RE.sub("-", name).strip("-._")
    return sanitized or "unnamed"


def _systemd_user_available() -> bool:
    try:
        result = subprocess.run(
            ["systemctl", "--user", "list-units"],
            capture_output=True, text=True, timeout=5,
        )
        return result.returncode == 0
    except Exception:
        return False


def _cgroup_available() -> bool:
    return shutil.which("systemd-run") is not None and _systemd_user_available()


# Environment variable names that systemd-run should forward to the service
# so that GUI applications can connect to the display and session bus.
_GUI_ENV_KEYS = frozenset({
    "DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY",
    "DBUS_SESSION_BUS_ADDRESS", "XDG_RUNTIME_DIR",
    "XDG_SESSION_TYPE", "XDG_CURRENT_DESKTOP",
    "GDK_BACKEND", "QT_QPA_PLATFORM", "MOZ_ENABLE_WAYLAND",
    "GTK_MODULES", "DESKTOP_AUTOPLAY",
    "LANG", "LANGUAGE", "LC_ALL", "LC_CTYPE",
    "HOME", "USER", "LOGNAME", "PATH",
})


def _sanitize_unit(name: str) -> str:
    return f"automation-{_sanitize_unit_name(name)}"


def _inject_env(args: list[str], env: dict[str, str]) -> list[str]:
    """Prepend ``--setenv=KEY=VALUE`` arguments for every GUI-relevant env var."""
    result = list(args)
    for key in sorted(env):
        if key in _GUI_ENV_KEYS and env[key]:
            result.insert(1, f"--setenv={key}={env[key]}")
    return result


def _clear_stale_scope(unit: str) -> None:
    """Release a systemd scope left by an interrupted previous run."""
    if not _systemd_user_available():
        return
    subprocess.run(
        ["systemctl", "--user", "stop", unit],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    subprocess.run(
        ["systemctl", "--user", "reset-failed", unit],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    time.sleep(0.2)


def _cgroup_launch(command: str, name: str, env: dict[str, str], test_slice: str = "") -> str:
    """Launch *command* (simple, no shell syntax) in a systemd scope.

    Uses ``--scope`` so the cgroup stays alive as long as **any** child
    process is running — essential for GUI apps that daemonise on startup.

    If *test_slice* is set, the scope is placed under that slice via ``--slice``.
    Returns the unit name (``.scope`` suffix).
    """
    unit_name = _sanitize_unit(name)
    unit = f"{unit_name}.scope"
    _clear_stale_scope(unit)
    base = ["systemd-run", "--user", "--scope", f"--unit={unit_name}"]
    if test_slice:
        base.append(f"--slice={test_slice}")
    args = _inject_env(base, env)
    args.extend(shlex.split(command))
    # systemd-run --scope blocks as long as the scope has processes.
    # Run in the background so the automation can continue.
    subprocess.Popen(args, env=env)
    return unit


def _cgroup_launch_shell(command: str, name: str, env: dict[str, str], test_slice: str = "") -> str:
    """Launch a full *shell* command in a systemd scope.

    ``nohup`` and trailing ``&`` are stripped — systemd already daemonises.
    If *test_slice* is set, the scope is placed under that slice via ``--slice``.
    """
    cmd = command.strip()
    if cmd.startswith("nohup "):
        cmd = cmd[len("nohup "):]
    if cmd.endswith("&"):
        cmd = cmd.rstrip("&").rstrip()
    unit_name = _sanitize_unit(name)
    unit = f"{unit_name}.scope"
    _clear_stale_scope(unit)
    base = ["systemd-run", "--user", "--scope", f"--unit={unit_name}"]
    if test_slice:
        base.append(f"--slice={test_slice}")
    args = _inject_env(base, env)
    args.extend(["sh", "-c", cmd])
    subprocess.Popen(args, env=env)
    return unit


def _cgroup_stop(unit: str) -> None:
    """Stop a systemd user unit (scope or service) — kills every process in its cgroup."""
    subprocess.run(
        ["systemctl", "--user", "stop", "--no-block", unit],
        check=False, capture_output=True,
    )


def _cgroup_kill(unit: str, sig: str = "SIGTERM") -> None:
    """Send a signal to every process in the unit's cgroup."""
    subprocess.run(
        ["systemctl", "--user", "kill", unit, f"--signal={sig}"],
        check=False, capture_output=True,
    )


def _cgroup_unit_active(unit: str) -> bool:
    """Return True if the systemd unit is still active."""
    result = subprocess.run(
        ["systemctl", "--user", "is-active", unit],
        check=False, capture_output=True, text=True,
    )
    return result.stdout.strip() == "active"


def log(message: str) -> None:
    print(f"[automation] {message}", flush=True)


def infer_app(action: dict[str, Any]) -> str:
    explicit = str(action.get("app_key", "")).strip()
    if explicit:
        return explicit
    text = " ".join(
        str(action.get(key, ""))
        for key in ("name", "class", "command", "shell_command", "title")
    ).lower()
    if (
        "wps" in text
        or "wpsoffice" in text
        or "kingsoft" in text
        or re.search(r"(^|[| /])wpp($|[| /])", text)
        or re.search(r"(^|[| /])et($|[| /])", text)
    ):
        return "WPS"
    if "linuxqq" in text or "qq" in text:
        return "QQ"
    if "nautilus" in text or "files" in text:
        return "FILES"
    if "bilibili" in text or "哔哩哔哩" in text:
        return "BILIBILI"
    if "firefox" in text:
        return "FIREFOX"
    if "wechat" in text or "weixin" in text or "微信" in text:
        return "WECHAT"
    if "thunderbird" in text:
        return "THUNDERBIRD"
    if "telegram" in text:
        return "TELEGRAM"
    if re.search(r"(^|[| /])vlc($|[| /])", text) or "videolan" in text:
        return "VLC"
    if "gimp" in text:
        return "GIMP"
    if "audacity" in text:
        return "AUDACITY"
    if "evince" in text or "document viewer" in text or "文档查看器" in text:
        return "EVINCE"
    if "gnome-calculator" in text or "calculator" in text or "计算器" in text:
        return "CALCULATOR"
    if "keepassxc" in text or "keepass" in text:
        return "KEEPASSXC"
    if (
        "libreoffice" in text
        or "soffice" in text
        or re.search(r"(^|[| /])lo(?:writer|calc|impress)($|[| /])", text)
    ):
        return "LIBREOFFICE"
    return "UNKNOWN"


def default_label(action: dict[str, Any], app: str) -> str:
    action_type = str(action.get("type", "")).strip()
    if action.get("label"):
        return str(action["label"])
    if action_type == "launch" and app == "WPS":
        return "WPS_LAUNCH"
    if action_type == "shell" and app == "QQ":
        return "QQ_LAUNCH"
    if action_type == "shell" and app == "FILES":
        return "FILES_LAUNCH"
    if action_type == "switch" and app in {"WPS", "QQ", "FILES"}:
        return f"APP_SWITCH_{app}"
    if action_type == "focus" and app == "WPS":
        return "APP_FOCUS_WPS"
    if action_type == "close" and app == "WPS":
        return "WPS_CLOSE"
    if action_type == "close" and app == "QQ":
        return "QQ_CLOSE"
    if action_type == "close" and app == "FILES":
        return "FILES_CLOSE"
    if action_type == "close" and app == "BILIBILI":
        return "BILIBILI_CLOSE"
    if action_type in {"switch", "focus", "verify_foreground"} and app == "BILIBILI":
        return f"APP_{action_type.upper()}_BILIBILI"
    if action_type == "gnome_search_focus":
        return f"APP_GNOME_SEARCH_FOCUS_{app}"
    if action_type in {"key", "hotkey"}:
        return "APP_KEY"
    if action_type == "wait":
        return "WAIT"
    if action_type == "open_file":
        return "WPS_OPEN_FILE"
    if action_type == "save_as":
        return "WPS_SAVE_AS"
    if action_type == "window_state":
        return "WPS_WINDOW_STATE"
    if action_type == "scroll":
        return "WPS_SCROLL"
    if action_type in {"paste_text", "clipboard_file"}:
        return "WPS_CLIPBOARD"
    return f"ACTION_{action_type.upper() or 'UNKNOWN'}"


def action_command(action: dict[str, Any]) -> str:
    for key in ("command", "shell_command", "key", "text", "search_text"):
        value = action.get(key)
        if value:
            return str(value)
    return ""


def _semantic_trace_fields(action: dict[str, Any]) -> dict[str, Any]:
    """Return optional semantic fields without imposing them on legacy actions."""
    metadata = action.get("metadata", action.get("metadata_json", ""))
    return {
        "phase_id": action.get("phase_id", ""),
        "action_id": action.get("action_id", ""),
        "foreground_epoch_id": action.get("foreground_epoch_id", ""),
        "operation_domain": action.get("operation_domain", ""),
        "operation_id": action.get("operation_id", ""),
        "operation_name": action.get("operation_name", ""),
        "requested_operation": action.get("requested_operation", ""),
        "side_effect_level": action.get("side_effect_level", ""),
        "scope_name": action.get("scope_name", ""),
        "command_or_action": action.get("command_or_action", action_command(action)),
        "screenshot_path": action.get("screenshot_path", ""),
        "metadata_json": metadata,
    }


def action_window_match(action: dict[str, Any]) -> str:
    parts = []
    if action.get("class"):
        parts.append(f"class={action['class']}")
    if action.get("title"):
        parts.append(f"title={action['title']}")
    if action.get("name"):
        parts.append(f"name={action['name']}")
    return ";".join(parts)


def proc_meta_for_pid(pid: int) -> dict[str, str]:
    if pid <= 0:
        return {}
    status = read_proc_text(Path("/proc") / str(pid) / "status")
    tgid = ""
    for line in status.splitlines():
        if line.startswith("Tgid:"):
            tgid = line.split(":", 1)[1].strip()
            break
    cgroup_path = read_proc_text(Path("/proc") / str(pid) / "cgroup")
    return {"pid": str(pid), "tgid": tgid, "cgroup_path": cgroup_path.replace("\n", "|")}


def cgroup_unit_from_path(path: str) -> str:
    for part in reversed(path.replace("|", "/").strip("/").split("/")):
        if "." in part:
            return part
    return ""


def map_window_app(title: str, wm_class: str, pid_comm: str, cgroup_path: str = "") -> str:
    text = " ".join([title, wm_class, pid_comm, cgroup_path]).lower()
    process_tokens = {
        token
        for token in re.split(r"[|, /\\]+", " ".join([wm_class, pid_comm, cgroup_path]).lower())
        if token
    }
    if (
        "wps" in text
        or "wpsoffice" in text
        or "kingsoft" in text
        or process_tokens.intersection({"wpp", "et", "wpspdf"})
    ):
        return "WPS"
    if "linuxqq" in text or "tencent" in text or "腾讯" in text or "qq" in text:
        return "QQ"
    if (
        "org.gnome.nautilus" in text
        or "nautilus" in text
        or "pcmanfm" in text
        or "files" in text
        or "文件" in text
        or "home" in text
        or "主文件夹" in text
    ):
        return "FILES"
    if "bilibili" in text or "哔哩哔哩" in text:
        return "BILIBILI"
    if "firefox" in text:
        return "FIREFOX"
    if "thunderbird" in text:
        return "THUNDERBIRD"
    if "telegram" in text:
        return "TELEGRAM"
    if "vlc" in process_tokens or "videolan" in text:
        return "VLC"
    if any(token.startswith("gimp") for token in process_tokens) or "gnu image manipulation" in text:
        return "GIMP"
    if "audacity" in text:
        return "AUDACITY"
    if "evince" in text or "document viewer" in text or "文档查看器" in text:
        return "EVINCE"
    if "gnome-calculator" in text or "calculator" in text or "计算器" in text:
        return "CALCULATOR"
    if "keepassxc" in text or "keepass" in text:
        return "KEEPASSXC"
    if (
        "libreoffice" in text
        or "soffice" in text
        or process_tokens.intersection({"lowriter", "localc", "loimpress", "soffice.bin"})
    ):
        return "LIBREOFFICE"
    return "UNKNOWN"


def read_window_info(window_id: str) -> WindowInfo:
    info = WindowInfo(window_id=window_id)
    try:
        title = subprocess.run(
            ["xdotool", "getwindowname", window_id],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
        ).stdout.strip()
        info.title = title
    except OSError:
        pass
    props = subprocess.run(
        ["xprop", "-id", window_id, "WM_CLASS", "_NET_WM_NAME", "WM_NAME", "_NET_WM_PID"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    for line in props.stdout.splitlines():
        if line.startswith("WM_CLASS") and "=" in line:
            info.wm_class = "|".join(part.strip().strip('"') for part in line.split("=", 1)[1].split(","))
        elif (line.startswith("_NET_WM_NAME") or line.startswith("WM_NAME")) and not info.title and "=" in line:
            info.title = line.split("=", 1)[1].strip().strip('"')
        elif line.startswith("_NET_WM_PID") and "=" in line:
            try:
                info.pid = int(line.split("=", 1)[1].strip())
            except ValueError:
                info.pid = 0
    if not info.pid:
        try:
            pid_text = subprocess.run(
                ["xdotool", "getwindowpid", window_id],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                check=False,
            ).stdout.strip()
            info.pid = int(pid_text) if pid_text else 0
        except (OSError, ValueError):
            info.pid = 0
    if info.pid:
        info.pid_comm = read_proc_text(Path("/proc") / str(info.pid) / "comm")
        info.cgroup_path = read_proc_text(Path("/proc") / str(info.pid) / "cgroup")
        info.cgroup_unit = cgroup_unit_from_path(info.cgroup_path)
    try:
        geo = subprocess.run(
            ["xdotool", "getwindowgeometry", "--shell", window_id],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
        ).stdout
        for line in geo.splitlines():
            if line.startswith("WIDTH="):
                info.width = int(line.split("=", 1)[1])
            elif line.startswith("HEIGHT="):
                info.height = int(line.split("=", 1)[1])
    except (OSError, ValueError):
        pass
    info.mapped_app = map_window_app(info.title, info.wm_class, info.pid_comm, info.cgroup_path)
    return info


def format_window_info(info: WindowInfo) -> str:
    return (
        f"id={info.window_id} title={info.title!r} class={info.wm_class!r} "
        f"pid={info.pid} comm={info.pid_comm!r} unit={info.cgroup_unit!r} "
        f"mapped={info.mapped_app} size={info.width}x{info.height}"
    )


def list_window_candidates(action: dict[str, Any], app: str = "") -> list[WindowInfo]:
    if not shutil.which("xdotool"):
        raise AutomationError("需要安装 xdotool")
    title = get_str(action, "title")
    window_class = get_str(action, "class")
    name = get_str(action, "name")
    pid_cmdline_contains = get_str(action, "pid_cmdline_contains")
    allowed_pids: set[int] | None = None
    if pid_cmdline_contains:
        allowed_pids = set()
        marker = pid_cmdline_contains.encode("utf-8")
        for proc_dir in Path("/proc").glob("[0-9]*"):
            try:
                if marker in (proc_dir / "cmdline").read_bytes():
                    allowed_pids.add(int(proc_dir.name))
            except (FileNotFoundError, PermissionError, ProcessLookupError, ValueError):
                continue
    visibility = [] if bool(action.get("include_hidden")) else ["--onlyvisible"]
    queries: list[list[str]] = []
    if window_class:
        queries.append(["xdotool", "search", *visibility, "--class", window_class])
    if title:
        queries.append(["xdotool", "search", *visibility, "--name", title])
    if name and not window_class and not title:
        queries.append(["xdotool", "search", *visibility, "--name", name])
    if allowed_pids is not None:
        for pid in sorted(allowed_pids):
            queries.append(["xdotool", "search", *visibility, "--pid", str(pid)])
    # Some applications have historically needed broad fallback discovery when
    # their WM_CLASS changes between releases.  An isolated test window must be
    # able to opt out: adding generic QQ candidates after a unique class match
    # can otherwise select an unrelated, already-running personal QQ window.
    if app == "QQ" and not bool(action.get("strict_window_match")):
        queries.extend([
            ["xdotool", "search", *visibility, "--class", "qq|QQ|linuxqq"],
            ["xdotool", "search", *visibility, "--name", "QQ"],
        ])
    seen: set[str] = set()
    ids: list[str] = []
    for query in queries:
        result = subprocess.run(query, text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False)
        if result.returncode == 0:
            for window_id in result.stdout.splitlines():
                window_id = window_id.strip()
                if window_id and window_id not in seen:
                    seen.add(window_id)
                    ids.append(window_id)
    candidates = [read_window_info(window_id) for window_id in ids]
    if allowed_pids is not None:
        candidates = [candidate for candidate in candidates if candidate.pid in allowed_pids]
    return candidates


def best_window_candidate(action: dict[str, Any], app: str) -> WindowInfo | None:
    candidates = list_window_candidates(action, app)
    if not candidates:
        return None
    prefer_content = bool(action.get("prefer_content_window"))
    prefer_smallest = bool(action.get("prefer_smallest_window"))
    prefer_outer = bool(action.get("prefer_outer_window"))
    content_titles = {"wpsoffice", "wps", "wpp", "et", "wpspdf"}
    indexed = list(enumerate(candidates))
    indexed.sort(
        key=lambda item: (
            item[1].mapped_app == app,
            prefer_outer and (
                "wps office" in item[1].title.strip().lower()
                or item[1].title.strip().lower().endswith(" - wps")
            ),
            -(item[1].width * item[1].height) if prefer_smallest else 0,
            prefer_content and item[1].title.strip().lower() in content_titles,
            bool(item[1].title),
            0 if prefer_smallest else item[1].width * item[1].height,
            item[1].cgroup_unit == f"automation-{app.lower()}.scope",
            item[0],
        ),
        reverse=True,
    )
    return indexed[0][1]


def get_foreground_window_info() -> WindowInfo:
    active_id = ""
    result = subprocess.run(
        ["xdotool", "getactivewindow"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if result.returncode == 0:
        active_id = result.stdout.strip()
    active = read_window_info(active_id) if active_id else WindowInfo("")
    stacking = subprocess.run(
        ["xprop", "-root", "_NET_CLIENT_LIST_STACKING"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if "#" in stacking.stdout:
        ids = [item.strip() for item in stacking.stdout.split("#", 1)[1].split(",") if item.strip()]
        for window_id in reversed(ids):
            info = read_window_info(window_id)
            if info.mapped_app != "UNKNOWN":
                return info
    return active


def quiet_window_trace(action: dict[str, Any]) -> dict[str, str]:
    display = os.environ.get("DISPLAY")
    if not display or shutil.which("xdotool") is None:
        return {}
    title = get_str(action, "title")
    window_class = get_str(action, "class")
    name = get_str(action, "name")
    queries: list[list[str]] = []
    if window_class:
        queries.append(["xdotool", "search", "--onlyvisible", "--class", window_class])
    if title:
        queries.append(["xdotool", "search", "--onlyvisible", "--name", title])
    if name:
        queries.append(["xdotool", "search", "--onlyvisible", "--name", name])
    window_id = ""
    for query in queries:
        result = subprocess.run(query, text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False)
        if result.returncode == 0 and result.stdout.strip():
            window_id = result.stdout.strip().splitlines()[-1]
            break
    if not window_id:
        return {}
    out = {"window_id": window_id}
    props = subprocess.run(
        ["xprop", "-id", window_id, "_NET_WM_PID", "_NET_WM_NAME", "WM_NAME"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    ).stdout
    pid = 0
    for line in props.splitlines():
        # A close action can race the X server: xprop then reports a textual
        # "not found" line with the property prefix but no '='.  Trace
        # collection is best-effort and must not invalidate an otherwise
        # completed calibration session in that case.
        if line.startswith("_NET_WM_PID") and "=" in line:
            try:
                pid = int(line.split("=", 1)[1].strip())
            except (IndexError, ValueError):
                pid = 0
        elif line.startswith("_NET_WM_NAME") or line.startswith("WM_NAME"):
            if "=" in line and not out.get("window_title"):
                out["window_title"] = line.split("=", 1)[1].strip().strip('"')
    out.update(proc_meta_for_pid(pid))
    return out


def trace_marker(
    ctx: Context,
    *,
    event_type: str,
    status: str,
    step_id: int = 0,
    action: dict[str, Any] | None = None,
    op_type: str = "",
    error: str = "",
) -> None:
    if ctx.trace is None:
        return
    action = action or {}
    app = infer_app(action)
    row: dict[str, Any] = {
        "step_id": step_id,
        "phase": "marker",
        "action": get_str(action, "type"),
        "op_type": op_type or get_str(action, "type"),
        "event_type": event_type,
        "app": app,
        "app_key": app,
        "label": default_label(action, app),
        "status": status,
        "validation_mode": str(ctx.validation_mode).lower(),
        "optional": "true" if bool(action.get("optional")) else "false",
        "command": action_command(action),
        "window_match": action_window_match(action),
        "error": error,
        "error_type": "AutomationError" if error else "",
        "error_message": error,
    }
    row.update(_semantic_trace_fields(action))
    ctx.trace.write(row)


def trace_action(ctx: Context, step_id: int, phase: str, status: str, action: dict[str, Any], error: str = "") -> None:
    if ctx.trace is None:
        return
    app = infer_app(action)
    event_type = "OP_START" if phase == "start" else ("OP_FAILED" if status == "failed" else "OP_DONE")
    row: dict[str, Any] = {
        "step_id": step_id,
        "phase": phase,
        "action": get_str(action, "type"),
        "op_type": get_str(action, "type"),
        "event_type": event_type,
        "app": app,
        "app_key": app,
        "label": default_label(action, app),
        "status": status,
        "validation_mode": str(ctx.validation_mode).lower(),
        "optional": "true" if bool(action.get("optional")) else "false",
        "command": action_command(action),
        "window_match": action_window_match(action),
        "error": error,
        "error_type": "AutomationError" if error else "",
        "error_message": error,
    }
    row.update(_semantic_trace_fields(action))
    if phase == "end":
        row.update(quiet_window_trace(action))
        name = get_str(action, "name")
        tracked = ctx.processes.get(name) if name else None
        if isinstance(tracked, str):
            row.setdefault("cgroup_path", tracked)
    ctx.trace.write(row)


def trace_marker_action(action: dict[str, Any], ctx: Context) -> None:
    """Write a semantic trace event only; this action intentionally has no UI effect."""
    event_type = get_str(action, "event_type")
    if not event_type:
        raise AutomationError("trace_marker 需要 event_type")
    trace_marker(
        ctx,
        event_type=event_type,
        status=get_str(action, "status", "success"),
        step_id=get_int(action, "step_id", 0),
        action=action,
        op_type=get_str(action, "op_type", "trace_marker"),
        error=get_str(action, "error_message", ""),
    )


def require_tool(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise AutomationError(f"需要安装 {name}。Ubuntu/Debian 可执行：sudo apt install {name}")
    return path


def detect_display() -> str:
    if os.environ.get("DISPLAY"):
        return str(os.environ["DISPLAY"])
    x11_dir = Path("/tmp/.X11-unix")
    if not x11_dir.is_dir():
        return ""
    for item in sorted(x11_dir.glob("X*")):
        suffix = item.name[1:]
        if suffix.isdigit():
            return f":{suffix}"
    return ""


def detect_xauthority(display: str) -> str:
    runtime_dir = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))
    mutter_auths = sorted(runtime_dir.glob(".mutter-Xwaylandauth.*"))
    if mutter_auths:
        return str(mutter_auths[-1])

    if display:
        try:
            ps_output = subprocess.run(
                ["ps", "-eo", "args"],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                check=False,
            ).stdout
        except OSError:
            ps_output = ""
        for line in ps_output.splitlines():
            if "Xwayland" not in line or display not in line:
                continue
            parts = shlex.split(line)
            for index, part in enumerate(parts):
                if part == "-auth" and index + 1 < len(parts):
                    return parts[index + 1]

    candidate = Path.home() / ".Xauthority"
    if candidate.exists():
        return str(candidate)
    return ""


def configure_display(display: str = "", xauthority: str = "") -> None:
    chosen_display = display or detect_display()
    if chosen_display and not os.environ.get("DISPLAY"):
        os.environ["DISPLAY"] = chosen_display
        log(f"DISPLAY={chosen_display}")
    elif display:
        os.environ["DISPLAY"] = display
        log(f"DISPLAY={display}")

    chosen_xauthority = xauthority or os.environ.get("XAUTHORITY", "") or detect_xauthority(chosen_display)
    if chosen_xauthority:
        os.environ["XAUTHORITY"] = chosen_xauthority
        log(f"XAUTHORITY={chosen_xauthority}")


def require_xdotool() -> None:
    require_tool("xdotool")
    if not os.environ.get("DISPLAY"):
        raise AutomationError(
            "当前终端没有 DISPLAY，xdotool 无法连接图形会话。"
            "请在桌面终端运行，或传入 --display :0；如果是 Wayland 原生会话，xdotool 可能无法控制原生窗口。"
        )


def run(args: list[str], ctx: Context, *, check: bool = True) -> subprocess.CompletedProcess[str] | None:
    rendered = " ".join(shlex.quote(part) for part in args)
    if ctx.dry_run:
        log(f"dry-run: {rendered}")
        return None
    log(rendered)
    return subprocess.run(args, check=check, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def run_shell(command: str, ctx: Context, *, check: bool = True) -> subprocess.CompletedProcess[str] | None:
    if ctx.dry_run:
        log(f"dry-run: {command}")
        return None
    log(command)
    return subprocess.run(command, shell=True, check=check, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def get_str(action: dict[str, Any], key: str, default: str = "") -> str:
    value = action.get(key, default)
    if value is None:
        return default
    if not isinstance(value, str):
        raise AutomationError(f"{key} 必须是字符串")
    return value


def get_float(action: dict[str, Any], key: str, default: float = 0.0) -> float:
    try:
        return float(action.get(key, default))
    except (TypeError, ValueError) as exc:
        raise AutomationError(f"{key} 必须是数字") from exc


def get_int(action: dict[str, Any], key: str, default: int = 0) -> int:
    try:
        return int(action.get(key, default))
    except (TypeError, ValueError) as exc:
        raise AutomationError(f"{key} 必须是整数") from exc


def get_str_list(action: dict[str, Any], key: str) -> list[str]:
    value = action.get(key, [])
    if value in (None, ""):
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return value
    raise AutomationError(f"{key} 必须是字符串或字符串数组")


_VARIABLE_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def parse_var_overrides(values: list[str]) -> dict[str, str]:
    overrides: dict[str, str] = {}
    for value in values:
        if "=" not in value:
            raise AutomationError(f"--var 必须使用 NAME=VALUE 格式：{value}")
        name, raw = value.split("=", 1)
        name = name.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise AutomationError(f"变量名无效：{name}")
        overrides[name] = raw
    return overrides


def expand_scenario_value(value: Any, variables: dict[str, str]) -> Any:
    if isinstance(value, str):
        expanded = value
        for _ in range(10):
            replaced = _VARIABLE_RE.sub(
                lambda match: variables.get(match.group(1), match.group(0)), expanded
            )
            if replaced == expanded:
                break
            expanded = replaced
        unresolved = sorted(set(_VARIABLE_RE.findall(expanded)))
        if unresolved:
            raise AutomationError(f"未设置场景变量：{', '.join(unresolved)}")
        return os.path.expanduser(expanded)
    if isinstance(value, list):
        return [expand_scenario_value(item, variables) for item in value]
    if isinstance(value, dict):
        return {key: expand_scenario_value(item, variables) for key, item in value.items()}
    return value


def _expand_actions(actions: list[Any], *, prefix: str = "") -> list[dict[str, Any]]:
    expanded: list[dict[str, Any]] = []
    for index, action in enumerate(actions, start=1):
        if not isinstance(action, dict):
            raise AutomationError(f"{prefix}第 {index} 个 action 必须是对象")
        action_type = str(action.get("type", "")).strip()
        if action_type == "repeat":
            times = get_int(action, "times", get_int(action, "count", 1))
            nested = action.get("actions", [])
            if times < 1:
                continue
            if not isinstance(nested, list):
                raise AutomationError(f"{prefix}repeat.actions 必须是 action 数组")
            for repeat_index in range(1, times + 1):
                for nested_action in _expand_actions(nested, prefix=f"{prefix}repeat {index}: "):
                    copied = dict(nested_action)
                    copied.setdefault("_repeat_index", repeat_index)
                    copied.setdefault("_repeat_total", times)
                    expanded.append(copied)
            continue
        expanded.append(dict(action))
    return expanded


def load_scenario(path: Path, cli_variables: dict[str, str] | None = None) -> Scenario:
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, list):
        raw_actions = data
        keep_alive_after_s = 0.0
        validation_mode = False
    elif isinstance(data, dict) and isinstance(data.get("actions"), list):
        raw_actions = data["actions"]
        raw_variables = data.get("variables", {})
        if not isinstance(raw_variables, dict):
            raise AutomationError("场景 variables 必须是对象")
        try:
            keep_alive_after_s = float(data.get("keep_alive_after_s", 0.0) or 0.0)
        except (TypeError, ValueError) as exc:
            raise AutomationError("keep_alive_after_s 必须是数字") from exc
        validation_mode = bool(data.get("validation_mode", False))
    else:
        raise AutomationError("场景文件必须是 action 数组，或包含 actions 数组的对象")
    if isinstance(data, list):
        raw_variables = {}
    workspace_root = path.resolve().parent.parent.parent
    variables = {
        "HOME": str(Path.home()),
        "USER": os.environ.get("USER", ""),
        "PROJECT_ROOT": str(workspace_root),
        "SCENARIO_DIR": str(path.resolve().parent),
        **{key: str(value) for key, value in raw_variables.items()},
        **os.environ,
        **(cli_variables or {}),
    }
    raw_actions = expand_scenario_value(raw_actions, variables)
    actions = _expand_actions(raw_actions)
    for index, action in enumerate(actions, start=1):
        if not isinstance(action, dict):
            raise AutomationError(f"第 {index} 个 action 必须是对象")
    return Scenario(
        actions=actions,
        keep_alive_after_s=keep_alive_after_s,
        validation_mode=validation_mode,
    )


def keep_alive(seconds: float, ctx: Context) -> None:
    if seconds <= 0:
        return
    trace_marker(ctx, event_type="KEEP_ALIVE_START", status="running", op_type="keep_alive")
    log(f"keep_alive_after_s {seconds:g}s")
    if not ctx.dry_run:
        time.sleep(seconds)
    trace_marker(ctx, event_type="KEEP_ALIVE_DONE", status="success", op_type="keep_alive")


def launch(action: dict[str, Any], ctx: Context) -> None:
    command = get_str(action, "command")
    name = get_str(action, "name") or command
    scope_name = get_str(action, "scope_name") or name
    if not command:
        raise AutomationError("launch 需要 command")
    if ctx.dry_run:
        log(f"dry-run: launch {command}")
        return
    env = os.environ.copy()
    if _cgroup_available():
        unit_name = _cgroup_launch(command, scope_name, env, test_slice=ctx.test_slice)
        log(f"launch {command}  (cgroup: {unit_name})")
        ctx.processes[name] = unit_name
    else:
        log(f"launch {command}")
        proc = subprocess.Popen(shlex.split(command), env=env)
        ctx.processes[name] = proc


def wait(action: dict[str, Any], ctx: Context) -> None:
    seconds = get_float(action, "seconds", 1.0)
    log(f"wait {seconds:g}s")
    if not ctx.dry_run:
        time.sleep(seconds)


def key(action: dict[str, Any], ctx: Context) -> None:
    value = get_str(action, "key")
    if not value:
        raise AutomationError("key 需要 key，例如 ctrl+o 或 Escape")
    repeat = max(1, get_int(action, "repeat", 1))
    interval = max(0.0, get_float(action, "interval", 0.1))
    if not ctx.dry_run:
        require_xdotool()
    for index in range(repeat):
        run(["xdotool", "key", "--clearmodifiers", value], ctx)
        if index + 1 < repeat and not ctx.dry_run and interval:
            time.sleep(interval)


def type_text(action: dict[str, Any], ctx: Context) -> None:
    text = get_str(action, "text")
    delay = str(get_int(action, "delay_ms", 20))
    repeat = max(1, get_int(action, "repeat", 1))
    if not ctx.dry_run:
        require_xdotool()
    for _ in range(repeat):
        run(["xdotool", "type", "--clearmodifiers", "--delay", delay, text], ctx)


def _run_ydotool(args: list[str], ctx: Context) -> None:
    """Send compositor-level input when X11 window activation is unavailable."""
    candidates: list[list[str]] = []
    if shutil.which("sudo") and shutil.which("ydotool"):
        candidates.append(["sudo", "-n", "ydotool", *args])
    if shutil.which("ydotool"):
        candidates.append(["ydotool", *args])
    if not candidates:
        raise AutomationError("需要安装 ydotool，并允许访问 /dev/uinput")
    last_error = ""
    for command in candidates:
        result = run(command, ctx, check=False)
        if ctx.dry_run or (result is not None and result.returncode == 0):
            return
        last_error = (result.stderr or result.stdout or "").strip()
    raise AutomationError(f"ydotool 输入失败：{last_error or 'unknown error'}")


def gnome_search_focus(action: dict[str, Any], ctx: Context) -> None:
    """Focus an app through GNOME's compositor-level application search."""
    app = infer_app(action)
    search_text = get_str(action, "search_text")
    if not search_text:
        raise AutomationError("gnome_search_focus 需要 search_text")
    if ctx.dry_run:
        log(f"dry-run: GNOME search focus {app} ({search_text})")
        return
    _run_ydotool(["key", "super"], ctx)
    time.sleep(get_float(action, "search_open_delay_s", 0.35))
    _run_ydotool(["type", "--delay", str(get_int(action, "type_delay_ms", 35)), search_text], ctx)
    time.sleep(get_float(action, "search_type_delay_s", 0.25))
    _run_ydotool(["key", "enter"], ctx)
    time.sleep(get_float(action, "wait_after_focus_s", 1.0))
    if bool(action.get("verify", True)):
        active = get_foreground_window_info()
        if active.mapped_app != app:
            raise AutomationError(
                f"GNOME search focus failed: expected={app} active={format_window_info(active)}"
            )
        log(f"GNOME search focus {app} verified: {format_window_info(active)}")


def _stop_clipboard_process(ctx: Context) -> None:
    process = ctx.clipboard_process
    ctx.clipboard_process = None
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=1)


def _set_clipboard(payload: bytes, mime: str, ctx: Context) -> None:
    if ctx.dry_run:
        log(f"dry-run: set clipboard ({mime}, {len(payload)} bytes)")
        return
    _stop_clipboard_process(ctx)
    require_tool("xclip")
    process = subprocess.Popen(
        ["xclip", "-selection", "clipboard", "-target", mime, "-in", "-silent"],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if process.stdin is None:
        process.terminate()
        raise AutomationError("无法打开 xclip 标准输入")
    try:
        process.stdin.write(payload)
        process.stdin.close()
    except (BrokenPipeError, OSError) as exc:
        process.terminate()
        raise AutomationError(f"xclip 写入失败：{exc}") from exc
    time.sleep(0.2)
    if process.poll() not in (None, 0):
        raise AutomationError(f"xclip 启动失败，退出码 {process.returncode}")
    ctx.clipboard_process = process


def paste_text(action: dict[str, Any], ctx: Context) -> None:
    text = get_str(action, "text")
    repeat = max(1, get_int(action, "repeat", 1))
    separator = get_str(action, "separator")
    _set_clipboard(separator.join(text for _ in range(repeat)).encode("utf-8"), "UTF8_STRING", ctx)
    key({"key": get_str(action, "paste_key", "ctrl+v")}, ctx)


def clipboard_file(action: dict[str, Any], ctx: Context) -> None:
    path = Path(get_str(action, "path"))
    if not ctx.dry_run and not path.is_file():
        raise AutomationError(f"剪贴板文件不存在：{path}")
    mime = get_str(action, "mime") or mimetypes.guess_type(str(path))[0] or "application/octet-stream"
    _set_clipboard(path.read_bytes() if not ctx.dry_run else b"", mime, ctx)


def assert_file(action: dict[str, Any], ctx: Context) -> None:
    path = Path(get_str(action, "path"))
    if ctx.dry_run:
        log(f"dry-run: require file {path}")
        return
    if not path.is_file():
        raise AutomationError(f"所需样本文件不存在：{path}")
    log(f"sample file ready: {path}")


def click(action: dict[str, Any], ctx: Context) -> None:
    x = str(get_int(action, "x"))
    y = str(get_int(action, "y"))
    button = str(get_int(action, "button", 1))
    if not ctx.dry_run:
        require_xdotool()
    run(["xdotool", "mousemove", x, y, "click", button], ctx)


def click_window(action: dict[str, Any], ctx: Context) -> None:
    app = infer_app(action)
    button = str(get_int(action, "button", 1))
    if ctx.dry_run:
        log(f"dry-run: click_window {app}")
        return
    require_xdotool()
    candidate = best_window_candidate(action, app)
    if candidate is None:
        candidates = list_window_candidates(action, app)
        raise AutomationError(f"click_window {app}: no candidate; candidates={[format_window_info(item) for item in candidates]}")
    x = get_int(action, "x", -1)
    y = get_int(action, "y", -1)
    if x < 0:
        x = int(candidate.width * get_float(action, "x_ratio", 0.5))
    if y < 0:
        y = int(candidate.height * get_float(action, "y_ratio", 0.82))
    log(f"click_window {app}: {format_window_info(candidate)} at {x},{y}")
    run(["xdotool", "mousemove", "--window", candidate.window_id, str(x), str(y), "click", button], ctx)


def drag(action: dict[str, Any], ctx: Context) -> None:
    x1 = str(get_int(action, "x1"))
    y1 = str(get_int(action, "y1"))
    x2 = str(get_int(action, "x2"))
    y2 = str(get_int(action, "y2"))
    delay = str(get_int(action, "duration_ms", 500))
    if not ctx.dry_run:
        require_xdotool()
    run(["xdotool", "mousemove", x1, y1, "mousedown", "1", "mousemove", "--sync", x2, y2, "sleep", str(int(delay) / 1000), "mouseup", "1"], ctx)


def scroll(action: dict[str, Any], ctx: Context) -> None:
    direction = get_str(action, "direction", "down").lower()
    if direction not in {"up", "down"}:
        raise AutomationError("scroll direction 必须是 up 或 down")
    amount = max(1, get_int(action, "amount", 3))
    if ctx.dry_run:
        log(f"dry-run: scroll {direction} x{amount}")
        return
    require_xdotool()
    if any(action.get(key_name) for key_name in ("class", "title", "name")):
        candidate = best_window_candidate(action, infer_app(action))
        if candidate is None:
            raise AutomationError(f"scroll 找不到窗口：{action_window_match(action)}")
        x = get_int(action, "x", int(candidate.width * get_float(action, "x_ratio", 0.5)))
        y = get_int(action, "y", int(candidate.height * get_float(action, "y_ratio", 0.5)))
        run(["xdotool", "mousemove", "--window", candidate.window_id, str(x), str(y)], ctx)
    button = "4" if direction == "up" else "5"
    for _ in range(amount):
        run(["xdotool", "click", button], ctx)


def drag_window(action: dict[str, Any], ctx: Context) -> None:
    if ctx.dry_run:
        log(f"dry-run: drag_window {infer_app(action)}")
        return
    require_xdotool()
    candidate = best_window_candidate(action, infer_app(action))
    if candidate is None:
        raise AutomationError(f"drag_window 找不到窗口：{action_window_match(action)}")
    x1 = get_int(action, "x1", int(candidate.width * get_float(action, "x1_ratio", 0.45)))
    y1 = get_int(action, "y1", int(candidate.height * get_float(action, "y1_ratio", 0.45)))
    x2 = get_int(action, "x2", int(candidate.width * get_float(action, "x2_ratio", 0.70)))
    y2 = get_int(action, "y2", int(candidate.height * get_float(action, "y2_ratio", 0.70)))
    duration_s = max(0.0, get_float(action, "duration_ms", 500.0) / 1000.0)
    button = str(get_int(action, "button", 1))
    run([
        "xdotool", "mousemove", "--window", candidate.window_id, str(x1), str(y1),
        "mousedown", button, "mousemove", "--sync", "--window", candidate.window_id,
        str(x2), str(y2), "sleep", str(duration_s), "mouseup", button,
    ], ctx)


def find_window(action: dict[str, Any], ctx: Context) -> str:
    if not ctx.dry_run:
        require_xdotool()
    title = get_str(action, "title")
    window_class = get_str(action, "class")
    name = get_str(action, "name")
    search_args = ["xdotool", "search"]
    if not bool(action.get("include_hidden")):
        search_args.append("--onlyvisible")
    if window_class:
        search_args.extend(["--class", window_class])
    elif title:
        search_args.extend(["--name", title])
    elif name:
        search_args.extend(["--name", name])
    else:
        raise AutomationError("窗口操作需要 title、class 或 name")

    result = run(search_args, ctx, check=False)
    if ctx.dry_run:
        return "DRY_RUN_WINDOW"
    if (result is None or result.returncode != 0 or not result.stdout.strip()) and window_class and title:
        fallback = ["xdotool", "search"]
        if not bool(action.get("include_hidden")):
            fallback.append("--onlyvisible")
        fallback.extend(["--name", title])
        result = run(fallback, ctx, check=False)
    if result is None or result.returncode != 0 or not result.stdout.strip():
        raise AutomationError(f"找不到窗口：{title or window_class or name}")
    return result.stdout.strip().splitlines()[-1]


def focus(action: dict[str, Any], ctx: Context) -> None:
    candidate = best_window_candidate(action, infer_app(action)) if not ctx.dry_run else None
    window_id = candidate.window_id if candidate is not None else find_window(action, ctx)
    if window_id == "DRY_RUN_WINDOW":
        log("dry-run: focus window")
        return
    if bool(action.get("include_hidden")):
        run(["xdotool", "windowmap", window_id], ctx, check=False)
    retag_class = get_str(action, "retag_class")
    if retag_class:
        run(["xdotool", "set_window", "--class", retag_class, "--classname", retag_class, window_id], ctx)
    if bool(action.get("ensure_visible_geometry")):
        width = str(get_int(action, "window_width", 900))
        height = str(get_int(action, "window_height", 700))
        x = str(get_int(action, "window_x", 120))
        y = str(get_int(action, "window_y", 80))
        run(["xdotool", "windowsize", window_id, width, height], ctx, check=False)
        run(["xdotool", "windowmove", window_id, x, y], ctx, check=False)
    run(["xdotool", "windowactivate", "--sync", window_id], ctx)
    run(["xdotool", "windowraise", window_id], ctx, check=False)


def window_state(action: dict[str, Any], ctx: Context) -> None:
    state = get_str(action, "state").lower()
    if state not in {"maximize", "minimize", "restore"}:
        raise AutomationError("window_state state 必须是 maximize、minimize 或 restore")
    if ctx.dry_run:
        log(f"dry-run: window_state {state}")
        return
    search_action = dict(action)
    # QQ and some tray-aware desktop applications may hide their window while
    # an Observe dwell is in progress.  Every state transition must retain the
    # ability to address that same uniquely matched hidden window.
    search_action["include_hidden"] = True
    candidate = best_window_candidate(search_action, infer_app(search_action))
    window_id = candidate.window_id if candidate is not None else find_window(search_action, ctx)
    if state == "minimize":
        run(["xdotool", "windowminimize", window_id], ctx, check=False)
        return
    require_tool("wmctrl")
    run(["xdotool", "windowmap", window_id], ctx, check=False)
    run([
        "wmctrl", "-i", "-r", window_id, "-b",
        f"{'add' if state == 'maximize' else 'remove'},maximized_vert,maximized_horz",
    ], ctx)
    if state == "restore":
        run(["xdotool", "windowactivate", "--sync", window_id], ctx)


def dialog_path(action: dict[str, Any], ctx: Context) -> None:
    path = Path(get_str(action, "path"))
    if bool(action.get("must_exist", True)) and not ctx.dry_run and not path.is_file():
        raise AutomationError(f"对话框所需文件不存在：{path}")
    wait_before = max(0.0, get_float(action, "wait_before", 1.0))
    if wait_before:
        wait({"seconds": wait_before}, ctx)
    location_shortcut = get_str(action, "location_shortcut", "ctrl+l")
    if location_shortcut:
        key({"key": location_shortcut}, ctx)
    key({"key": "ctrl+a"}, ctx)
    _set_clipboard(str(path).encode("utf-8"), "UTF8_STRING", ctx)
    key({"key": "ctrl+v"}, ctx)
    key({"key": "Return"}, ctx)
    for _ in range(max(1, get_int(action, "confirm_count", 1)) - 1):
        wait({"seconds": get_float(action, "confirm_interval", 0.8)}, ctx)
        key({"key": "Return"}, ctx)
    wait_after = max(0.0, get_float(action, "wait_after", 3.0))
    if wait_after:
        wait({"seconds": wait_after}, ctx)


def open_file(action: dict[str, Any], ctx: Context) -> None:
    path = Path(get_str(action, "path"))
    if not ctx.dry_run and not path.is_file():
        raise AutomationError(f"要打开的文件不存在：{path}")
    if any(action.get(key_name) for key_name in ("class", "title", "name")):
        focus(action, ctx)
    key({"key": get_str(action, "shortcut", "ctrl+o")}, ctx)
    dialog_path({**action, "path": str(path), "must_exist": True}, ctx)


def save_as(action: dict[str, Any], ctx: Context) -> None:
    path = Path(get_str(action, "path"))
    if not ctx.dry_run:
        path.parent.mkdir(parents=True, exist_ok=True)
    if any(action.get(key_name) for key_name in ("class", "title", "name")):
        focus(action, ctx)
    key({"key": get_str(action, "shortcut", "ctrl+shift+s")}, ctx)
    dialog_path({**action, "path": str(path), "must_exist": False}, ctx)
    if bool(action.get("confirm_overwrite")):
        key({"key": get_str(action, "overwrite_key", "Left")}, ctx)
        key({"key": "Return"}, ctx)


def goto_cell(action: dict[str, Any], ctx: Context) -> None:
    reference = get_str(action, "reference")
    if not reference:
        raise AutomationError("goto_cell 需要 reference，例如 A1 或 A1:C100")
    key({"key": get_str(action, "shortcut", "ctrl+g")}, ctx)
    _set_clipboard(reference.encode("utf-8"), "UTF8_STRING", ctx)
    key({"key": "ctrl+v"}, ctx)
    key({"key": "Return"}, ctx)


def robust_switch_to_app(action: dict[str, Any], ctx: Context, app: str) -> None:
    if ctx.dry_run:
        log(f"dry-run: robust switch {app}")
        return
    require_xdotool()
    errors: list[str] = []
    for attempt in range(1, 4):
        candidates = list_window_candidates(action, app)
        if not candidates:
            errors.append(f"attempt {attempt}: no candidates")
            time.sleep(0.5)
            continue
        candidate = best_window_candidate(action, app)
        if candidate is None:
            errors.append(f"attempt {attempt}: no best candidate")
            time.sleep(0.5)
            continue
        log(f"switch {app} candidate: {format_window_info(candidate)}")
        subprocess.run(["xdotool", "windowactivate", "--sync", candidate.window_id], check=False)
        subprocess.run(["xdotool", "windowraise", candidate.window_id], check=False)
        time.sleep(0.2)
        active = get_foreground_window_info()
        if active.mapped_app == app:
            log(f"switch {app} verified: {format_window_info(active)}")
            return
        errors.append(
            f"attempt {attempt}: active={format_window_info(active)} "
            f"candidates={[format_window_info(item) for item in candidates]}"
        )
        time.sleep(0.5)
    raise AutomationError("; ".join(errors))


def switch(action: dict[str, Any], ctx: Context) -> None:
    app = infer_app(action)
    if app != "UNKNOWN":
        try:
            robust_switch_to_app(action, ctx, app)
            return
        except AutomationError as first_error:
            command = get_str(action, "command")
            if command:
                run_shell(command, ctx, check=False)
                time.sleep(1)
                robust_switch_to_app(action, ctx, app)
                return
            raise first_error
    try:
        focus(action, ctx)
        return
    except AutomationError:
        shell_command = get_str(action, "shell_command")
        if shell_command:
            run_shell(shell_command, ctx, check=False)
            return
        command = get_str(action, "command")
        if not command:
            raise
        launch(action, ctx)


def wait_window(action: dict[str, Any], ctx: Context) -> None:
    app = infer_app(action)
    if ctx.dry_run:
        log(f"dry-run: wait_window {app}")
        return
    timeout = get_float(action, "timeout", 15.0)
    deadline = time.monotonic() + timeout
    last_error = ""
    while time.monotonic() <= deadline:
        candidates = list_window_candidates(action, app)
        matched = [item for item in candidates if item.mapped_app == app]
        if matched:
            best = best_window_candidate(action, app) or matched[-1]
            log(f"wait_window {app} found: {format_window_info(best)}")
            return
        last_error = f"candidates={[format_window_info(item) for item in candidates]}"
        time.sleep(0.5)
    raise AutomationError(f"wait_window {app} timeout after {timeout:g}s; {last_error}")


def verify_foreground(action: dict[str, Any], ctx: Context) -> None:
    app = infer_app(action)
    if ctx.dry_run:
        log(f"dry-run: verify_foreground {app}")
        return
    active = get_foreground_window_info()
    if active.mapped_app != app:
        raise AutomationError(f"foreground verify failed: expected={app} active={format_window_info(active)}")
    log(f"verify_foreground {app}: {format_window_info(active)}")


def verify_window_profile(action: dict[str, Any], ctx: Context) -> None:
    """Validate class/title/geometry before a profile-dependent UI operation."""
    profile_path = Path(get_str(action, "profile"))
    if not profile_path.exists():
        raise AutomationError(f"窗口 profile 不存在: {profile_path}")
    try:
        profile = json.loads(profile_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AutomationError(f"无法读取窗口 profile: {exc}") from exc
    app = infer_app(action)
    candidate = best_window_candidate(action, app)
    if candidate is None:
        raise AutomationError(f"profile verify failed: 未找到 {app} 窗口")
    expected = profile.get("window", {}) if isinstance(profile, dict) else {}
    expected_width = int(expected.get("width", 0) or 0)
    expected_height = int(expected.get("height", 0) or 0)
    tolerance = get_float(action, "geometry_tolerance_ratio", float(profile.get("geometry_tolerance_ratio", 0.25) if isinstance(profile, dict) else 0.25))
    if expected_width and abs(candidate.width - expected_width) / expected_width > tolerance:
        raise AutomationError(f"profile verify failed: width={candidate.width}, expected={expected_width}, tolerance={tolerance}")
    if expected_height and abs(candidate.height - expected_height) / expected_height > tolerance:
        raise AutomationError(f"profile verify failed: height={candidate.height}, expected={expected_height}, tolerance={tolerance}")
    action["metadata"] = {
        **(action.get("metadata", {}) if isinstance(action.get("metadata"), dict) else {}),
        "profile": str(profile_path), "window_id": candidate.window_id,
        "window_title": candidate.title, "window_class": candidate.wm_class,
        "window_geometry": {"width": candidate.width, "height": candidate.height},
    }
    log(f"verify_window_profile {app}: {format_window_info(candidate)}")


def read_proc_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


def read_proc_link(path: Path) -> str:
    try:
        return os.readlink(path)
    except OSError:
        return ""


def find_matching_pids(process_names: list[str], path_contains: list[str], cmdline_contains: list[str] | None = None) -> list[int]:
    names = set(process_names)
    cmdline_tokens = cmdline_contains or []
    current_pid = os.getpid()
    matches: list[int] = []
    for item in Path("/proc").iterdir():
        if not item.name.isdigit():
            continue
        pid = int(item.name)
        if pid == current_pid:
            continue
        comm = read_proc_text(item / "comm")
        exe = read_proc_link(item / "exe")
        cmdline = read_proc_text(item / "cmdline").replace("\x00", " ")
        if (
            comm in names
            or any(token and token in exe for token in path_contains)
            or any(token and token in cmdline for token in cmdline_tokens)
        ):
            matches.append(pid)
    return sorted(matches)


def signal_from_name(name: str) -> signal.Signals:
    normalized = name.upper()
    if normalized.startswith("SIG"):
        normalized = normalized[3:]
    try:
        return signal.Signals[f"SIG{normalized}"]
    except KeyError as exc:
        raise AutomationError(f"不支持的 signal: {name}") from exc


def kill_pids(pids: list[int], sig: signal.Signals, ctx: Context) -> None:
    for pid in pids:
        if ctx.dry_run:
            log(f"dry-run: kill -{sig.name} {pid}")
            continue
        try:
            log(f"kill -{sig.name} {pid}")
            os.kill(pid, sig)
        except ProcessLookupError:
            continue
        except PermissionError as exc:
            log(f"skip pid {pid}: {exc}")


def close(action: dict[str, Any], ctx: Context) -> None:
    command = get_str(action, "command")
    if command:
        run_shell(command, ctx, check=False)
        return

    title = get_str(action, "title")
    window_class = get_str(action, "class")
    name = get_str(action, "name")
    process_names = get_str_list(action, "process_names")
    path_contains = get_str_list(action, "path_contains")
    cmdline_contains = get_str_list(action, "cmdline_contains")
    if title or window_class:
        if not ctx.dry_run:
            require_xdotool()
        try:
            window_id = find_window(action, ctx)
            if window_id == "DRY_RUN_WINDOW":
                log("dry-run: close window")
            else:
                run(["xdotool", "windowclose", window_id], ctx, check=False)
        except AutomationError as exc:
            log(f"window close skipped: {exc}")
        wait_seconds = get_float(action, "wait_after_window_close", 2.0)
        if wait_seconds > 0:
            wait({"seconds": wait_seconds}, ctx)
        if not process_names and not path_contains and not cmdline_contains and not name:
            return

    # --- cgroup / tracked-process cleanup ---
    cgroup_cleaned_up = False
    if name and name in ctx.processes:
        tracked = ctx.processes[name]
        if isinstance(tracked, str) and tracked.startswith("automation-"):
            # systemd unit — kill entire cgroup in one shot
            unit_name = tracked
            log(f"stop cgroup unit: {unit_name}")
            if not ctx.dry_run:
                _cgroup_stop(unit_name)
                if _cgroup_unit_active(unit_name):
                    time.sleep(1.0)
                    _cgroup_kill(unit_name, "SIGKILL")
                    _cgroup_stop(unit_name)
            del ctx.processes[name]
            cgroup_cleaned_up = True
        else:
            # old-style Popen tracking
            proc = tracked
            if proc.poll() is None:
                if ctx.dry_run:
                    log(f"dry-run: terminate {name}")
                else:
                    log(f"terminate {name}")
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
            del ctx.processes[name]

    if cgroup_cleaned_up and not process_names and not path_contains and not cmdline_contains:
        return  # cgroup already killed everything, no further cleanup needed

    if process_names or path_contains or cmdline_contains:
        if ctx.dry_run:
            log(
                "dry-run: match processes "
                f"names={process_names} paths={path_contains} cmdline={cmdline_contains}"
            )
            return
        sig = signal_from_name(get_str(action, "signal", "TERM"))
        pids = find_matching_pids(process_names, path_contains, cmdline_contains)
        if pids:
            log(f"matched pids: {','.join(str(pid) for pid in pids)}")
            kill_pids(pids, sig, ctx)
        else:
            log("matched pids: none")
        force_after = get_float(action, "force_after_seconds", 0.0)
        if force_after > 0:
            wait({"seconds": force_after}, ctx)
            remaining = find_matching_pids(process_names, path_contains, cmdline_contains)
            if remaining:
                log(f"force kill pids: {','.join(str(pid) for pid in remaining)}")
                kill_pids(remaining, signal.SIGKILL, ctx)
        return

    if name:
        run(["pkill", "-x", name], ctx, check=False)
        return

    raise AutomationError("close 需要 name、title、class、process_names、path_contains、cmdline_contains 或 command")


def cleanup_tracked_processes(ctx: Context) -> None:
    """Stop applications launched by a scenario that aborted before close actions."""
    for name in reversed(list(ctx.processes)):
        try:
            close({"name": name}, ctx)
        except (AutomationError, subprocess.CalledProcessError) as exc:
            log(f"cleanup skipped for {name}: {exc}")


ACTION_HANDLERS = {
    "trace_marker": trace_marker_action,
    "launch": launch,
    "wait": wait,
    "key": key,
    "hotkey": key,
    "type": type_text,
    "text": type_text,
    "paste_text": paste_text,
    "clipboard_file": clipboard_file,
    "assert_file": assert_file,
    "click": click,
    "tap": click,
    "click_window": click_window,
    "scroll": scroll,
    "drag": drag,
    "swipe": drag,
    "drag_window": drag_window,
    "focus": focus,
    "gnome_search_focus": gnome_search_focus,
    "window_state": window_state,
    "switch": switch,
    "wait_window": wait_window,
    "verify_foreground": verify_foreground,
    "verify_window_profile": verify_window_profile,
    "dialog_path": dialog_path,
    "open_file": open_file,
    "save_as": save_as,
    "goto_cell": goto_cell,
    "close": close,
    "shell": lambda action, ctx: _handle_shell(action, ctx),
}


def _handle_shell(action: dict[str, Any], ctx: Context) -> None:
    """Execute a shell command, optionally tracking it in a cgroup."""
    command = get_str(action, "command")
    name = get_str(action, "name")
    if not command:
        raise AutomationError("shell 需要 command")
    if ctx.dry_run:
        log(f"dry-run: {command}")
        return
    if name and _cgroup_available():
        unit_name = _cgroup_launch_shell(command, name, os.environ.copy(), test_slice=ctx.test_slice)
        log(f"shell (cgroup: {unit_name}): {command}")
        ctx.processes[name] = unit_name
    else:
        run_shell(command, ctx)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Linux 应用自动化：打开、操作、关闭、切换应用。")
    parser.add_argument("scenario", type=Path, help="JSON 场景文件")
    parser.add_argument("--dry-run", action="store_true", help="只打印动作，不实际执行")
    parser.add_argument("--display", default="", help="指定 X11 DISPLAY，例如 :0。默认会从环境或 /tmp/.X11-unix 自动探测")
    parser.add_argument("--xauthority", default="", help="指定 XAUTHORITY，例如 /home/lzx/.Xauthority")
    parser.add_argument("--trace-output", default="", help="写出 automation_trace.csv")
    parser.add_argument("--session-id", default="", help="实验 session id")
    parser.add_argument("--scenario-id", default="", help="场景 id，默认使用 scenario 文件名")
    parser.add_argument("--test-slice", default="huawei-test.slice", help="systemd --slice 参数，所有自动化 scope 挂到此 slice 下")
    parser.add_argument("--var", action="append", default=[], metavar="NAME=VALUE", help="覆盖场景变量，可重复传入")
    parser.add_argument("--calibration-only", action="store_true", help="只采集窗口信息，不执行 UI 操作")
    parser.add_argument("--calibration-output-dir", default="", help="calibration 截图目录（可选）")
    parser.add_argument("--screenshot-output-dir", default="", help="失败时截图目录（可选）")
    return parser.parse_args(argv)


def calibration_trace(action: dict[str, Any], ctx: Context) -> None:
    """Collect a best-effort window snapshot while keeping the desktop untouched."""
    details = quiet_window_trace(action)
    screenshot_path = ""
    if ctx.calibration_output_dir is not None and not ctx.dry_run and shutil.which("gnome-screenshot"):
        ctx.calibration_output_dir.mkdir(parents=True, exist_ok=True)
        stem = str(action.get("action_id") or action.get("operation_id") or "window")
        destination = ctx.calibration_output_dir / f"{stem}.png"
        result = subprocess.run(
            ["gnome-screenshot", "-f", str(destination)],
            check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        if result.returncode == 0 and destination.exists():
            screenshot_path = str(destination)
    calibration_action = dict(action)
    calibration_action.update(details)
    calibration_action["screenshot_path"] = screenshot_path
    trace_marker(
        ctx,
        event_type="OP_DONE",
        status="calibration_only",
        action=calibration_action,
        op_type="calibration",
    )


def capture_failure_screenshot(action: dict[str, Any], ctx: Context) -> str:
    if ctx.screenshot_output_dir is None or ctx.dry_run or not shutil.which("gnome-screenshot"):
        return ""
    ctx.screenshot_output_dir.mkdir(parents=True, exist_ok=True)
    stem = str(action.get("action_id") or action.get("operation_id") or "failed")
    destination = ctx.screenshot_output_dir / f"{stem}_failed.png"
    result = subprocess.run(["gnome-screenshot", "-f", str(destination)], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return str(destination) if result.returncode == 0 and destination.exists() else ""


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        configure_display(args.display, args.xauthority)
        scenario = load_scenario(args.scenario, parse_var_overrides(args.var))
        actions = scenario.actions
        scenario_id = args.scenario_id or args.scenario.stem
        session_id = args.session_id or dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        trace = TraceWriter(args.trace_output, session_id, scenario_id) if args.trace_output else None
        ctx = Context(
            dry_run=args.dry_run,
            processes={},
            trace=trace,
            session_id=session_id,
            scenario_id=scenario_id,
            test_slice=args.test_slice,
            validation_mode=scenario.validation_mode,
            calibration_only=args.calibration_only,
            calibration_output_dir=Path(args.calibration_output_dir) if args.calibration_output_dir else None,
            screenshot_output_dir=Path(args.screenshot_output_dir) if args.screenshot_output_dir else None,
        )
        log(f"scenario={args.scenario}")
        completed = False
        try:
            trace_marker(ctx, event_type="SCENARIO_START", status="running", op_type="scenario")
            for index, action in enumerate(actions, start=1):
                action_type = get_str(action, "type")
                handler = ACTION_HANDLERS.get(action_type)
                if handler is None:
                    raise AutomationError(f"不支持的 action type: {action_type}")
                log(f"action #{index}: {action_type}")
                if action_type == "trace_marker":
                    handler(action, ctx)
                    continue
                trace_action(ctx, index, "start", "running", action)
                try:
                    calibration_safe_actions = {"launch", "shell", "wait", "wait_window", "focus", "gnome_search_focus", "switch", "verify_foreground", "verify_window_profile", "close"}
                    if ctx.calibration_only and action_type not in calibration_safe_actions:
                        calibration_trace(action, ctx)
                        continue
                    handler(action, ctx)
                    trace_action(ctx, index, "end", "success", action)
                    is_named_app_action = bool(get_str(action, "name")) or action_type == "launch"
                    if is_named_app_action and action_type in {"launch", "shell"} and infer_app(action) != "UNKNOWN":
                        trace_marker(
                            ctx,
                            event_type="APP_LAUNCH",
                            status="success",
                            step_id=index,
                            action=action,
                            op_type=action_type,
                        )
                    elif is_named_app_action and action_type in {"focus", "gnome_search_focus", "switch", "verify_foreground"} and infer_app(action) != "UNKNOWN":
                        trace_marker(
                            ctx,
                            event_type="APP_FOCUS",
                            status="success",
                            step_id=index,
                            action=action,
                            op_type=action_type,
                        )
                except (AutomationError, subprocess.CalledProcessError) as exc:
                    if isinstance(exc, subprocess.CalledProcessError):
                        message = exc.stderr.strip() or exc.stdout.strip() or str(exc)
                    else:
                        message = str(exc)
                    failed_action = dict(action)
                    failed_action["screenshot_path"] = capture_failure_screenshot(action, ctx)
                    trace_action(ctx, index, "end", "failed", failed_action, message)
                    if get_str(action, "operation_id"):
                        trace_marker(
                            ctx,
                            event_type="OP_FAILED",
                            status="failed",
                            step_id=index,
                            action=failed_action,
                            op_type="semantic_operation",
                            error=message,
                        )
                    if action.get("optional"):
                        log(f"optional action skipped: {message}")
                        continue
                    raise
            keep_alive(scenario.keep_alive_after_s, ctx)
            trace_marker(ctx, event_type="SCENARIO_DONE", status="success", op_type="scenario")
            log("done")
            completed = True
            return 0
        except (AutomationError, subprocess.CalledProcessError) as exc:
            trace_marker(
                ctx,
                event_type="SCENARIO_FAILED",
                status="failed",
                op_type="scenario",
                error=str(exc),
            )
            raise
        finally:
            _stop_clipboard_process(ctx)
            if not completed:
                cleanup_tracked_processes(ctx)
            if ctx.trace is not None:
                ctx.trace.close()
    except (AutomationError, subprocess.CalledProcessError) as exc:
        if isinstance(exc, subprocess.CalledProcessError):
            message = exc.stderr.strip() or exc.stdout.strip() or str(exc)
            if "Can't open display" in message:
                message += "\n提示：尝试在桌面终端运行，或执行 python3 app_automation.py <scenario> --display :0"
        else:
            message = str(exc)
        print(f"error: {message}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
