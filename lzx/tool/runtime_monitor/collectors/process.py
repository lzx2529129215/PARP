"""Process discovery and procfs per-process stats."""

from __future__ import annotations

import hashlib
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.app_mapper import AppMapper, ProcessIdentity


CLK_TCK = os.sysconf(os.sysconf_names.get("SC_CLK_TCK", "SC_CLK_TCK"))


@dataclass
class ProcessSample:
    identity: ProcessIdentity
    app_id: str
    io: dict[str, int]
    status: dict[str, int]
    stat: dict[str, int]
    in_test_slice: bool = False


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _read_link(path: Path) -> str:
    try:
        return os.readlink(path)
    except OSError:
        return ""


def _parse_key_value_kb(text: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, rest = line.split(":", 1)
        parts = rest.strip().split()
        if not parts:
            continue
        try:
            out[key] = int(parts[0])
        except ValueError:
            continue
    return out


def _parse_io(text: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        try:
            out[key.strip()] = int(value.strip())
        except ValueError:
            pass
    return out


def _parse_stat(text: str) -> dict[str, int]:
    if not text:
        return {}
    try:
        right = text.rsplit(")", 1)[1].strip().split()
        return {
            "minflt": int(right[7]),
            "majflt": int(right[9]),
            "starttime": int(right[19]),
        }
    except (IndexError, ValueError):
        return {}


def _read_cgroup_path(pid: int) -> str:
    text = _read_text(Path("/proc") / str(pid) / "cgroup")
    for line in text.splitlines():
        parts = line.split(":", 2)
        if len(parts) == 3 and parts[0] == "0":
            return parts[2]
    return ""


def _read_cmdline_hash(pid: int) -> str:
    """SHA-256 hash of /proc/<pid>/cmdline, first 16 hex chars."""
    try:
        raw = (Path("/proc") / str(pid) / "cmdline").read_bytes()
        raw = raw.replace(b"\x00", b" ")
        return hashlib.sha256(raw).hexdigest()[:16]
    except OSError:
        return ""


def read_identity(pid: int) -> ProcessIdentity | None:
    proc = Path("/proc") / str(pid)
    if not proc.exists():
        return None
    comm = _read_text(proc / "comm").strip()
    exe_path = _read_link(proc / "exe")
    stat = _parse_stat(_read_text(proc / "stat"))
    start_time = ""
    if stat.get("starttime") is not None:
        start_time = str(stat["starttime"])
    status = _parse_key_value_kb(_read_text(proc / "status"))
    tgid = int(status.get("Tgid", pid))
    return ProcessIdentity(
        pid=pid,
        tgid=tgid,
        comm=comm,
        exe_path=exe_path,
        cgroup_path=_read_cgroup_path(pid),
        start_time=start_time,
        cmdline_hash=_read_cmdline_hash(pid),
    )


class ProcessCollector:
    def __init__(
        self,
        mapper: AppMapper,
        target_app: str,
        target_apps: list[str] | None = None,
        target_pid: int | None = None,
        target_comm: str | None = None,
        test_slice: str = "",
    ) -> None:
        self.mapper = mapper
        self.target_app = target_app
        self.target_apps = target_apps or [target_app]
        self.target_pid = target_pid
        self.target_comm = target_comm.lower() if target_comm else ""
        self.test_slice = test_slice

    def pid_in_test_slice(self, pid: int) -> bool:
        """Return True if *pid* belongs to *self.test_slice* subtree."""
        if not self.test_slice:
            return False
        cgroup_path = _read_cgroup_path(pid)
        return f"/{self.test_slice}/" in cgroup_path or cgroup_path.endswith(f"/{self.test_slice}")

    def sample(self) -> list[ProcessSample]:
        samples: list[ProcessSample] = []
        pids = [self.target_pid] if self.target_pid else self._all_pids()
        for pid in pids:
            if not pid:
                continue
            identity = read_identity(pid)
            if identity is None:
                continue
            app_id = self.mapper.map_process(identity)
            if self.target_comm and self.target_comm not in identity.comm.lower():
                continue
            if self.target_pid and not app_id:
                app_id = self.target_app
            if app_id not in self.target_apps:
                continue
            in_slice = self.pid_in_test_slice(pid)
            # When test_slice is active, only collect processes within it
            if self.test_slice and not in_slice:
                continue
            samples.append(
                ProcessSample(
                    identity=identity,
                    app_id=app_id,
                    io=_parse_io(_read_text(Path("/proc") / str(pid) / "io")),
                    status=_parse_key_value_kb(_read_text(Path("/proc") / str(pid) / "status")),
                    stat=_parse_stat(_read_text(Path("/proc") / str(pid) / "stat")),
                    in_test_slice=in_slice,
                )
            )
        return samples

    @staticmethod
    def _all_pids() -> list[int]:
        pids: list[int] = []
        for item in Path("/proc").iterdir():
            if item.name.isdigit():
                pids.append(int(item.name))
        return pids


def aggregate_procfs(samples: list[ProcessSample]) -> dict[str, int]:
    total = {
        "read_bytes": 0,
        "write_bytes": 0,
        "rchar": 0,
        "wchar": 0,
        "voluntary_ctxt_switches": 0,
        "nonvoluntary_ctxt_switches": 0,
        "VmRSS": 0,
        "VmSize": 0,
        "majflt": 0,
    }
    for sample in samples:
        for key in ("read_bytes", "write_bytes", "rchar", "wchar"):
            total[key] += int(sample.io.get(key, 0))
        for key in ("voluntary_ctxt_switches", "nonvoluntary_ctxt_switches", "VmRSS", "VmSize"):
            total[key] += int(sample.status.get(key, 0))
        total["majflt"] += int(sample.stat.get("majflt", 0))
    total["sample_time_ns"] = time.time_ns()
    return total
