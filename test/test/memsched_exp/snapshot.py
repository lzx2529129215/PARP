from __future__ import annotations

import os
import platform
import mmap
import gzip
import hashlib
import glob
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .readers import read_io_stat, read_key_values, read_pressure, read_proc_cpu_stat


@dataclass(frozen=True)
class Snapshot:
    monotonic_ns: int
    realtime_ns: int
    vmstat: dict[str, int]
    meminfo: dict[str, int]
    pressure_memory: dict[str, float | int]
    cpu_stat: dict[str, int]
    cgroup: dict[str, Any] | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _read_optional(path: Path, reader) -> dict:
    try:
        return reader(path)
    except (FileNotFoundError, PermissionError):
        return {}


def _read_with_status(path: Path, reader) -> tuple[dict, dict[str, Any]]:
    try:
        return reader(path), {"ok": True, "error": None}
    except (FileNotFoundError, PermissionError, OSError) as error:
        return {}, {"ok": False, "error": f"{type(error).__name__}: {error}"}


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8").strip()
    except (FileNotFoundError, PermissionError, OSError, UnicodeDecodeError):
        return None


def _cgroup_identity(cgroup: Path) -> dict[str, int] | None:
    try:
        stat = cgroup.stat()
    except (FileNotFoundError, PermissionError, OSError):
        return None
    return {"device": stat.st_dev, "inode": stat.st_ino}


def take_snapshot(proc_root: Path = Path("/proc"), cgroup: Path | None = None) -> Snapshot:
    cg: dict[str, Any] | None = None
    if cgroup is not None:
        readers = {
            "memory_stat": (cgroup / "memory.stat", read_key_values),
            "memory_events": (cgroup / "memory.events", read_key_values),
            "cpu_stat": (cgroup / "cpu.stat", read_key_values),
            "io_stat": (cgroup / "io.stat", read_io_stat),
            "pressure_memory": (cgroup / "memory.pressure", read_pressure),
        }
        values: dict[str, dict] = {}
        read_status: dict[str, dict[str, Any]] = {}
        for name, (path, reader) in readers.items():
            values[name], read_status[name] = _read_with_status(path, reader)
        cg = {
            "path": str(cgroup),
            "identity": _cgroup_identity(cgroup),
            "read_status": read_status,
            **values,
        }
    return Snapshot(
        monotonic_ns=time.monotonic_ns(),
        realtime_ns=time.time_ns(),
        vmstat=read_key_values(proc_root / "vmstat"),
        meminfo=read_key_values(proc_root / "meminfo"),
        pressure_memory=_read_optional(proc_root / "pressure" / "memory", read_pressure),
        cpu_stat=_read_optional(proc_root / "stat", read_proc_cpu_stat),
        cgroup=cg,
    )


def _kernel_config_metadata() -> dict[str, Any]:
    release = platform.release()
    candidates = [Path("/proc/config.gz"), Path(f"/boot/config-{release}")]
    for path in candidates:
        try:
            data = gzip.open(path, "rb").read() if path.suffix == ".gz" else path.read_bytes()
        except (FileNotFoundError, PermissionError, OSError):
            continue
        return {"path": str(path), "sha256": hashlib.sha256(data).hexdigest()}
    return {"path": None, "sha256": None}


def _swap_metadata() -> list[dict[str, Any]]:
    text = _read_text(Path("/proc/swaps"))
    if not text:
        return []
    rows: list[dict[str, Any]] = []
    for line in text.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 5:
            rows.append({"path": parts[0], "type": parts[1], "size_kib": int(parts[2]), "used_kib": int(parts[3]), "priority": int(parts[4])})
    return rows


def _zram_metadata() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for device in sorted(glob.glob("/sys/block/zram*")):
        base = Path(device)
        rows.append({
            "device": base.name,
            "disksize": _read_text(base / "disksize"),
            "compression_algorithm": _read_text(base / "comp_algorithm"),
        })
    return rows


def _mount_metadata(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    text = _read_text(Path("/proc/self/mountinfo"))
    if not text:
        return None
    resolved = path.resolve()
    matches: list[tuple[int, dict[str, Any]]] = []
    for line in text.splitlines():
        try:
            left, right = line.split(" - ", 1)
            left_fields = left.split()
            right_fields = right.split()
            mount_point = Path(left_fields[4].replace("\\040", " "))
            resolved.relative_to(mount_point)
        except (ValueError, IndexError):
            continue
        matches.append((len(str(mount_point)), {"mount_point": str(mount_point), "filesystem": right_fields[0], "source": right_fields[1]}))
    return max(matches, default=(0, None), key=lambda item: item[0])[1]


def _cpu_model() -> str | None:
    text = _read_text(Path("/proc/cpuinfo"))
    if not text:
        return None
    for line in text.splitlines():
        if line.lower().startswith("model name") and ":" in line:
            return line.split(":", 1)[1].strip()
    return None


def _cpu_frequency_constraints() -> list[dict[str, str | None]]:
    rows: list[dict[str, str | None]] = []
    for policy in sorted(Path("/sys/devices/system/cpu/cpufreq").glob("policy*")):
        rows.append(
            {
                "policy": policy.name,
                "min_khz": _read_text(policy / "scaling_min_freq"),
                "max_khz": _read_text(policy / "scaling_max_freq"),
                "governor": _read_text(policy / "scaling_governor"),
            }
        )
    return rows


def host_metadata(storage_path: Path | None = None) -> dict[str, Any]:
    sysctls = {
        key: _read_text(Path("/proc/sys") / Path(key.replace(".", "/")))
        for key in (
            "vm.swappiness",
            "vm.watermark_scale_factor",
            "vm.watermark_boost_factor",
            "vm.overcommit_memory",
            "vm.overcommit_ratio",
        )
    }
    governors = sorted({
        value
        for path in glob.glob("/sys/devices/system/cpu/cpu*/cpufreq/scaling_governor")
        if (value := _read_text(Path(path))) is not None
    })
    meminfo = _read_optional(Path("/proc/meminfo"), read_key_values)
    return {
        "hostname": platform.node(),
        "kernel": platform.release(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "cpu_model": _cpu_model(),
        "cpu_count": os.cpu_count() or 1,
        "page_size": mmap.PAGESIZE,
        "mem_total_kib": meminfo.get("MemTotal"),
        "kernel_cmdline": _read_text(Path("/proc/cmdline")),
        "kernel_config": _kernel_config_metadata(),
        "swap": _swap_metadata(),
        "zram": _zram_metadata(),
        "vm_sysctls": sysctls,
        "transparent_hugepage": {
            "enabled": _read_text(Path("/sys/kernel/mm/transparent_hugepage/enabled")),
            "defrag": _read_text(Path("/sys/kernel/mm/transparent_hugepage/defrag")),
        },
        "cpu_governors": governors,
        "cpu_frequency_constraints": _cpu_frequency_constraints(),
        "numa_nodes_online": _read_text(Path("/sys/devices/system/node/online")),
        "boot_id": _read_text(Path("/proc/sys/kernel/random/boot_id")),
        "session": {
            "type": os.environ.get("XDG_SESSION_TYPE"),
            "desktop": os.environ.get("XDG_CURRENT_DESKTOP"),
            "display": os.environ.get("DISPLAY"),
            "wayland_display": os.environ.get("WAYLAND_DISPLAY"),
        },
        "result_storage": _mount_metadata(storage_path),
    }
