"""Best-effort file event collection without eBPF."""

from __future__ import annotations

import hashlib
import os
import time
from dataclasses import dataclass
from pathlib import Path

from collectors.process import ProcessSample


TRACKED_EXTS = {
    "doc",
    "docx",
    "xls",
    "xlsx",
    "ppt",
    "pptx",
    "pdf",
    "tmp",
    "so",
    "dll",
    "ttf",
    "otf",
    "conf",
    "json",
    "xml",
    "png",
    "jpg",
    "jpeg",
}


def file_ext(path: str) -> str:
    suffix = Path(path).suffix.lower().lstrip(".")
    return suffix if suffix in TRACKED_EXTS else suffix


def path_for_mode(path: str, mode: str) -> str:
    if not path:
        return ""
    if mode == "raw":
        return path
    if mode == "basename":
        return Path(path).name
    if mode == "hash":
        return hashlib.sha256(path.encode("utf-8", errors="replace")).hexdigest()
    return path


def _stat_path(path: str) -> tuple[int, int]:
    try:
        st = os.stat(path)
        return int(st.st_ino), int(st.st_size)
    except OSError:
        return 0, 0


@dataclass(frozen=True)
class SeenFile:
    pid: int
    event: str
    path: str
    inode: int


class FileEventCollector:
    """Poll /proc fd and maps to approximate openat and mmap events.

    This is the no-eBPF fallback. It cannot reliably attribute read/write/fsync
    or rename to paths; those remain feature-level procfs/cgroup deltas.
    """

    def __init__(self, path_mode: str = "hash") -> None:
        self.path_mode = path_mode
        self.seen: set[SeenFile] = set()

    def poll(self, samples: list[ProcessSample]) -> list[dict[str, object]]:
        rows: list[dict[str, object]] = []
        ts_ns = time.time_ns()
        for sample in samples:
            rows.extend(self._poll_fd(sample, ts_ns))
            rows.extend(self._poll_maps(sample, ts_ns))
        return rows

    def _base_row(self, sample: ProcessSample, ts_ns: int, event: str, path: str, inode: int, size: int) -> dict[str, object]:
        return {
            "ts_ns": ts_ns,
            "pid": sample.identity.pid,
            "tgid": sample.identity.tgid,
            "app": sample.app_id,
            "comm": sample.identity.comm,
            "event": event,
            "path": path_for_mode(path, self.path_mode),
            "ext": file_ext(path),
            "inode": inode,
            "offset": 0,
            "size": size,
        }

    def _poll_fd(self, sample: ProcessSample, ts_ns: int) -> list[dict[str, object]]:
        rows: list[dict[str, object]] = []
        fd_dir = Path("/proc") / str(sample.identity.pid) / "fd"
        try:
            fds = list(fd_dir.iterdir())
        except OSError:
            return rows
        for fd in fds:
            try:
                path = os.readlink(fd)
            except OSError:
                continue
            if not path.startswith("/") or " (deleted)" in path:
                continue
            inode, size = _stat_path(path)
            key = SeenFile(sample.identity.pid, "openat", path, inode)
            if key in self.seen:
                continue
            self.seen.add(key)
            rows.append(self._base_row(sample, ts_ns, "openat", path, inode, size))
        return rows

    def _poll_maps(self, sample: ProcessSample, ts_ns: int) -> list[dict[str, object]]:
        rows: list[dict[str, object]] = []
        maps_path = Path("/proc") / str(sample.identity.pid) / "maps"
        try:
            lines = maps_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return rows
        for line in lines:
            parts = line.split(maxsplit=5)
            if len(parts) < 6:
                continue
            path = parts[5]
            if not path.startswith("/") or " (deleted)" in path:
                continue
            inode, size = _stat_path(path)
            key = SeenFile(sample.identity.pid, "mmap", path, inode)
            if key in self.seen:
                continue
            self.seen.add(key)
            rows.append(self._base_row(sample, ts_ns, "mmap", path, inode, size))
        return rows

