"""Epoch-aligned fixed ten-second offline windows."""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

WINDOW_NS = 10_000_000_000


def window_bounds(event_time_ns: int, window_ns: int = WINDOW_NS):
    if event_time_ns < 0 or window_ns <= 0:
        raise ValueError("invalid event time/window")
    start = event_time_ns // window_ns * window_ns
    return start, start + window_ns


@dataclass
class EventWindow:
    boot_id: str
    session_id: str
    start_ns: int
    end_ns: int
    events: List[Tuple[int, Any]] = field(default_factory=list)


class EventWindowBuilder:
    def __init__(self, window_ns: int = WINDOW_NS,
                 watermark_ns: int = 0,
                 monotonic_anchor_ns: Optional[int] = None,
                 wall_anchor_ns: Optional[int] = None):
        self.window_ns = window_ns
        self.watermark_ns = watermark_ns
        self.monotonic_anchor_ns = monotonic_anchor_ns
        self.wall_anchor_ns = wall_anchor_ns
        self._events: Dict[Tuple[str, str, int], EventWindow] = {}
        self._newest_ns = 0
        self.late_events = 0

    def add(self, boot_id: str, session_id: str, event_time_ns: int,
            value: Any):
        if not boot_id or not session_id:
            raise ValueError("boot and session identity are required")
        if self._newest_ns and event_time_ns + self.watermark_ns < self._newest_ns:
            self.late_events += 1
        self._newest_ns = max(self._newest_ns, event_time_ns)
        start, end = window_bounds(event_time_ns, self.window_ns)
        window = self._events.setdefault(
            (boot_id, session_id, start),
            EventWindow(boot_id, session_id, start, end))
        window.events.append((event_time_ns, value))

    def windows(self):
        output = sorted(self._events.values(),
                        key=lambda item: (item.boot_id, item.session_id,
                                          item.start_ns))
        for item in output:
            item.events.sort(key=lambda event: event[0])
        return output

    def monotonic_to_wall(self, monotonic_ns: int):
        if self.monotonic_anchor_ns is None or self.wall_anchor_ns is None:
            raise ValueError("clock anchor is unavailable")
        return self.wall_anchor_ns + monotonic_ns - self.monotonic_anchor_ns
