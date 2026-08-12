"""Leakage-safe operation interval alignment."""

from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class OperationEvent:
    operation_id: str
    app_id: str
    start_ns: int
    end_ns: int
    operation_type: str
    source: str
    confidence: float
    session_id: str

    def __post_init__(self):
        if self.end_ns <= self.start_ns or not 0 <= self.confidence <= 1:
            raise ValueError("invalid operation interval")


@dataclass(frozen=True)
class OperationAlignment:
    operation_at_start: str
    operation_at_end: str
    dominant_operation: str
    next_operation: str
    operation_transition: str
    operation_coverage_ratio: float
    operation_count: int
    state_changed: bool
    label_quality: str


def align_operations(window_start_ns: int, window_end_ns: int,
                     events: Iterable[OperationEvent],
                     next_operation: str = "UNKNOWN"):
    if window_end_ns <= window_start_ns:
        raise ValueError("invalid window")
    overlapping = []
    for event in events:
        overlap = max(0, min(window_end_ns, event.end_ns) -
                      max(window_start_ns, event.start_ns))
        if overlap:
            overlapping.append((event, overlap))
    at_start = next((event.operation_type for event, _ in overlapping
                     if event.start_ns <= window_start_ns < event.end_ns),
                    "UNKNOWN")
    at_end = next((event.operation_type for event, _ in reversed(overlapping)
                   if event.start_ns < window_end_ns <= event.end_ns),
                  "UNKNOWN")
    if not overlapping:
        return OperationAlignment(at_start, at_end, "UNKNOWN",
                                  next_operation, "NONE", 0.0, 0,
                                  False, "LOW_CONFIDENCE")
    dominant, duration = max(overlapping,
                             key=lambda item: (item[1], item[0].operation_type))
    ratio = duration / (window_end_ns - window_start_ns)
    quality = "PURE" if ratio >= .8 else (
        "MIXED" if ratio >= .5 else "LOW_CONFIDENCE")
    ordered = sorted(overlapping, key=lambda item: item[0].start_ns)
    transition = "NONE"
    distinct = []
    for event, _ in ordered:
        if not distinct or distinct[-1] != event.operation_type:
            distinct.append(event.operation_type)
    if len(distinct) > 1:
        transition = f"{distinct[0]}->{distinct[-1]}"
    return OperationAlignment(at_start, at_end, dominant.operation_type,
                              next_operation, transition, ratio,
                              len(overlapping), len(distinct) > 1, quality)
