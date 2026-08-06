"""Build one-second feature rows from raw samples — global_state_1s.csv."""

from __future__ import annotations

import datetime as dt
import subprocess
from pathlib import Path
from typing import Any

from collectors.foreground import ForegroundState


def _delta(now: dict[str, int], prev: dict[str, int] | None, key: str) -> int:
    value = int(now.get(key, 0))
    if not prev:
        return 0
    return max(0, value - int(prev.get(key, 0)))


def _resolve_test_slice_path(test_slice: str) -> str:
    """Return the real cgroup path for *test_slice* via systemctl, or fallback."""
    if not test_slice:
        return ""
    try:
        result = subprocess.run(
            ["systemctl", "--user", "show", test_slice, "-p", "ControlGroup"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                if line.startswith("ControlGroup="):
                    cg = line.split("=", 1)[1].strip()
                    if cg:
                        return str(Path("/sys/fs/cgroup") / cg.lstrip("/"))
    except (OSError, subprocess.TimeoutExpired):
        pass
    # Fallback
    print(f"warning: could not resolve ControlGroup for {test_slice}, using fallback")
    return str(Path("/sys/fs/cgroup") / test_slice.lstrip("/"))


class FeatureBuilder:
    def __init__(self, label: str = "", session_id: str = "", test_slice: str = "") -> None:
        self.label = label
        self.session_id = session_id
        self.test_slice = test_slice
        self.test_slice_path = _resolve_test_slice_path(test_slice)
        self.feature_window_id = 0
        self.prev_vmstat: dict[str, int] | None = None

    def build(
        self,
        foreground: ForegroundState,
        meminfo: dict[str, int],
        vmstat: dict[str, int],
        window_start_ns: int = 0,
        window_end_ns: int = 0,
        registry_summary: dict[str, str] | None = None,
        foreground_window_id: str = "",
        foreground_pid: int = 0,
        foreground_wm_class: str = "",
        operation_context: dict[str, str] | None = None,
        test_mem_current: int = 0,
        test_mem_high: int = 0,
        test_mem_max: int = 0,
    ) -> dict[str, Any]:
        registry_summary = registry_summary or {}
        op = operation_context or {}

        row = {
            "session_id": self.session_id,
            "feature_window_id": self.feature_window_id,
            "window_start_ns": window_start_ns,
            "window_end_ns": window_end_ns,
            "timestamp": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "foreground_app": foreground.foreground_app,
            "foreground_duration_ms": int(foreground.foreground_duration * 1000),
            "foreground_window_id": foreground_window_id,
            "foreground_pid": foreground_pid,
            "foreground_wm_class": foreground_wm_class,
            "foreground_window_title": foreground.window_title,
            "observed_apps": registry_summary.get("observed_apps", ""),
            "open_apps": registry_summary.get("open_apps", ""),
            "closed_apps": registry_summary.get("closed_apps", ""),
            "newly_opened_apps": registry_summary.get("newly_opened_apps", ""),
            "newly_closed_apps": registry_summary.get("newly_closed_apps", ""),
            "app_history": registry_summary.get("app_history", ""),
            "duration_history_ms": registry_summary.get("duration_history", ""),
            "current_operation_label": op.get("operation_label", ""),
            "current_operation_app": op.get("operation_app", ""),
            "current_action": op.get("action", ""),
            "state_label": op.get("state_label", ""),
            "manual_label": op.get("manual_label") or self.label,
            "scenario_id": op.get("scenario_id", ""),
            "step_id": op.get("step_id", ""),
            "global_mem_available_kb": meminfo.get("MemAvailable", 0),
            "global_pgmajfault_delta": _delta(vmstat, self.prev_vmstat, "pgmajfault"),
            "global_pswpin_delta": _delta(vmstat, self.prev_vmstat, "pswpin"),
            "global_pswpout_delta": _delta(vmstat, self.prev_vmstat, "pswpout"),
            "global_pgscan_delta": _delta(vmstat, self.prev_vmstat, "pgscan_kswapd")
            + _delta(vmstat, self.prev_vmstat, "pgscan_direct"),
            "global_pgsteal_delta": _delta(vmstat, self.prev_vmstat, "pgsteal_kswapd")
            + _delta(vmstat, self.prev_vmstat, "pgsteal_direct"),
            "test_slice": self.test_slice,
            "test_slice_path": self.test_slice_path,
            "test_mem_current": test_mem_current,
            "test_mem_high": test_mem_high,
            "test_mem_max": test_mem_max,
        }
        self.prev_vmstat = dict(vmstat)
        self.feature_window_id += 1
        return row
