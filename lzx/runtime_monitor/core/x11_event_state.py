"""Convert native X11 notifications into Runtime Monitor APP_* events."""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from collectors.foreground import WindowState


DIRECT_PREDICTION_EVENTS = {
    "APP_SWITCH",
    "APP_OPEN",
}


@dataclass
class TrackedWindow:
    app: str
    pid: int
    title: str
    hidden: bool


class X11EventState:
    """Maintain app/window state without taking periodic X11 snapshots."""

    def __init__(self, resolver: Callable[[str], WindowState]) -> None:
        self.resolver = resolver
        self.windows: dict[str, TrackedWindow] = {}
        self.foreground_window_id = ""
        self.foreground_app = ""
        self.foreground_pid = 0
        self.foreground_title = ""
        self.foreground_since_ns = 0
        self._foreground_initialized = False
        self._announced_apps: set[str] = set()
        self._closed_apps: set[str] = set()

    @property
    def open_apps(self) -> list[str]:
        return sorted({item.app for item in self.windows.values() if _is_known_app(item.app)})

    def snapshot(self) -> dict[str, Any]:
        return {
            "foreground_app": self.foreground_app or "UNKNOWN",
            "foreground_pid": self.foreground_pid,
            "foreground_window_id": self.foreground_window_id,
            "foreground_window_title": self.foreground_title,
            "open_apps": "|".join(self.open_apps),
        }

    def handle(self, raw: dict[str, Any]) -> list[dict[str, Any]]:
        kind = str(raw.get("event_type", ""))
        window_id = str(raw.get("window_id", ""))
        if kind == "COLLECTOR_ERROR":
            return [
                {
                    "ts_ns": int(raw.get("timestamp_ns", 0) or 0),
                    "timestamp": dt.datetime.now().isoformat(timespec="milliseconds"),
                    "event_type": "X11_COLLECTOR_ERROR",
                    "app": "",
                    "source": "x11-event",
                    "native_event": str(raw.get("error", "")),
                }
            ]
        if kind == "WINDOW_INITIAL":
            current = self._remember(window_id)
            if current is not None and _is_known_app(current.app):
                self._announced_apps.add(current.app)
            return []
        if kind == "CGROUP_APP_EMPTY":
            return self._close_from_cgroup(str(raw.get("app", "")), raw)
        if kind in {"WINDOW_CREATED", "WINDOW_MAPPED", "WINDOW_PROPERTY"}:
            previous = self.windows.get(window_id)
            current = self._remember(window_id)
            result: list[dict[str, Any]] = []
            if current is not None and _is_known_app(current.app) and current.app not in self._announced_apps:
                # Client-list/property notifications can arrive after an
                # initially incomplete Create/Map notification.  Announce
                # the launch once metadata becomes usable.
                result.append(self._lifecycle("APP_OPEN", current.app, window_id, current, raw))
                self._announced_apps.add(current.app)
                self._closed_apps.discard(current.app)
            if (
                kind == "WINDOW_MAPPED"
                and current is not None
                and _is_known_app(current.app)
                and previous is not None
                and previous.hidden
                and not current.hidden
            ):
                result.append(self._foreground("APP_RESTORE", current.app, window_id, current, raw))
            if kind == "WINDOW_PROPERTY" and current is not None and previous is not None:
                if previous.hidden != current.hidden:
                    result.append(self._foreground(
                        "APP_MINIMIZE" if current.hidden else "APP_RESTORE",
                        current.app, window_id, current, raw,
                    ))
                result.extend(self._refresh_foreground_mapping(window_id, current, raw))
            return result
        if kind == "WINDOW_UNMAPPED":
            previous = self.windows.get(window_id)
            current = self._remember(window_id)
            # Most EWMH window managers set _NET_WM_STATE_HIDDEN and emit a
            # PropertyNotify.  This fallback covers WMs that only unmap.
            if current is not None and current.hidden and (previous is None or not previous.hidden):
                return [self._foreground("APP_MINIMIZE", current.app, window_id, current, raw)]
            return []
        if kind == "WINDOW_DESTROYED":
            previous = self.windows.pop(window_id, None)
            if previous is None or not _is_known_app(previous.app):
                return []
            if previous.app not in self.open_apps:
                self._announced_apps.discard(previous.app)
                self._closed_apps.add(previous.app)
                return [self._lifecycle("APP_CLOSE", previous.app, window_id, previous, raw)]
            return []
        if kind in {"FOCUS_CHANGED", "FOCUS_RECHECK"}:
            return self._focus_changed(window_id, raw)
        return []

    def _focus_changed(self, window_id: str, raw: dict[str, Any]) -> list[dict[str, Any]]:
        previous_app = self.foreground_app
        previous_window = self.foreground_window_id
        previous_since_ns = self.foreground_since_ns
        current = self._remember(window_id)
        current_app = current.app if current is not None and _is_known_app(current.app) else "UNKNOWN"
        current_pid = current.pid if current is not None else 0
        current_title = current.title if current is not None else ""
        self.foreground_window_id = window_id
        self.foreground_app = current_app
        self.foreground_pid = current_pid
        self.foreground_title = current_title
        self.foreground_since_ns = int(raw.get("timestamp_ns", 0) or 0)
        result: list[dict[str, Any]] = []
        if current is not None and _is_known_app(current.app) and current.app not in self._announced_apps:
            # A focus recheck can be the first notification carrying stable
            # metadata.  It is a genuine observable APP_OPEN in that case.
            result.append(self._lifecycle("APP_OPEN", current.app, window_id, current, raw))
            self._announced_apps.add(current.app)
            self._closed_apps.discard(current.app)
        if not self._foreground_initialized:
            self._foreground_initialized = True
            return result
        if previous_app == current_app and previous_window == window_id:
            return result
        # A different window of the same application is not an application
        # switch; it remains represented by the native event in the audit log.
        if previous_app == current_app:
            return result
        if previous_app:
            previous_duration_ms = max(0, int((int(raw.get("timestamp_ns", 0) or 0) - previous_since_ns) / 1_000_000)) if previous_since_ns else 0
            result.append(self._foreground(
                "APP_FOCUS_OUT", previous_app, previous_window,
                TrackedWindow(previous_app, 0, "", False), raw,
                old_app=previous_app, new_app=current_app,
                duration_ms=previous_duration_ms,
            ))
        else:
            previous_duration_ms = 0
        result.append(self._foreground(
            "APP_SWITCH", current_app, window_id,
            current or TrackedWindow(current_app, current_pid, current_title, False), raw,
            old_app=previous_app, new_app=current_app,
            duration_ms=previous_duration_ms,
        ))
        result.append(self._foreground(
            "APP_FOCUS_IN", current_app, window_id,
            current or TrackedWindow(current_app, current_pid, current_title, False), raw,
            old_app=previous_app, new_app=current_app,
        ))
        return result

    def _refresh_foreground_mapping(
        self, window_id: str, current: TrackedWindow, raw: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """Promote an active window once its delayed metadata is available."""
        if window_id != self.foreground_window_id or not _is_known_app(current.app):
            return []
        if self.foreground_app == current.app:
            return []
        return self._focus_changed(window_id, raw)

    def _close_from_cgroup(self, app: str, raw: dict[str, Any]) -> list[dict[str, Any]]:
        """Close an app when its controlled cgroup has become empty.

        This is a lifecycle fallback for applications terminated by the test
        harness.  It does not trigger inference; foreground predictions remain
        driven solely by direct X11 APP_OPEN/APP_SWITCH events.
        """
        if not _is_known_app(app):
            return []
        if app in self._closed_apps:
            return []
        matching = [
            (window_id, window)
            for window_id, window in self.windows.items()
            if window.app == app
        ]
        for window_id, _window in matching:
            self.windows.pop(window_id, None)
        self._announced_apps.discard(app)
        self._closed_apps.add(app)
        if self.foreground_app == app:
            self.foreground_app = "UNKNOWN"
            self.foreground_pid = 0
            self.foreground_title = ""
        anchor = matching[0][1] if matching else TrackedWindow(app, 0, "", False)
        window_id = matching[0][0] if matching else ""
        return [self._lifecycle("APP_CLOSE", app, window_id, anchor, raw)]

    def _remember(self, window_id: str) -> TrackedWindow | None:
        if not window_id:
            return None
        try:
            state = self.resolver(window_id)
        except Exception:
            state = WindowState(window_id=window_id, app="UNKNOWN", source="x11-event")
        if not state.window_id:
            return self.windows.get(window_id)
        if not state.app:
            # Override-redirect/selection/helper windows are not application
            # windows and must not enter open_apps or prediction history.
            self.windows.pop(window_id, None)
            return None
        current = TrackedWindow(
            app=state.app or "UNKNOWN",
            pid=int(state.pid or 0),
            title=state.window_title or "",
            hidden=bool(state.is_hidden),
        )
        self.windows[window_id] = current
        return current

    def _base(self, event_type: str, app: str, window_id: str, window: TrackedWindow, raw: dict[str, Any]) -> dict[str, Any]:
        ts_ns = int(raw.get("timestamp_ns", 0) or 0)
        timestamp = dt.datetime.fromtimestamp(ts_ns / 1_000_000_000).isoformat(timespec="milliseconds")
        return {
            "ts_ns": ts_ns,
            "timestamp": timestamp,
            "event_type": event_type,
            "app": app,
            "old_app": "",
            "new_app": app,
            "foreground_app": self.foreground_app or app,
            "duration_ms": max(0, int((ts_ns - self.foreground_since_ns) / 1_000_000)) if self.foreground_since_ns else 0,
            "window_id": window_id,
            "window_title": window.title,
            "pid": window.pid,
            "tgid": "",
            "open_apps_before": "",
            "open_apps_after": "|".join(self.open_apps),
            "pid_count_before": "",
            "pid_count_after": "",
            "source": str(raw.get("source") or "x11-event"),
            "native_event": str(raw.get("event_type", "")),
        }

    def _foreground(
        self,
        event_type: str,
        app: str,
        window_id: str,
        window: TrackedWindow,
        raw: dict[str, Any],
        *,
        old_app: str = "",
        new_app: str = "",
        duration_ms: int | None = None,
    ) -> dict[str, Any]:
        row = self._base(event_type, app, window_id, window, raw)
        row.update({"old_app": old_app, "new_app": new_app or app, "foreground_app": self.foreground_app or app})
        if duration_ms is not None:
            row["duration_ms"] = duration_ms
        return row

    def _lifecycle(
        self,
        event_type: str,
        app: str,
        window_id: str,
        window: TrackedWindow,
        raw: dict[str, Any],
    ) -> dict[str, Any]:
        before = set(self.open_apps)
        if event_type == "APP_OPEN":
            before.discard(app)
        if event_type == "APP_CLOSE":
            before.add(app)
        row = self._base(event_type, app, window_id, window, raw)
        row.update({
            "open_apps_before": "|".join(sorted(before)),
            "open_apps_after": "|".join(self.open_apps),
            "pid_count_after": 0 if event_type == "APP_CLOSE" else 1,
        })
        return row


def _is_known_app(app: str) -> bool:
    return bool(app and app != "UNKNOWN")
