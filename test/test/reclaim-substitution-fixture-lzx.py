#!/usr/bin/env python3
"""Exact clean-cold, dirty-cold and foreground-hot page fixture. lzx-note

The three mappings use different files so mincore() can attribute eviction to
the page class that supplied it.  PREPARE materializes and cleans every page;
COLDIFY dirties only the dirty mapping and applies MADV_COLD to both measured
working sets.  TOUCH_CLEAN provides an exact post-pressure replay of the pages
whose preservation is the purpose of the experiment. lzx-note
"""

from __future__ import annotations

import argparse
import csv
import ctypes
import mmap
import os
import signal
import socket
import sys
import time
from pathlib import Path
from typing import TextIO


PAGE_SIZE = int(os.sysconf("SC_PAGE_SIZE"))
MADV_COLD = 20
STOP = False


def on_signal(_signum: int, _frame: object) -> None:
    global STOP
    STOP = True


def aligned(value: int) -> int:
    if value <= 0:
        return 0
    return max(PAGE_SIZE, (int(value) // PAGE_SIZE) * PAGE_SIZE)


class Mapping:
    def __init__(self, path: Path, length: int, salt: int) -> None:
        self.path = path
        self.length = aligned(length)
        self.salt = salt
        self.fd = -1
        self.mapping: mmap.mmap | None = None

    def open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.unlink(missing_ok=True)
        self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        os.ftruncate(self.fd, self.length)
        self.mapping = mmap.mmap(self.fd, self.length, access=mmap.ACCESS_WRITE)

    def materialize_clean(self) -> int:
        assert self.mapping is not None
        for offset in range(0, self.length, PAGE_SIZE):
            self.mapping[offset] = ((offset // PAGE_SIZE) * 131 + self.salt) & 0xFF
        self.mapping.flush()
        os.fsync(self.fd)
        return self.length

    def dirty_and_cold(self) -> int:
        assert self.mapping is not None
        for offset in range(0, self.length, PAGE_SIZE):
            self.mapping[offset] = (self.mapping[offset] + 1) & 0xFF
        self.mapping.madvise(MADV_COLD)
        return self.length

    def cold(self) -> None:
        assert self.mapping is not None
        self.mapping.madvise(MADV_COLD)

    def touch(self) -> tuple[int, int]:
        assert self.mapping is not None
        checksum = 1469598103934665603
        for offset in range(0, self.length, PAGE_SIZE):
            checksum ^= self.mapping[offset]
            checksum = (checksum * 1099511628211) & ((1 << 64) - 1)
        return self.length, checksum

    def resident_pages(self) -> int:
        assert self.mapping is not None
        count = self.length // PAGE_SIZE
        vector = (ctypes.c_ubyte * count)()
        address = ctypes.addressof(ctypes.c_char.from_buffer(self.mapping))
        libc = ctypes.CDLL(None, use_errno=True)
        result = libc.mincore(
            ctypes.c_void_p(address), ctypes.c_size_t(self.length), vector,
        )
        if result != 0:
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error))
        return sum(1 for value in vector if value & 1)

    def close(self) -> None:
        if self.mapping is not None:
            self.mapping.close()
        if self.fd >= 0:
            os.close(self.fd)
        self.path.unlink(missing_ok=True)


class Fixture:
    def __init__(self, args: argparse.Namespace) -> None:
        self.app = str(args.app)
        self.socket_path = Path(args.socket)
        self.log_path = Path(args.log)
        self.clean = Mapping(Path(args.clean_file), args.clean_bytes, 17)
        self.dirty = Mapping(Path(args.dirty_file), args.dirty_bytes, 53)
        self.hot = Mapping(Path(args.hot_file), args.hot_bytes, 97)
        self.server: socket.socket | None = None
        self.log_stream: TextIO | None = None
        self.writer: csv.DictWriter | None = None
        self.prepared = False
        self.coldified = False

    def start(self) -> None:
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.socket_path.unlink(missing_ok=True)
        for region in (self.clean, self.dirty, self.hot):
            region.open()
        self.log_stream = self.log_path.open("w", encoding="utf-8", newline="")
        self.writer = csv.DictWriter(
            self.log_stream,
            fieldnames=[
                "timestamp_ns", "app", "pid", "command", "status",
                "clean_bytes", "dirty_bytes", "hot_bytes", "touched_bytes",
                "latency_us", "checksum", "detail",
            ],
        )
        self.writer.writeheader()
        self.log("START", "OK", 0, 0, 0, "")
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(str(self.socket_path))
        os.chmod(self.socket_path, 0o600)
        self.server.listen(8)
        self.server.settimeout(0.25)

    def log(
        self, command: str, status: str, touched: int, latency_us: int,
        checksum: int, detail: str,
    ) -> None:
        if self.writer is None or self.log_stream is None:
            return
        self.writer.writerow({
            "timestamp_ns": time.time_ns(), "app": self.app, "pid": os.getpid(),
            "command": command, "status": status,
            "clean_bytes": self.clean.length, "dirty_bytes": self.dirty.length,
            "hot_bytes": self.hot.length, "touched_bytes": touched,
            "latency_us": latency_us, "checksum": f"{checksum:016x}",
            "detail": detail,
        })
        self.log_stream.flush()

    def status(self) -> str:
        return (
            f"OK app={self.app} pid={os.getpid()} prepared={int(self.prepared)} "
            f"coldified={int(self.coldified)} clean_bytes={self.clean.length} "
            f"dirty_bytes={self.dirty.length} hot_bytes={self.hot.length}\n"
        )

    def prepare(self) -> str:
        if self.prepared:
            return "ERR reason=ALREADY_PREPARED\n"
        started = time.monotonic_ns()
        try:
            touched = sum(
                region.materialize_clean() for region in (self.clean, self.dirty, self.hot)
            )
            self.prepared = True
            latency = (time.monotonic_ns() - started) // 1000
            self.log("PREPARE", "OK", touched, latency, 0, "all_regions_clean=1")
            return f"OK prepared=1 touched_bytes={touched} latency_us={latency}\n"
        except (BufferError, MemoryError, OSError, ValueError) as exc:
            latency = (time.monotonic_ns() - started) // 1000
            self.log("PREPARE", "ERROR", 0, latency, 0, type(exc).__name__)
            return f"ERR reason={type(exc).__name__}\n"

    def coldify(self) -> str:
        if not self.prepared:
            return "ERR reason=NOT_PREPARED\n"
        if self.coldified:
            return "ERR reason=ALREADY_COLDIFIED\n"
        started = time.monotonic_ns()
        try:
            self.clean.cold()
            dirty_bytes = self.dirty.dirty_and_cold()
            self.hot.cold()
            self.coldified = True
            latency = (time.monotonic_ns() - started) // 1000
            detail = (
                f"clean_pages={self.clean.length // PAGE_SIZE} "
                f"dirty_pages={self.dirty.length // PAGE_SIZE} "
                f"hot_pages={self.hot.length // PAGE_SIZE} madv_cold=1 flushed=0"
            )
            self.log("COLDIFY", "OK", dirty_bytes, latency, 0, detail)
            return f"OK dirty_bytes={dirty_bytes} {detail} latency_us={latency}\n"
        except (BufferError, MemoryError, OSError, ValueError) as exc:
            latency = (time.monotonic_ns() - started) // 1000
            self.log("COLDIFY", "ERROR", 0, latency, 0, type(exc).__name__)
            return f"ERR reason={type(exc).__name__}\n"

    def redirty(self) -> str:
        """Refresh only the measured dirty mapping before inference. lzx-note"""
        if not self.coldified:
            return "ERR reason=NOT_COLDIFIED\n"
        started = time.monotonic_ns()
        try:
            dirty_bytes = self.dirty.dirty_and_cold()
            latency = (time.monotonic_ns() - started) // 1000
            detail = (
                f"dirty_pages={self.dirty.length // PAGE_SIZE} "
                "madv_cold=1 flushed=0 repeatable=1"
            )
            self.log("REDIRTY", "OK", dirty_bytes, latency, 0, detail)
            return f"OK dirty_bytes={dirty_bytes} {detail} latency_us={latency}\n"
        except (BufferError, MemoryError, OSError, ValueError) as exc:
            latency = (time.monotonic_ns() - started) // 1000
            self.log("REDIRTY", "ERROR", 0, latency, 0, type(exc).__name__)
            return f"ERR reason={type(exc).__name__}\n"

    def residency(self, command: str) -> str:
        if not self.coldified:
            return "ERR reason=NOT_COLDIFIED\n"
        started = time.monotonic_ns()
        try:
            clean = self.clean.resident_pages()
            dirty = self.dirty.resident_pages()
            hot = self.hot.resident_pages()
            latency = (time.monotonic_ns() - started) // 1000
            detail = (
                f"clean_resident_pages={clean} clean_pages={self.clean.length // PAGE_SIZE} "
                f"dirty_resident_pages={dirty} dirty_pages={self.dirty.length // PAGE_SIZE} "
                f"hot_resident_pages={hot} hot_pages={self.hot.length // PAGE_SIZE}"
            )
            self.log(command, "OK", 0, latency, 0, detail)
            return f"OK {detail} latency_us={latency}\n"
        except (BufferError, MemoryError, OSError, ValueError) as exc:
            latency = (time.monotonic_ns() - started) // 1000
            self.log(command, "ERROR", 0, latency, 0, type(exc).__name__)
            return f"ERR reason={type(exc).__name__}\n"

    def touch_region(self, command: str, region: Mapping) -> str:
        if not self.prepared:
            return "ERR reason=NOT_PREPARED\n"
        started = time.monotonic_ns()
        try:
            touched, checksum = region.touch()
            latency = (time.monotonic_ns() - started) // 1000
            self.log(command, "OK", touched, latency, checksum, "exact_page_replay=1")
            return (
                f"OK touched_bytes={touched} latency_us={latency} "
                f"checksum={checksum:016x}\n"
            )
        except (BufferError, MemoryError, OSError, ValueError) as exc:
            latency = (time.monotonic_ns() - started) // 1000
            self.log(command, "ERROR", 0, latency, 0, type(exc).__name__)
            return f"ERR reason={type(exc).__name__}\n"

    def handle(self, line: str) -> str:
        global STOP
        command = line.strip().upper()
        if command == "STATUS":
            return self.status()
        if command == "PREPARE":
            return self.prepare()
        if command == "COLDIFY":
            return self.coldify()
        if command == "REDIRTY":
            return self.redirty()
        if command == "TOUCH_HOT":
            return self.touch_region(command, self.hot)
        if command == "TOUCH_CLEAN":
            return self.touch_region(command, self.clean)
        if command == "TOUCH_CLEAN_WARM":
            return self.touch_region(command, self.clean)
        if command in {"RESIDENCY_BEFORE", "RESIDENCY_AFTER"}:
            return self.residency(command)
        if command == "STOP":
            STOP = True
            self.log("STOP", "OK", 0, 0, 0, "")
            return "OK stopped=1\n"
        return "ERR reason=UNKNOWN_COMMAND\n"

    def serve(self) -> None:
        assert self.server is not None
        while not STOP:
            try:
                client, _ = self.server.accept()
            except socket.timeout:
                continue
            with client:
                client.settimeout(5.0)
                try:
                    line = client.recv(4096).decode("ascii", errors="replace")
                    client.sendall(self.handle(line).encode("ascii", errors="replace"))
                except OSError:
                    continue

    def close(self) -> None:
        if self.server is not None:
            self.server.close()
        for region in (self.clean, self.dirty, self.hot):
            region.close()
        self.socket_path.unlink(missing_ok=True)
        if self.log_stream is not None:
            self.log_stream.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--app", required=True)
    parser.add_argument("--socket", required=True)
    parser.add_argument("--log", required=True)
    parser.add_argument("--clean-file", required=True)
    parser.add_argument("--dirty-file", required=True)
    parser.add_argument("--hot-file", required=True)
    parser.add_argument("--clean-bytes", type=int, required=True)
    parser.add_argument("--dirty-bytes", type=int, required=True)
    parser.add_argument("--hot-bytes", type=int, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    fixture = Fixture(args)
    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    try:
        fixture.start()
        fixture.serve()
        return 0
    except (OSError, ValueError) as exc:
        print(f"reclaim substitution fixture error: {exc}", file=sys.stderr)
        return 1
    finally:
        fixture.close()


if __name__ == "__main__":
    raise SystemExit(main())
