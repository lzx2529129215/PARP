"""Observed application registry for per-app runtime features."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from collectors.foreground import ForegroundState
from collectors.process import ProcessSample


def _extract_cgroup_unit(cgroup_path: str) -> str:
    """Extract the innermost cgroup unit name, e.g. 'automation-wps.scope'."""
    if not cgroup_path:
        return ""
    parts = cgroup_path.strip("/").split("/")
    for part in reversed(parts):
        if "." in part:
            return part
    return ""


@dataclass
class AppRecord:
    app_id: str
    display_name: str = ""
    wm_class: str = ""
    last_window_title: str = ""
    pid_set: set[int] = field(default_factory=set)
    tgid_set: set[int] = field(default_factory=set)
    cgroup_path: str = ""
    app_cgroup_unit: str = ""
    in_test_slice: bool = False
    comm: str = ""
    exe_path: str = ""
    cmdline_hash: str = ""
    first_seen_ns: int = 0
    last_seen_ns: int = 0
    is_foreground: bool = False
    foreground_count: int = 0
    closed: bool = False

    @property
    def is_open(self) -> bool:
        return bool(self.first_seen_ns) and not self.closed


class AppRegistry:
    def __init__(self, target_apps: list[str] | None = None, test_slice: str = "", close_grace_windows: int = 2) -> None:
        self.records: dict[str, AppRecord] = {}
        self.app_history: list[str] = []
        self.duration_history: list[str] = []
        self._last_foreground_app = ""
        self.test_slice = test_slice
        self.close_grace_windows = max(1, close_grace_windows)
        self._missing_counts: dict[str, int] = {}
        self._newly_opened: set[str] = set()
        self._newly_closed: set[str] = set()
        for app_id in target_apps or []:
            if app_id:
                self.ensure(app_id)

    def ensure(self, app_id: str) -> AppRecord:
        if app_id not in self.records:
            self.records[app_id] = AppRecord(app_id=app_id, display_name=app_id)
        return self.records[app_id]

    def update(self, samples: list[ProcessSample], foreground: ForegroundState) -> None:
        ts_ns = time.time_ns()
        by_app: dict[str, list[ProcessSample]] = {}
        for sample in samples:
            if not sample.app_id:
                continue
            by_app.setdefault(sample.app_id, []).append(sample)

        foreground_app = foreground.foreground_app if foreground.foreground_app != "UNKNOWN" else ""
        if foreground_app:
            record = self.ensure(foreground_app)
            record.last_window_title = foreground.window_title
            record.wm_class = foreground_app
            if foreground.foreground_pid:
                by_app.setdefault(foreground_app, [])

        if foreground_app and foreground_app != self._last_foreground_app:
            self.app_history.append(foreground_app)
            self._last_foreground_app = foreground_app

        # Snapshot before-update state for delta computation
        previously_open = set(self.open_apps)

        self._newly_opened.clear()
        self._newly_closed.clear()

        for app_id, record in self.records.items():
            app_samples = by_app.get(app_id, [])
            record.pid_set = {sample.identity.pid for sample in app_samples}
            record.tgid_set = {sample.identity.tgid for sample in app_samples}
            record.is_foreground = app_id == foreground_app
            if record.is_foreground:
                record.foreground_count += 1
                record.last_window_title = foreground.window_title
                self.duration_history.append(str(int(foreground.foreground_duration * 1000)))
            if app_samples:
                self._missing_counts[app_id] = 0
                previously_was_closed = record.closed
                record.closed = False
                if not record.first_seen_ns:
                    record.first_seen_ns = ts_ns
                record.last_seen_ns = ts_ns
                first = app_samples[0].identity
                record.cgroup_path = first.cgroup_path
                record.cmdline_hash = "|".join(sorted({
                    sample.identity.cmdline_hash for sample in app_samples
                    if sample.identity.cmdline_hash
                }))
                # Check test slice membership
                if self.test_slice:
                    record.in_test_slice = any(
                        f"/{self.test_slice}/" in (sample.identity.cgroup_path or "")
                        for sample in app_samples
                    )
                # Extract the innermost cgroup unit name
                record.app_cgroup_unit = _extract_cgroup_unit(record.cgroup_path)
                record.comm = "|".join(sorted({sample.identity.comm for sample in app_samples if sample.identity.comm}))
                record.exe_path = "|".join(sorted({sample.identity.exe_path for sample in app_samples if sample.identity.exe_path}))
                # Detect newly opened
                if previously_was_closed or not record.first_seen_ns:
                    self._newly_opened.add(app_id)
            elif record.first_seen_ns and not record.closed:
                record.last_seen_ns = ts_ns
                self._missing_counts[app_id] = self._missing_counts.get(app_id, 0) + 1
                if self._missing_counts[app_id] >= self.close_grace_windows:
                    record.closed = True
                    self._newly_closed.add(app_id)
            elif record.closed:
                record.pid_set = set()
                record.tgid_set = set()

        # Also detect newly opened from records that appeared since last update
        current_open = set(self.open_apps)
        for app_id in current_open - previously_open:
            self._newly_opened.add(app_id)
        for app_id in previously_open - current_open:
            self._newly_closed.add(app_id)

    @property
    def observed_apps(self) -> list[str]:
        return sorted(self.records)

    @property
    def open_apps(self) -> list[str]:
        return sorted(
            app_id
            for app_id, record in self.records.items()
            if record.first_seen_ns and not record.closed
        )

    @property
    def closed_apps(self) -> list[str]:
        return sorted(app_id for app_id, record in self.records.items() if record.closed)

    @property
    def newly_opened_apps(self) -> list[str]:
        return sorted(self._newly_opened)

    @property
    def newly_closed_apps(self) -> list[str]:
        return sorted(self._newly_closed)

    def summary(self) -> dict[str, str]:
        return {
            "observed_apps": "|".join(self.observed_apps),
            "open_apps": "|".join(self.open_apps),
            "closed_apps": "|".join(self.closed_apps),
            "newly_opened_apps": "|".join(self.newly_opened_apps),
            "newly_closed_apps": "|".join(self.newly_closed_apps),
            "app_history": "|".join(self.app_history),
            "duration_history": "|".join(self.duration_history),
        }

    def records_for_output(self) -> list[AppRecord]:
        return [self.records[app_id] for app_id in self.observed_apps]
