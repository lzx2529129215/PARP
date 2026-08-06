from __future__ import annotations

import time
from pathlib import Path

from .models import ProcessInfo
from .process_role_resolver import ProcessRoleResolver


class CgroupPidTracker:
    def __init__(self, app_id: str, cgroup_path: str | Path, role_resolver: ProcessRoleResolver) -> None:
        self.app_id = str(app_id)
        self.cgroup_path = Path(cgroup_path)
        self.role_resolver = role_resolver
        self.processes: dict[tuple[int, int], ProcessInfo] = {}

    def refresh(self, now_ns: int | None = None) -> tuple[list[ProcessInfo], list[ProcessInfo]]:
        now_ns = now_ns or time.time_ns()
        current: dict[tuple[int, int], ProcessInfo] = {}
        for pid in self._read_pids():
            info = self._read_process(pid, now_ns)
            if info is not None:
                current[info.identity] = info

        added: list[ProcessInfo] = []
        exited: list[ProcessInfo] = []
        for identity, info in current.items():
            previous = self.processes.get(identity)
            if previous is None:
                added.append(info)
            else:
                info.first_seen_ns = previous.first_seen_ns
        for identity, info in self.processes.items():
            if identity not in current:
                info.status = "exited"
                info.last_seen_ns = now_ns
                exited.append(info)
        self.processes = current
        return added, exited

    def snapshot(self) -> list[ProcessInfo]:
        return list(self.processes.values())

    def _read_pids(self) -> list[int]:
        try:
            pids = {int(line.strip()) for line in (self.cgroup_path / "cgroup.procs").read_text(encoding="utf-8").splitlines() if line.strip()}
            return sorted(pids)
        except (OSError, ValueError):
            return []

    def _read_process(self, pid: int, now_ns: int) -> ProcessInfo | None:
        proc = Path("/proc") / str(pid)
        try:
            stat_text = (proc / "stat").read_text(encoding="utf-8", errors="replace")
            starttime = parse_proc_stat_starttime(stat_text)
        except (OSError, ValueError, IndexError):
            return None
        comm = _read_text(proc / "comm").strip()
        cmdline = _read_bytes(proc / "cmdline").replace(b"\x00", b" ").decode("utf-8", errors="replace").strip()
        exe = ""
        try:
            exe = str((proc / "exe").resolve())
        except OSError:
            exe = ""
        role = self.role_resolver.resolve(comm, cmdline, exe)
        return ProcessInfo(
            pid=pid,
            process_starttime=starttime,
            comm=comm,
            cmdline=cmdline,
            exe=exe,
            process_role=role,
            first_seen_ns=now_ns,
            last_seen_ns=now_ns,
            status="running",
        )


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _read_bytes(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError:
        return b""


def parse_proc_stat_starttime(stat_text: str) -> int:
    end_comm = stat_text.rfind(")")
    if end_comm < 0:
        raise ValueError("invalid /proc stat: missing comm terminator")
    fields_after_comm = stat_text[end_comm + 2 :].split()
    # fields_after_comm[0] is field 3 (state), so field 22 starttime is index 19.
    return int(fields_after_comm[19])
