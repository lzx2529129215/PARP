from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any


STAT_FIELDS = [
    "anon",
    "file",
    "shmem",
    "pgfault",
    "pgmajfault",
    "workingset_refault_anon",
    "workingset_refault_file",
    "pgscan",
    "pgsteal",
    "pswpin",
    "pswpout",
]


@dataclass
class CgroupFeatureSample:
    values: dict[str, int | None]
    availability: dict[str, str]
    status: str
    error: str


class CgroupFeatureAdapter:
    def __init__(self) -> None:
        self.previous: dict[str, dict[str, int | None]] = {}

    def sample(self, scope_name: str, cgroup_path: Path) -> CgroupFeatureSample:
        values: dict[str, int | None] = {"memory_current": None}
        availability: dict[str, str] = {}
        if not cgroup_path.is_dir():
            return CgroupFeatureSample(values, availability, "missing_cgroup", f"missing cgroup: {cgroup_path}")
        memory_current = _read_int(cgroup_path / "memory.current")
        values["memory_current"] = memory_current
        availability["memory_current"] = "ok" if memory_current is not None else "missing"
        stat = _read_kv(cgroup_path / "memory.stat")
        for field in STAT_FIELDS:
            values[field] = stat.get(field)
            availability[field] = "ok" if field in stat else "missing"
        prev = self.previous.get(scope_name)
        for key in ["memory_current", *STAT_FIELDS]:
            current = values.get(key)
            delta_key = f"{key}_delta"
            if current is None or prev is None or prev.get(key) is None:
                values[delta_key] = None
                availability[delta_key] = "missing_previous" if current is not None else "missing"
            else:
                values[delta_key] = int(current) - int(prev[key])
                availability[delta_key] = "ok"
        self.previous[scope_name] = dict(values)
        return CgroupFeatureSample(values, availability, "ok", "")


def resolve_user_slice_path(slice_name: str) -> tuple[Path, str]:
    try:
        result = subprocess.run(
            ["systemctl", "--user", "show", slice_name, "-p", "ControlGroup", "--value"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return Path("/sys/fs/cgroup"), f"failed to run systemctl --user show: {exc}"
    if result.returncode != 0:
        return Path("/sys/fs/cgroup"), result.stderr.strip() or f"systemctl returned {result.returncode}"
    control_group = result.stdout.strip()
    if not control_group:
        return Path("/sys/fs/cgroup"), f"empty ControlGroup for {slice_name}"
    return Path("/sys/fs/cgroup") / control_group.lstrip("/"), ""


def _read_int(path: Path) -> int | None:
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _read_kv(path: Path) -> dict[str, int]:
    values: dict[str, int] = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            parts = line.split()
            if len(parts) != 2:
                continue
            try:
                values[parts[0]] = int(parts[1])
            except ValueError:
                continue
    except OSError:
        return {}
    return values

