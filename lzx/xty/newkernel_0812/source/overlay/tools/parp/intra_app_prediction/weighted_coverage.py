"""Union and max-weight sweep-line coverage for logical page intervals."""

from dataclasses import dataclass
from statistics import mean
from typing import Iterable, Tuple


@dataclass(frozen=True)
class IntervalEvidence:
    start_page: int
    end_page_exclusive: int
    active_ratio: float
    age: int

    def __post_init__(self):
        if self.start_page < 0 or self.end_page_exclusive <= self.start_page:
            raise ValueError("invalid half-open page interval")
        if not 0.0 <= self.active_ratio <= 1.0:
            raise ValueError("active_ratio outside [0,1]")


@dataclass(frozen=True)
class CoverageSummary:
    segment_page_count: int
    observed_unique_pages: int
    accessed_unique_pages: int
    coverage_ratio: float
    weighted_access_pages: float
    weighted_coverage_ratio: float
    access_intensity: float
    mean_age: float
    max_age: int
    region_count: int


def coverage_summary(segment_start: int, segment_end: int,
                     intervals: Iterable[IntervalEvidence]):
    if segment_start < 0 or segment_end <= segment_start:
        raise ValueError("invalid segment")
    clipped = []
    for item in intervals:
        start = max(segment_start, item.start_page)
        end = min(segment_end, item.end_page_exclusive)
        if start < end:
            clipped.append((start, end, item.active_ratio, item.age))
    page_count = segment_end - segment_start
    if not clipped:
        return CoverageSummary(page_count, 0, 0, 0.0, 0.0, 0.0,
                               0.0, 0.0, 0, 0)
    points = sorted({p for start, end, _, _ in clipped for p in (start, end)})
    observed = accessed = 0
    weighted = intensity = 0.0
    for left, right in zip(points, points[1:]):
        weights = [weight for start, end, weight, _ in clipped
                   if start < right and end > left]
        if not weights:
            continue
        length = right - left
        observed += length
        maximum = max(weights)
        if maximum > 0:
            accessed += length
        weighted += length * maximum
        intensity += length * sum(weights) / len(weights)
    coverage = min(1.0, accessed / page_count)
    weighted_ratio = min(1.0, weighted / page_count)
    return CoverageSummary(
        page_count, observed, accessed, coverage, weighted,
        weighted_ratio, intensity / page_count,
        mean(item[3] for item in clipped), max(item[3] for item in clipped),
        len(clipped))
