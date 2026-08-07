"""Application lifecycle and window state event builder.

Emits three separate event streams:
- process_events:    PROCESS_START, PROCESS_EXIT, PROCESS_BASELINE
- foreground_events: APP_SWITCH, APP_FOCUS_IN, APP_FOCUS_OUT,
                     APP_MINIMIZE, APP_RESTORE
- app_lifecycle:     APP_OPEN, APP_CLOSE
"""

from __future__ import annotations

import datetime as dt
import time
from dataclasses import dataclass, field
from typing import Any

from collectors.foreground import ForegroundState, WindowState
from collectors.process import ProcessSample


def _timestamp_from_ns(ts_ns: int) -> str:
    return dt.datetime.fromtimestamp(ts_ns / 1_000_000_000).strftime("%Y-%m-%d %H:%M:%S")


@dataclass
class LifecycleEvents:
    """Three independent event streams produced by one sample."""
    process_events: list[dict[str, Any]] = field(default_factory=list)
    foreground_events: list[dict[str, Any]] = field(default_factory=list)
    app_lifecycle: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class LifecycleEventBuilder:
    target_app: str
    session_id: str = ""
    close_grace_windows: int = 2
    test_slice: str = ""
    app_pid_sets: dict[str, set[int]] = field(default_factory=dict)
    pid_meta: dict[int, dict[str, Any]] = field(default_factory=dict)
    foreground_app: str = ""
    foreground_since_ns: int = 0
    window_hidden: dict[str, bool] = field(default_factory=dict)
    _baseline_emitted: bool = False
    _app_lifecycle_active: bool = False  # starts after first sample baseline
    _missing_counts: dict[str, int] = field(default_factory=dict)

    def build_all(
        self,
        samples: list[ProcessSample],
        foreground: ForegroundState,
        windows: list[WindowState] | None = None,
    ) -> LifecycleEvents:
        ts_ns = time.time_ns()
        events = LifecycleEvents()
        windows = windows or []

        current_by_app: dict[str, set[int]] = {}
        current_meta: dict[int, dict[str, Any]] = {}
        for sample in samples:
            app = sample.app_id
            pid = sample.identity.pid
            current_by_app.setdefault(app, set()).add(pid)
            current_meta[pid] = {
                "app": app,
                "pid": pid,
                "tgid": sample.identity.tgid,
                "comm": sample.identity.comm,
                "cmdline_hash": sample.identity.cmdline_hash,
                "exe_path": sample.identity.exe_path,
                "cgroup_path": sample.identity.cgroup_path,
                "cgroup_unit": _cgroup_unit(sample.identity.cgroup_path),
                "test_slice": self.test_slice,
                "in_test_slice": getattr(sample, "in_test_slice", False),
                "source": "procfs",
            }

        # --- process_events ---
        old_pids = set(self.pid_meta)
        current_pids = set(current_meta)

        if not self._baseline_emitted and self.pid_meta:
            # First sample where we already have pids: emit PROCESS_BASELINE
            for pid in sorted(self.pid_meta):
                meta = self.pid_meta[pid]
                events.process_events.append(self._process_event(
                    ts_ns, "PROCESS_BASELINE", meta, in_test_slice=self._in_test_slice(meta),
                ))
            self._baseline_emitted = True

        for pid in sorted(current_pids - old_pids):
            meta = current_meta[pid]
            in_slice = self._in_test_slice(meta)
            events.process_events.append(
                self._process_event(ts_ns, "PROCESS_START", meta, in_test_slice=in_slice)
            )

        for pid in sorted(old_pids - current_pids):
            meta = self.pid_meta[pid]
            in_slice = self._in_test_slice(meta)
            events.process_events.append(
                self._process_event(ts_ns, "PROCESS_EXIT", meta, in_test_slice=in_slice)
            )

        # --- app_lifecycle ---
        # Skip APP_OPEN/APP_CLOSE on the first sample (baseline).
        # Only start tracking after the baseline app_pid_sets are established.
        if self._app_lifecycle_active:
            old_open = set(self.app_pid_sets)
            current_apps = set(current_by_app)
            next_open_sets = {app: set(pids) for app, pids in self.app_pid_sets.items()}
            rolling_open = set(old_open)

            for app in sorted(current_apps):
                pids = set(current_by_app.get(app, set()))
                self._missing_counts[app] = 0
                if app not in old_open and app:
                    before = set(rolling_open)
                    rolling_open.add(app)
                    events.app_lifecycle.append(self._lifecycle_event(
                        ts_ns,
                        "APP_OPEN",
                        app,
                        0,
                        len(pids),
                        before,
                        set(rolling_open),
                    ))
                next_open_sets[app] = pids

            for app in sorted(old_open - current_apps):
                if app:
                    self._missing_counts[app] = self._missing_counts.get(app, 0) + 1
                    if self._missing_counts[app] >= max(1, self.close_grace_windows):
                        before = set(rolling_open)
                        rolling_open.discard(app)
                        events.app_lifecycle.append(self._lifecycle_event(
                            ts_ns,
                            "APP_CLOSE",
                            app,
                            len(self.app_pid_sets.get(app, set())),
                            0,
                            before,
                            set(rolling_open),
                        ))
                        next_open_sets.pop(app, None)
                    else:
                        next_open_sets[app] = set()
            self.app_pid_sets = next_open_sets
        else:
            self._app_lifecycle_active = True
            self.app_pid_sets = {app: set(pids) for app, pids in current_by_app.items()}

        # --- foreground_events ---
        resolved_foreground = self._resolve_foreground_app(foreground, current_meta)
        if resolved_foreground != self.foreground_app:
            old_app = self.foreground_app
            if old_app:
                events.foreground_events.append(
                    self._foreground_event(
                        ts_ns, "APP_FOCUS_OUT",
                        app=old_app, old_app=old_app, new_app=resolved_foreground,
                        foreground_app=resolved_foreground,
                        duration_ms=self._duration_ms(ts_ns),
                        source=foreground.source,
                    )
                )
            if resolved_foreground:
                events.foreground_events.append(
                    self._foreground_event(
                        ts_ns, "APP_SWITCH",
                        app=resolved_foreground,
                        pid=foreground.foreground_pid,
                        window_id=foreground.window_id,
                        window_title=foreground.window_title,
                        old_app=old_app,
                        new_app=resolved_foreground,
                        foreground_app=resolved_foreground,
                        duration_ms=self._duration_ms(ts_ns) if old_app else 0,
                        source=foreground.source,
                    )
                )
                events.foreground_events.append(
                    self._foreground_event(
                        ts_ns, "APP_FOCUS_IN",
                        app=resolved_foreground,
                        pid=foreground.foreground_pid,
                        window_id=foreground.window_id,
                        window_title=foreground.window_title,
                        old_app=old_app,
                        new_app=resolved_foreground,
                        foreground_app=resolved_foreground,
                        source=foreground.source,
                    )
                )
            self.foreground_app = resolved_foreground
            self.foreground_since_ns = ts_ns

        # Window minimize/restore events
        events.foreground_events.extend(
            self._window_events(ts_ns, windows, current_meta, resolved_foreground)
        )

        # --- update state ---
        self.pid_meta = current_meta
        return events

    # ------------------------------------------------------------------
    # private helpers
    # ------------------------------------------------------------------

    def _window_events(
        self,
        ts_ns: int,
        windows: list[WindowState],
        current_meta: dict[int, dict[str, Any]],
        foreground_app: str,
    ) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        seen: set[str] = set()
        for window in windows:
            if not window.window_id:
                continue
            app = current_meta.get(window.pid, {}).get("app", "")
            if not app:
                continue
            seen.add(window.window_id)
            previous = self.window_hidden.get(window.window_id)
            if previous is None:
                self.window_hidden[window.window_id] = window.is_hidden
                continue
            if previous == window.is_hidden:
                continue
            self.window_hidden[window.window_id] = window.is_hidden
            events.append(
                self._foreground_event(
                    ts_ns,
                    "APP_MINIMIZE" if window.is_hidden else "APP_RESTORE",
                    app=app,
                    pid=window.pid,
                    window_id=window.window_id,
                    window_title=window.window_title,
                    foreground_app=foreground_app,
                    source=window.source,
                )
            )
        for stale in set(self.window_hidden) - seen:
            self.window_hidden.pop(stale, None)
        return events

    def _resolve_foreground_app(
        self, foreground: ForegroundState, current_meta: dict[int, dict[str, Any]]
    ) -> str:
        if foreground.foreground_pid in current_meta:
            return str(current_meta[foreground.foreground_pid].get("app", ""))
        if foreground.foreground_app == self.target_app:
            return self.target_app
        return foreground.foreground_app

    def _duration_ms(self, ts_ns: int) -> int:
        if not self.foreground_since_ns:
            return 0
        return max(0, int((ts_ns - self.foreground_since_ns) / 1_000_000))

    @staticmethod
    def _in_test_slice(meta: dict[str, Any]) -> bool:
        return bool(meta.get("in_test_slice", False))

    # ------------------------------------------------------------------
    # event factories
    # ------------------------------------------------------------------

    def _process_event(
        self, ts_ns: int, event_type: str, meta: dict[str, Any],
        *, in_test_slice: bool = False,
    ) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "ts_ns": ts_ns,
            "timestamp": _timestamp_from_ns(ts_ns),
            "event_type": event_type,
            "app": str(meta.get("app", "")),
            "pid": meta.get("pid", ""),
            "tgid": meta.get("tgid", ""),
            "comm": str(meta.get("comm", "")),
            "cmdline_hash": str(meta.get("cmdline_hash", "")),
            "exe_path": str(meta.get("exe_path", "")),
            "cgroup_unit": str(meta.get("cgroup_unit", "")),
            "cgroup_path": str(meta.get("cgroup_path", "")),
            "test_slice": str(meta.get("test_slice", "")),
            "in_test_slice": "1" if in_test_slice else "0",
            "source": str(meta.get("source", "")),
        }

    def _foreground_event(
        self,
        ts_ns: int,
        event_type: str,
        *,
        app: str = "",
        pid: int | str = "",
        tgid: int | str = "",
        window_id: str = "",
        window_title: str = "",
        old_app: str = "",
        new_app: str = "",
        foreground_app: str = "",
        duration_ms: int | str = "",
        source: str = "",
    ) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "ts_ns": ts_ns,
            "timestamp": _timestamp_from_ns(ts_ns),
            "event_type": event_type,
            "old_app": old_app,
            "new_app": new_app,
            "foreground_app": foreground_app or app,
            "duration_ms": duration_ms,
            "window_id": window_id,
            "window_title": window_title,
            "wm_class": "",
            "pid": pid,
            "tgid": tgid,
            "source": source,
        }

    def _lifecycle_event(
        self,
        ts_ns: int,
        event_type: str,
        app: str,
        pid_count_before: int,
        pid_count_after: int,
        open_apps_before: set[str],
        open_apps_after: set[str],
    ) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "ts_ns": ts_ns,
            "timestamp": _timestamp_from_ns(ts_ns),
            "event_type": event_type,
            "app": app,
            "open_apps_before": "|".join(sorted(open_apps_before)),
            "open_apps_after": "|".join(sorted(open_apps_after)),
            "pid_count_before": pid_count_before,
            "pid_count_after": pid_count_after,
            "source": "procfs",
        }


def _cgroup_unit(cgroup_path: str) -> str:
    if not cgroup_path:
        return ""
    parts = cgroup_path.strip("/").split("/")
    for part in reversed(parts):
        if "." in part:
            return part
    return ""
