"""Shared immutable schemas for Phase2.7."""

from dataclasses import asdict, dataclass
from typing import Any, Dict, Optional


@dataclass(frozen=True, order=True)
class FileIdentity:
    dev_major: int
    dev_minor: int
    inode: int
    file_version: int

    def __post_init__(self):
        if min(self.dev_major, self.dev_minor, self.inode,
               self.file_version) < 0:
            raise ValueError("file identity fields must be nonnegative")

    @property
    def stable_key(self) -> str:
        return f"{self.dev_major}:{self.dev_minor}:{self.inode}:{self.file_version}"


@dataclass(frozen=True)
class SegmentSpec:
    requested_bins: int
    effective_bins: int
    segment_id: int
    start_page: int
    end_page_exclusive: int
    file_page_count: int
    partition_generation: int = 1


@dataclass(frozen=True)
class FileRegion:
    timestamp_ns: int
    collection_time_ns: int
    snapshot_publish_ns: int
    boot_id: str
    session_id: str
    run_id: str
    sample_id: int
    domain_id: int
    app_id: int
    pid: int
    tgid: int
    identity: FileIdentity
    file_page_start: int
    file_page_end_exclusive: int
    file_size_bytes: int
    file_page_count: int
    nr_accesses: int
    max_possible_accesses: int
    age: int
    bind_generation: int
    model_version: int
    foreground_epoch: int

    @property
    def active_ratio(self) -> float:
        if self.max_possible_accesses <= 0:
            return 0.0
        return min(1.0, max(0.0, self.nr_accesses /
                            self.max_possible_accesses))


@dataclass(frozen=True)
class AnonRegion:
    timestamp_ns: int
    boot_id: str
    session_id: str
    run_id: str
    domain_id: int
    foreground_epoch: int
    mm_cookie: int
    vma_signature: int
    relative_start_pages: int
    nr_pages: int
    nr_accesses: int
    max_possible_accesses: int
    age: int


def dataclass_dict(value: Any) -> Dict[str, Any]:
    return asdict(value)


def optional_int(value: Any) -> Optional[int]:
    if value in (None, ""):
        return None
    return int(value)
