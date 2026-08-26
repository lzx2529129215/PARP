from __future__ import annotations

from pathlib import Path
from typing import Iterable


def read_key_values(path: str | Path) -> dict[str, int]:
    """Read whitespace-delimited kernel key/value files."""
    result: dict[str, int] = {}
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        parts = raw.split()
        if len(parts) < 2:
            continue
        try:
            result[parts[0].rstrip(":")] = int(parts[1])
        except ValueError:
            continue
    return result


def read_pressure(path: str | Path) -> dict[str, float | int]:
    """Read a PSI file into keys such as some.avg10 and full.total."""
    result: dict[str, float | int] = {}
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        parts = raw.split()
        if not parts:
            continue
        prefix = parts[0]
        for item in parts[1:]:
            key, value = item.split("=", 1)
            result[f"{prefix}.{key}"] = float(value) if key.startswith("avg") else int(value)
    return result


def read_io_stat(path: str | Path) -> dict[str, int]:
    """Sum cgroup v2 io.stat counters across devices."""
    totals: dict[str, int] = {}
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        parts = raw.split()
        for item in parts[1:]:
            key, value = item.split("=", 1)
            totals[key] = totals.get(key, 0) + int(value)
    return totals


def read_proc_cpu_stat(path: str | Path) -> dict[str, int]:
    """Read aggregate CPU jiffies from the first line of /proc/stat."""
    fields = ("user", "nice", "system", "idle", "iowait", "irq", "softirq", "steal", "guest", "guest_nice")
    first = Path(path).read_text(encoding="utf-8").splitlines()[0].split()
    if not first or first[0] != "cpu":
        raise ValueError(f"unexpected /proc/stat format in {path}")
    return {name: int(value) for name, value in zip(fields, first[1:])}


def sum_keys(values: dict[str, int], keys: Iterable[str]) -> int:
    return sum(values.get(key, 0) for key in keys)
