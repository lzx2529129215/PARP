"""cgroup v2 collection with procfs fallback support."""

from __future__ import annotations

from pathlib import Path

from collectors.process import ProcessSample, aggregate_procfs


CGROUP_ROOT = Path("/sys/fs/cgroup")
MEM_STAT_KEYS = [
    "anon",
    "file",
    "active_file",
    "inactive_file",
    "active_anon",
    "inactive_anon",
    "pgfault",
    "pgmajfault",
    "workingset_refault_file",
]


def cgroup_v2_available() -> bool:
    return (CGROUP_ROOT / "cgroup.controllers").exists()


def _read_int(path: Path) -> int:
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return 0


def _read_kv_file(path: Path) -> dict[str, int]:
    out: dict[str, int] = {}
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return out
    for line in lines:
        parts = line.split()
        if len(parts) == 2:
            try:
                out[parts[0]] = int(parts[1])
            except ValueError:
                pass
    return out


def _read_io_stat(path: Path) -> dict[str, int]:
    total = {"rbytes": 0, "wbytes": 0, "rios": 0, "wios": 0}
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return total
    for line in lines:
        for item in line.split()[1:]:
            if "=" not in item:
                continue
            key, value = item.split("=", 1)
            if key in total:
                try:
                    total[key] += int(value)
                except ValueError:
                    pass
    return total


def _exact_cgroup_paths(samples: list[ProcessSample]) -> list[Path]:
    """Return unique leaf cgroups represented by the application's processes."""
    # lzx-note: Using a common ancestor for an app split across a transient
    # scope and a D-Bus service counts unrelated processes in the whole slice.
    return sorted({
        CGROUP_ROOT / sample.identity.cgroup_path.strip("/")
        for sample in samples
        if sample.identity.cgroup_path
    })


class AppResourceCollector:
    def sample(self, samples: list[ProcessSample]) -> dict[str, int | str]:
        cg_paths = _exact_cgroup_paths(samples) if cgroup_v2_available() else []
        existing_paths = [path for path in cg_paths if path.exists()]
        if existing_paths:
            mem_stat = {key: 0 for key in MEM_STAT_KEYS}
            mem_events = {key: 0 for key in ("low", "high", "max", "oom")}
            io_stat = {key: 0 for key in ("rbytes", "wbytes", "rios", "wios")}
            memory_current = 0
            for cg_path in existing_paths:
                memory_current += _read_int(cg_path / "memory.current")
                one_mem_stat = _read_kv_file(cg_path / "memory.stat")
                one_mem_events = _read_kv_file(cg_path / "memory.events")
                one_io_stat = _read_io_stat(cg_path / "io.stat")
                for key in mem_stat:
                    mem_stat[key] += one_mem_stat.get(key, 0)
                for key in mem_events:
                    mem_events[key] += one_mem_events.get(key, 0)
                for key in io_stat:
                    io_stat[key] += one_io_stat.get(key, 0)
            out: dict[str, int | str] = {
                "source": "cgroup_v2",
                "memory.current": memory_current,
            }
            for key in MEM_STAT_KEYS:
                out[f"memory.stat.{key}"] = mem_stat.get(key, 0)
            for key in ("low", "high", "max", "oom"):
                out[f"memory.events.{key}"] = mem_events.get(key, 0)
            for key, value in io_stat.items():
                out[f"io.stat.{key}"] = value
            return out

        proc = aggregate_procfs(samples)
        return {
            "source": "procfs",
            "memory.current": proc.get("VmRSS", 0) * 1024,
            "memory.stat.anon": 0,
            "memory.stat.file": 0,
            "memory.stat.active_file": 0,
            "memory.stat.inactive_file": 0,
            "memory.stat.active_anon": 0,
            "memory.stat.inactive_anon": 0,
            "memory.stat.pgfault": 0,
            "memory.stat.pgmajfault": proc.get("majflt", 0),
            "memory.stat.workingset_refault_file": 0,
            "io.stat.rbytes": proc.get("read_bytes", 0),
            "io.stat.wbytes": proc.get("write_bytes", 0),
            "io.stat.rios": 0,
            "io.stat.wios": 0,
        }
