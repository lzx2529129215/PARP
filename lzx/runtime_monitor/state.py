"""Runtime state transitions for desktop application events."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

from .mapping import AppMapper, MappingResult


PREDICT_EVENTS = {"Opened", "Switched", "APP_START", "APP_SWITCH"}
STATE_EVENTS = PREDICT_EVENTS | {"Closed", "Minimized", "APP_CLOSE", "APP_MINIMIZE", "APP_RESTORE"}


def event_timestamp(timestamp_ms: object | None = None) -> str:
    if timestamp_ms is None:
        now = dt.datetime.now()
    else:
        try:
            now = dt.datetime.fromtimestamp(int(timestamp_ms) / 1000.0)
        except (TypeError, ValueError, OSError):
            now = dt.datetime.now()
    return now.strftime("%Y-%m-%d %H:%M:%S")


@dataclass
class RuntimeUpdate:
    raw_event: dict[str, Any]
    mapping: MappingResult
    timestamp: str
    foreground_app: str | None
    opened_apps: list[str]
    history_apps: list[str]
    should_predict: bool
    csv_row: dict[str, str] | None


@dataclass
class RuntimeState:
    mapper: AppMapper
    user_id: str = "local"
    user_group: str = "通用用户"
    history_len: int = 5
    opened_apps: list[str] = field(default_factory=list)
    history_apps: list[str] = field(default_factory=list)
    foreground_app: str | None = None
    window_to_app: dict[str, str] = field(default_factory=dict)
    app_window_count: dict[str, int] = field(default_factory=dict)

    def handle_event(self, event: dict[str, Any]) -> RuntimeUpdate:
        event_type = str(event.get("event_type", "")).strip()
        timestamp = event_timestamp(event.get("timestamp_ms"))
        window_key = self._window_key(event)
        app_event_app = str(event.get("app") or event.get("new_app") or "").strip()
        if event_type.startswith("APP_") and app_event_app:
            mapping = MappingResult(app=app_event_app, source="app_event", value=app_event_app)
        else:
            mapping = self.mapper.map_event(event)
        if event_type not in STATE_EVENTS or not mapping.known:
            return RuntimeUpdate(
                raw_event=event,
                mapping=mapping,
                timestamp=timestamp,
                foreground_app=None,
                opened_apps=list(self.opened_apps),
                history_apps=list(self.history_apps),
                should_predict=False,
                csv_row=None,
            )

        app = str(mapping.app)
        should_predict = False
        csv_event_type: str | None = None
        if event_type in {"Opened", "APP_START"}:
            first_app_window = self._add_window(window_key, app)
            if first_app_window:
                self._add_opened(app)
                csv_event_type = "Opened"
                should_predict = True
        elif event_type in {"Switched", "APP_SWITCH"}:
            self._add_opened(app)
            if app != self.foreground_app:
                self._set_foreground(app)
                csv_event_type = "Switched"
                should_predict = True
        elif event_type in {"Closed", "APP_CLOSE"}:
            last_app_window = self._remove_window(window_key, app)
            if last_app_window:
                self._close_opened(app)
                csv_event_type = "Closed"
                if self.foreground_app == app:
                    self.foreground_app = None
        elif event_type in {"Minimized", "APP_MINIMIZE", "APP_RESTORE"}:
            csv_event_type = "Minimized" if event_type in {"Minimized", "APP_MINIMIZE"} else "Restored"

        csv_row = None
        if csv_event_type is not None:
            csv_row = {
                "user_id": self.user_id,
                "timestamp": timestamp,
                "foreground_app": app,
                "opened_apps": ";".join(self.opened_apps),
                "user_group": self.user_group,
                "event_type": csv_event_type,
            }

        return RuntimeUpdate(
            raw_event=event,
            mapping=mapping,
            timestamp=timestamp,
            foreground_app=app,
            opened_apps=list(self.opened_apps),
            history_apps=list(self.history_apps),
            should_predict=should_predict,
            csv_row=csv_row,
        )

    def _add_opened(self, app: str) -> None:
        if app not in self.opened_apps:
            self.opened_apps.append(app)

    def _close_opened(self, app: str) -> None:
        self.opened_apps = [opened for opened in self.opened_apps if opened != app]

    def _set_foreground(self, app: str) -> None:
        self.foreground_app = app
        if not self.history_apps or self.history_apps[-1] != app:
            self.history_apps.append(app)
            if len(self.history_apps) > self.history_len:
                self.history_apps = self.history_apps[-self.history_len :]

    def _add_window(self, window_key: str, app: str) -> bool:
        if not window_key:
            window_key = f"unknown:{app}"
        if window_key in self.window_to_app:
            return False
        previous_count = self.app_window_count.get(app, 0)
        self.window_to_app[window_key] = app
        self.app_window_count[app] = previous_count + 1
        return previous_count == 0

    def _remove_window(self, window_key: str, app: str) -> bool:
        tracked_app = self.window_to_app.pop(window_key, None)
        if tracked_app is None:
            tracked_app = app
        count = max(0, self.app_window_count.get(tracked_app, 0) - 1)
        if count:
            self.app_window_count[tracked_app] = count
            return False
        self.app_window_count.pop(tracked_app, None)
        return tracked_app == app

    @staticmethod
    def _window_key(event: dict[str, Any]) -> str:
        explicit = str(event.get("window_id") or "").strip()
        if explicit:
            return explicit
        pid = str(event.get("pid") or "").strip()
        wm_class = str(event.get("wm_class") or "").strip()
        title = str(event.get("title") or "").strip()
        return f"{pid}:{wm_class}:{title}"
