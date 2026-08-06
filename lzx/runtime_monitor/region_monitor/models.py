from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class CapabilityStatus(str, Enum):
    SUPPORTED = "SUPPORTED"
    SUPPORTED_NEEDS_ROOT = "SUPPORTED_NEEDS_ROOT"
    MISSING_CONFIG = "MISSING_CONFIG"
    MISSING_SYSFS = "MISSING_SYSFS"
    MISSING_TRACEPOINT = "MISSING_TRACEPOINT"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    UNSUPPORTED = "UNSUPPORTED"


class MappingType(str, Enum):
    FILE = "FILE"
    SHARED_LIBRARY = "SHARED_LIBRARY"
    DOCUMENT_FILE = "DOCUMENT_FILE"
    ANON_HEAP = "ANON_HEAP"
    ANON_STACK = "ANON_STACK"
    NAMED_ANON = "NAMED_ANON"
    GRAPHICS_DEVICE = "GRAPHICS_DEVICE"
    OTHER_DEVICE = "OTHER_DEVICE"
    UNKNOWN_ANON = "UNKNOWN_ANON"


class IdentityConfidence(str, Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


@dataclass(frozen=True)
class RegionMonitorConfig:
    enabled: bool = False
    target_apps: list[str] = field(default_factory=lambda: ["WPS"])
    pid_refresh_ms: int = 1000
    vma_refresh_ms: int = 1000
    region_bucket_bytes: int = 262144
    damon_sample_us: int = 5000
    damon_aggr_us: int = 100000
    damon_update_us: int = 1000000
    damon_min_nr_regions: int = 10
    damon_max_nr_regions: int = 1000
    region_window_ms: int = 500
    cgroup_window_ms: int = 1000
    anonymous_regions_enabled: bool = True
    anonymous_protection_eligible: bool = False
    file_regions_enabled: bool = True
    file_protection_eligible: bool = False
    process_role_rules: dict[str, list[str]] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RegionMonitorConfig":
        damon = data.get("damon", {}) if isinstance(data.get("damon"), dict) else {}
        windows = data.get("windows", {}) if isinstance(data.get("windows"), dict) else {}
        anon = data.get("anonymous_regions", {}) if isinstance(data.get("anonymous_regions"), dict) else {}
        file_regions = data.get("file_regions", {}) if isinstance(data.get("file_regions"), dict) else {}
        rules = data.get("process_role_rules", {}) if isinstance(data.get("process_role_rules"), dict) else {}
        cfg = cls(
            enabled=bool(data.get("enabled", False)),
            target_apps=[str(x) for x in data.get("target_apps", ["WPS"])],
            pid_refresh_ms=int(data.get("pid_refresh_ms", 1000)),
            vma_refresh_ms=int(data.get("vma_refresh_ms", 1000)),
            region_bucket_bytes=int(data.get("region_bucket_bytes", 262144)),
            damon_sample_us=int(damon.get("sample_us", 5000)),
            damon_aggr_us=int(damon.get("aggr_us", 100000)),
            damon_update_us=int(damon.get("update_us", 1000000)),
            damon_min_nr_regions=int(damon.get("min_nr_regions", 10)),
            damon_max_nr_regions=int(damon.get("max_nr_regions", 1000)),
            region_window_ms=int(windows.get("region_window_ms", 500)),
            cgroup_window_ms=int(windows.get("cgroup_window_ms", 1000)),
            anonymous_regions_enabled=bool(anon.get("enabled", True)),
            anonymous_protection_eligible=bool(anon.get("protection_eligible", False)),
            file_regions_enabled=bool(file_regions.get("enabled", True)),
            file_protection_eligible=bool(file_regions.get("protection_eligible", False)),
            process_role_rules={str(k): [str(v) for v in values] for k, values in rules.items() if isinstance(values, list)},
        )
        cfg.validate()
        return cfg

    def validate(self) -> None:
        if self.region_bucket_bytes <= 0:
            raise ValueError("region_bucket_bytes must be positive")
        if self.pid_refresh_ms <= 0 or self.vma_refresh_ms <= 0:
            raise ValueError("refresh intervals must be positive")
        if self.region_window_ms <= 0 or self.cgroup_window_ms <= 0:
            raise ValueError("window intervals must be positive")
        if self.anonymous_protection_eligible or self.file_protection_eligible:
            raise ValueError("region monitor v1 is observe-only; protection_eligible must be false")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ProcessInfo:
    pid: int
    process_starttime: int
    comm: str
    cmdline: str
    exe: str
    process_role: str
    first_seen_ns: int
    last_seen_ns: int
    status: str

    @property
    def identity(self) -> tuple[int, int]:
        return (self.pid, self.process_starttime)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class VMARecord:
    pid: int
    process_starttime: int
    process_role: str
    start_addr: int
    end_addr: int
    size_bytes: int
    permissions: str
    file_offset: int
    dev_major: int
    dev_minor: int
    inode: int
    pathname: str
    anon_name: str
    mapping_type: str


@dataclass(frozen=True)
class DamonEvent:
    timestamp_ns: int
    target_id: str
    start: int
    end: int
    nr_accesses: float
    age: int
    nr_regions: int
    raw_line: str

    @property
    def size_bytes(self) -> int:
        return max(0, self.end - self.start)


@dataclass(frozen=True)
class RegionObservation:
    region_id: int
    region_type: str
    stable_key: str
    canonical_key: str
    weighted_accesses: float
    age: int
    observed_bytes: int
    resolution_confidence: float
    identity_confidence: str
    process_role: str
    canonical_region_id: int
    low_resolution: bool


@dataclass
class CapabilityReport:
    status: str
    uname_r: str
    config_source: str
    configs: dict[str, str]
    damon_sysfs_path: str
    tracefs_path: str
    tracepoint_path: str
    permissions: dict[str, str]
    errors: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

