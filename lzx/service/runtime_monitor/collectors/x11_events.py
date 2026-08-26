"""Native X11 event collector.

This collector blocks in XNextEvent/select and therefore does not poll the
active window.  It reports low-level events; the monitor converts them into
the stable APP_* event schema after resolving the window metadata.
"""

from __future__ import annotations

import os
import select
import threading
import time
from collections.abc import Callable
from typing import Any


class X11EventCollector:
    """Listen for X11 window lifecycle, focus and state events."""

    def __init__(self, callback: Callable[[dict[str, Any]], None], display_name: str | None = None) -> None:
        self.callback = callback
        self.display_name = display_name
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._wake_r, self._wake_w = os.pipe()
        self._display: Any | None = None
        self._root: Any | None = None
        self._atoms: dict[str, int] = {}
        self._watched_windows: set[int] = set()
        # _NET_CLIENT_LIST names the actual EWMH client windows.  This is
        # more reliable than Root children under a reparenting window manager
        # (such as Openbox), where the client can be below a frame window.
        self._client_window_ids: set[int] = set()

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="runtime-monitor-x11-events", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        try:
            os.write(self._wake_w, b"x")
        except OSError:
            pass
        if self._thread is not None:
            self._thread.join(timeout=3.0)
        self._thread = None
        for fd in (self._wake_r, self._wake_w):
            try:
                os.close(fd)
            except OSError:
                pass
        self._wake_r = self._wake_w = -1

    def _run(self) -> None:
        try:
            from Xlib import X, display, error
        except ImportError as exc:
            self._emit("COLLECTOR_ERROR", error=f"python-xlib unavailable: {exc}")
            return

        try:
            self._display = display.Display(self.display_name)
            # Windows can disappear between a native notification and the
            # metadata lookup.  Treat BadWindow as an event-time miss rather
            # than printing an asynchronous X protocol traceback.
            self._display.set_error_handler(lambda *_args: None)
            self._root = self._display.screen().root
            self._atoms = {
                name: self._display.intern_atom(name, only_if_exists=False)
                for name in (
                    "_NET_ACTIVE_WINDOW",
                    "_NET_CLIENT_LIST",
                    "_NET_CLIENT_LIST_STACKING",
                    "_NET_WM_STATE",
                    "_NET_WM_STATE_HIDDEN",
                    "_NET_WM_NAME",
                    "_NET_WM_PID",
                    "WM_CLASS",
                    "WM_NAME",
                    "WM_STATE",
                )
            }
            self._root.change_attributes(event_mask=X.PropertyChangeMask | X.SubstructureNotifyMask)
            self._display.flush()

            # Establish a baseline without manufacturing APP_OPEN events for
            # windows that existed before the monitor started.
            for window in self._query_children():
                self._watch_window(window)
                self._emit("WINDOW_INITIAL", window_id=self._window_id(window))
            self._sync_client_windows(emit_changes=False)
            self._emit("FOCUS_CHANGED", window_id=self._active_window_id())

            while not self._stop.is_set():
                readable, _, _ = select.select(
                    [self._display.fileno(), self._wake_r], [], [], None
                )
                if self._wake_r in readable:
                    try:
                        os.read(self._wake_r, 4096)
                    except OSError:
                        pass
                    break
                if self._display.fileno() not in readable:
                    continue
                while self._display.pending_events():
                    self._handle_event(self._display.next_event(), X, error)
        except Exception as exc:  # keep monitor failure explicit and bounded
            self._emit("COLLECTOR_ERROR", error=f"x11 collector stopped: {type(exc).__name__}: {exc}")
        finally:
            if self._display is not None:
                try:
                    self._display.close()
                except Exception:
                    pass
            self._display = None

    def _handle_event(self, event: Any, X: Any, error: Any) -> None:
        event_type = int(getattr(event, "type", -1))
        if event_type == X.PropertyNotify:
            atom_name = self._atom_name(getattr(event, "atom", 0))
            window_id = self._window_id(getattr(event, "window", None))
            event_window = getattr(event, "window", None)
            is_root = (
                event_window is not None
                and self._root is not None
                and int(getattr(event_window, "id", 0)) == int(getattr(self._root, "id", -1))
            )
            if is_root and atom_name == "_NET_ACTIVE_WINDOW":
                active_window_id = self._active_window_id()
                self._emit("FOCUS_CHANGED", window_id=active_window_id, atom=atom_name)
                # Some toolkits publish the active-window property before
                # WM_CLASS/_NET_WM_NAME is stable.  A single, event-triggered
                # recheck resolves that race without falling back to periodic
                # foreground polling.
                self._schedule_focus_recheck(active_window_id)
            elif is_root and atom_name == "_NET_CLIENT_LIST":
                self._sync_client_windows(emit_changes=True)
            elif atom_name in {
                "_NET_WM_STATE", "_NET_WM_NAME", "WM_NAME", "WM_CLASS", "_NET_WM_PID", "WM_STATE",
            } and window_id:
                self._emit("WINDOW_PROPERTY", window_id=window_id, atom=atom_name)
            return

        window = getattr(event, "window", None)
        window_id = self._window_id(window)
        if event_type == X.CreateNotify:
            self._watch_window(window)
            self._emit("WINDOW_CREATED", window_id=window_id)
        elif event_type == X.MapNotify:
            self._watch_window(window)
            self._emit("WINDOW_MAPPED", window_id=window_id)
        elif event_type == X.UnmapNotify:
            self._emit("WINDOW_UNMAPPED", window_id=window_id)
        elif event_type == X.DestroyNotify:
            self._emit("WINDOW_DESTROYED", window_id=window_id)
            self._watched_windows.discard(getattr(window, "id", 0))
            self._client_window_ids.discard(getattr(window, "id", 0))

    def _query_children(self) -> list[Any]:
        if self._root is None:
            return []
        try:
            return list(self._root.query_tree().children)
        except Exception:
            return []

    def _watch_window(self, window: Any) -> None:
        if window is None or getattr(window, "id", 0) in self._watched_windows:
            return
        try:
            from Xlib import X

            window.change_attributes(event_mask=X.PropertyChangeMask | X.StructureNotifyMask)
            self._watched_windows.add(int(window.id))
            if self._display is not None:
                self._display.flush()
        except Exception:
            # A window can disappear between Create/Map and metadata watch.
            return

    def _query_client_windows(self) -> list[Any]:
        if self._root is None or self._display is None:
            return []
        try:
            prop = self._root.get_full_property(self._atoms["_NET_CLIENT_LIST"], 0)
            if prop is None:
                return []
            return [
                self._display.create_resource_object("window", int(window_id))
                for window_id in prop.value
                if int(window_id)
            ]
        except Exception:
            return []

    def _sync_client_windows(self, *, emit_changes: bool) -> None:
        windows = {int(window.id): window for window in self._query_client_windows()}
        current_ids = set(windows)
        added = current_ids - self._client_window_ids
        removed = self._client_window_ids - current_ids
        self._client_window_ids = current_ids

        for window_id in sorted(added):
            window = windows[window_id]
            self._watch_window(window)
            if emit_changes:
                self._emit("WINDOW_CREATED", window_id=self._format_window_id(window_id))
        for window_id in sorted(removed):
            if emit_changes:
                self._emit("WINDOW_DESTROYED", window_id=self._format_window_id(window_id))
            self._watched_windows.discard(window_id)

    def _schedule_focus_recheck(self, window_id: str) -> None:
        if not window_id:
            return

        def recheck() -> None:
            if not self._stop.is_set():
                self._emit("FOCUS_RECHECK", window_id=window_id, trigger="_NET_ACTIVE_WINDOW")

        timer = threading.Timer(0.15, recheck)
        timer.daemon = True
        timer.start()

    def _active_window_id(self) -> str:
        if self._root is None or not self._atoms:
            return ""
        try:
            prop = self._root.get_full_property(self._atoms["_NET_ACTIVE_WINDOW"], 0)
            if prop is not None and len(prop.value):
                return self._format_window_id(int(prop.value[0]))
        except Exception:
            pass
        return ""

    def _atom_name(self, atom: int) -> str:
        if self._display is None:
            return ""
        try:
            return str(self._display.get_atom_name(atom))
        except Exception:
            return ""

    @staticmethod
    def _format_window_id(window_id: int) -> str:
        return f"0x{int(window_id):x}" if int(window_id) else ""

    def _window_id(self, window: Any) -> str:
        if window is None:
            return ""
        try:
            return self._format_window_id(int(window.id))
        except Exception:
            return ""

    def _emit(self, event_type: str, **values: Any) -> None:
        payload = {
            "event_type": event_type,
            "timestamp_ns": time.time_ns(),
            "source": "x11-event",
            **values,
        }
        try:
            self.callback(payload)
        except Exception:
            # The monitor owns error reporting; the listener must not die
            # because a callback encountered a malformed window.
            return
