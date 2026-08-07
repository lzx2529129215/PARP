from __future__ import annotations

from bisect import bisect_right

from .models import VMARecord


class VMAIntervalIndex:
    def __init__(self, records: list[VMARecord]) -> None:
        self.records = sorted(records, key=lambda r: r.start_addr)
        self.starts = [record.start_addr for record in self.records]

    def find(self, addr: int) -> VMARecord | None:
        idx = bisect_right(self.starts, addr) - 1
        if idx < 0:
            return None
        record = self.records[idx]
        return record if record.start_addr <= addr < record.end_addr else None

    def overlaps(self, start: int, end: int) -> list[tuple[VMARecord, int, int]]:
        if end <= start:
            return []
        idx = max(0, bisect_right(self.starts, start) - 1)
        result: list[tuple[VMARecord, int, int]] = []
        while idx < len(self.records):
            record = self.records[idx]
            if record.start_addr >= end:
                break
            overlap_start = max(start, record.start_addr)
            overlap_end = min(end, record.end_addr)
            if overlap_end > overlap_start:
                result.append((record, overlap_start, overlap_end))
            idx += 1
        return result

