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


def _common_cgroup_path(samples: list[ProcessSample]) -> Path | None:
    paths = [sample.identity.cgroup_path for sample in samples if sample.identity.cgroup_path]
    if not paths:
        return None
    parts = [path.strip("/").split("/") for path in paths]
    prefix: list[str] = []
    for items in zip(*parts):
        if len(set(items)) == 1:
            prefix.append(items[0])
        else:
            break
    if not prefix and paths:
        return CGROUP_ROOT / paths[0].strip("/")
    return CGROUP_ROOT.joinpath(*prefix) if prefix else CGROUP_ROOT


class AppResourceCollector:
    def sample(self, samples: list[ProcessSample]) -> dict[str, int | str]:
        cg_path = _common_cgroup_path(samples) if cgroup_v2_available() else None
        if cg_path and cg_path.exists():
            mem_stat = _read_kv_file(cg_path / "memory.stat")
            mem_events = _read_kv_file(cg_path / "memory.events")
            io_stat = _read_io_stat(cg_path / "io.stat")
            out: dict[str, int | str] = {
                "source": "cgroup_v2",
                "memory.current": _read_int(cg_path / "memory.current"),
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

