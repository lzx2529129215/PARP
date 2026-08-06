"""Global memory and vmstat collection."""

from __future__ import annotations

from pathlib import Path


MEMINFO_KEYS = [
    "MemTotal",
    "MemFree",
    "MemAvailable",
    "Buffers",
    "Cached",
    "SwapTotal",
    "SwapFree",
    "Active(file)",
    "Inactive(file)",
    "Active(anon)",
    "Inactive(anon)",
    "Dirty",
    "Writeback",
]

VMSTAT_KEYS = [
    "pgfault",
    "pgmajfault",
    "pswpin",
    "pswpout",
    "pgscan_kswapd",
    "pgscan_direct",
    "pgsteal_kswapd",
    "pgsteal_direct",
    "workingset_refault_file",
    "workingset_activate_file",
    "workingset_restore_file",
]


def _read_lines(path: str) -> list[str]:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []


def read_meminfo() -> dict[str, int]:
    values = {key: 0 for key in MEMINFO_KEYS}
    for line in _read_lines("/proc/meminfo"):
        if ":" not in line:
            continue
        key, rest = line.split(":", 1)
        if key not in values:
            continue
        parts = rest.strip().split()
        if parts:
            values[key] = int(parts[0])
    return values


def read_vmstat() -> dict[str, int]:
    values = {key: 0 for key in VMSTAT_KEYS}
    for line in _read_lines("/proc/vmstat"):
        parts = line.split()
        if len(parts) == 2 and parts[0] in values:
            values[parts[0]] = int(parts[1])
    return values

