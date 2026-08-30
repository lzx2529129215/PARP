#!/usr/bin/env python3
"""Controllable file/anonymous memory fixture for PARP acceptance tests.

The file mapping is deliberately sparse: it provides a large, reclaimable
file-backed address space without permanently consuming the same amount of
disk.  PREPARE faults the complete mapping once, while later commands touch
bounded hot or sampled regions.  The process never calls drop_caches,
memory.reclaim, mlock, swapoff, or any kernel policy interface.  The optional
materialized mode backs every file page with real storage and provides exact
full-file/full-anonymous replay commands for fault/refault validation. lzx-note
"""

from __future__ import annotations

import argparse
import csv
import ctypes
import hashlib
import mmap
import os
import signal
import socket
import sys
import time
from pathlib import Path


PAGE_SIZE = os.sysconf("SC_PAGE_SIZE")
CHUNK_BYTES = 32 * 1024 * 1024
MADV_COLD = 20  # Linux uapi value; Python 3.10 does not expose mmap.MADV_COLD. lzx-note
STOP = False


def on_signal(_signum: int, _frame: object) -> None:
    global STOP
    STOP = True


def aligned(value: int) -> int:
    return max(PAGE_SIZE, (int(value) // PAGE_SIZE) * PAGE_SIZE)


class Fixture:
    def __init__(self, args: argparse.Namespace) -> None:
        self.app = args.app
        self.socket_path = Path(args.socket)
        self.file_path = Path(args.file)
        self.log_path = Path(args.log)
        self.file_bytes = aligned(args.file_bytes)
        self.anon_bytes = aligned(args.anon_bytes) if args.anon_bytes else 0
        self.hot_bytes = min(aligned(args.hot_bytes), self.file_bytes)
        self.materialize_file = bool(args.materialize_file)  # lzx-note
        self.fd = -1
        self.file_map: mmap.mmap | None = None
        self.anon_map: mmap.mmap | None = None
        self.server: socket.socket | None = None
        self.prepared = False
        self.hot_touches = 0
        self.sample_touches = 0
        self.bytes_read = 0
        self.checksum = ""
        self.log_stream: object | None = None
        self.writer: csv.DictWriter | None = None

    @property
    def logical_bytes(self) -> int:
        return self.file_bytes + self.anon_bytes

    def start(self) -> None:
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.socket_path.unlink(missing_ok=True)
        self.file_path.unlink(missing_ok=True)
        self.fd = os.open(self.file_path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        os.ftruncate(self.fd, self.file_bytes)
        access = mmap.ACCESS_WRITE if self.materialize_file else mmap.ACCESS_READ
        self.file_map = mmap.mmap(self.fd, self.file_bytes, access=access)
        if self.anon_bytes:
            # mmap(-1, ..., access=ACCESS_WRITE) selects a shared anonymous
            # mapping on Linux and cgroup-v2 accounts it as shmem/file.  Use a
            # private anonymous VMA so workingset_refault_anon and swap-in/out
            # measure the intended anonymous-page lifecycle. lzx-note
            self.anon_map = mmap.mmap(
                -1,
                self.anon_bytes,
                flags=mmap.MAP_PRIVATE | mmap.MAP_ANONYMOUS,
                prot=mmap.PROT_READ | mmap.PROT_WRITE,
            )
        self.log_stream = self.log_path.open("w", encoding="utf-8", newline="")
        self.writer = csv.DictWriter(
            self.log_stream,
            fieldnames=[
                "timestamp_ns", "app", "pid", "command", "status",
                "logical_bytes", "file_bytes", "anon_bytes", "hot_bytes",
                "touched_bytes", "latency_us", "checksum", "detail",
            ],
        )
        self.writer.writeheader()
        self.log("START", "OK", 0, 0, "")
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(str(self.socket_path))
        os.chmod(self.socket_path, 0o600)
        self.server.listen(8)
        self.server.settimeout(0.25)

    def log(self, command: str, status: str, touched: int, latency_us: int, detail: str) -> None:
        if self.writer is None or self.log_stream is None:
            return
        self.writer.writerow({
            "timestamp_ns": time.time_ns(), "app": self.app, "pid": os.getpid(),
            "command": command, "status": status, "logical_bytes": self.logical_bytes,
            "file_bytes": self.file_bytes, "anon_bytes": self.anon_bytes,
            "hot_bytes": self.hot_bytes, "touched_bytes": touched,
            "latency_us": latency_us, "checksum": self.checksum, "detail": detail,
        })
        self.log_stream.flush()

    @staticmethod
    def digest_update(digest: hashlib._Hash, data: bytes) -> None:  # type: ignore[name-defined]
        digest.update(data[:64])
        digest.update(data[-64:] if len(data) >= 64 else data)

    def touch_file(self, start: int, length: int) -> tuple[int, str]:
        assert self.file_map is not None
        begin = max(0, min((int(start) // PAGE_SIZE) * PAGE_SIZE,
                           self.file_bytes - PAGE_SIZE))
        amount = max(PAGE_SIZE, min(aligned(length), self.file_bytes - begin))
        end = begin + amount
        digest = hashlib.blake2b(digest_size=8)
        offset = begin
        while offset < end:
            chunk_end = min(end, offset + CHUNK_BYTES)
            data = self.file_map[offset:chunk_end]
            self.digest_update(digest, data)
            offset = chunk_end
        self.bytes_read += amount
        return amount, digest.hexdigest()

    def touch_anon(self) -> int:
        if self.anon_map is None:
            return 0
        for offset in range(0, self.anon_bytes, PAGE_SIZE):
            self.anon_map[offset] = (offset // PAGE_SIZE) & 0xFF
        return self.anon_bytes

    def materialize(self) -> int:
        """Allocate one real disk-backed byte in every mapped file page. lzx-note"""
        if not self.materialize_file:
            return 0
        assert self.file_map is not None
        for offset in range(0, self.file_bytes, PAGE_SIZE):
            self.file_map[offset] = ((offset // PAGE_SIZE) * 131 + 17) & 0xFF
        self.file_map.flush()
        os.fsync(self.fd)
        return self.file_bytes

    @staticmethod
    def resident_pages(mapping: mmap.mmap, length: int) -> int:
        """Return the number of resident pages without faulting them in. lzx-note"""
        page_count = (length + PAGE_SIZE - 1) // PAGE_SIZE
        vector = (ctypes.c_ubyte * page_count)()
        address = ctypes.addressof(ctypes.c_char.from_buffer(mapping))
        libc = ctypes.CDLL(None, use_errno=True)
        result = libc.mincore(
            ctypes.c_void_p(address), ctypes.c_size_t(length), vector,
        )
        if result != 0:
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error))
        return sum(1 for value in vector if value & 1)

    def cold_dirty_file(self) -> str:
        """Dirty every file page, then mark it cold without writeback. lzx-note"""
        if not self.prepared:
            return "ERR reason=NOT_PREPARED\n"
        if not self.materialize_file:
            return "ERR reason=FILE_NOT_MATERIALIZED\n"
        assert self.file_map is not None
        started = time.monotonic_ns()
        try:
            for offset in range(0, self.file_bytes, PAGE_SIZE):
                self.file_map[offset] = (self.file_map[offset] + 1) & 0xFF
            # MADV_COLD deactivates the pages but deliberately leaves the
            # mmap dirtiness intact; do not flush or fsync after this point.
            # lzx-note
            self.file_map.madvise(MADV_COLD)
            latency_us = (time.monotonic_ns() - started) // 1000
            pages = self.file_bytes // PAGE_SIZE
            detail = f"dirty_pages={pages} madv_cold=1 flushed=0"
            self.log("COLD_DIRTY_FILE", "OK", self.file_bytes, latency_us, detail)
            return (
                f"OK dirty_bytes={self.file_bytes} dirty_pages={pages} "
                f"madv_cold=1 latency_us={latency_us}\n"
            )
        except (OSError, BufferError, MemoryError, ValueError) as exc:
            latency_us = (time.monotonic_ns() - started) // 1000
            self.log("COLD_DIRTY_FILE", "ERROR", 0, latency_us, type(exc).__name__)
            return f"ERR reason={type(exc).__name__}\n"

    def residency(self, command: str) -> str:
        """Record exact file/anonymous residency via mincore(). lzx-note"""
        if not self.prepared:
            return "ERR reason=NOT_PREPARED\n"
        assert self.file_map is not None
        started = time.monotonic_ns()
        try:
            file_resident = self.resident_pages(self.file_map, self.file_bytes)
            anon_resident = (
                self.resident_pages(self.anon_map, self.anon_bytes)
                if self.anon_map is not None else 0
            )
            file_pages = self.file_bytes // PAGE_SIZE
            anon_pages = self.anon_bytes // PAGE_SIZE if self.anon_bytes else 0
            latency_us = (time.monotonic_ns() - started) // 1000
            detail = (
                f"resident_file_pages={file_resident} file_pages={file_pages} "
                f"resident_anon_pages={anon_resident} anon_pages={anon_pages}"
            )
            self.log(command, "OK", 0, latency_us, detail)
            return f"OK {detail} latency_us={latency_us}\n"
        except (OSError, BufferError, MemoryError, ValueError) as exc:
            latency_us = (time.monotonic_ns() - started) // 1000
            self.log(command, "ERROR", 0, latency_us, type(exc).__name__)
            return f"ERR reason={type(exc).__name__}\n"

    def touch_file_pages(self, start: int, length: int) -> tuple[int, str]:
        """Touch exactly one byte per page without allocating large read buffers. lzx-note"""
        assert self.file_map is not None
        begin = max(0, min((int(start) // PAGE_SIZE) * PAGE_SIZE,
                           self.file_bytes - PAGE_SIZE))
        amount = max(PAGE_SIZE, min(aligned(length), self.file_bytes - begin))
        checksum = 1469598103934665603
        for offset in range(begin, begin + amount, PAGE_SIZE):
            checksum ^= self.file_map[offset]
            checksum = (checksum * 1099511628211) & ((1 << 64) - 1)
        self.bytes_read += amount
        return amount, f"{checksum:016x}"

    def prepare(self) -> str:
        if self.prepared:
            return "ERR reason=ALREADY_PREPARED\n"
        started = time.monotonic_ns()
        try:
            anon_touched = self.touch_anon()
            self.materialize()
            file_touched, digest = self.touch_file(0, self.file_bytes)
            self.checksum = digest
            self.prepared = True
            touched = anon_touched + file_touched
            self.log("PREPARE", "OK", touched, (time.monotonic_ns() - started) // 1000, "")
            return f"OK prepared=1 touched_bytes={touched} checksum={digest}\n"
        except (OSError, BufferError, MemoryError) as exc:
            self.log("PREPARE", "ERROR", 0, (time.monotonic_ns() - started) // 1000, type(exc).__name__)
            return f"ERR reason={type(exc).__name__}\n"

    def replay(self, command: str, touch_file: bool, touch_anon: bool) -> str:
        """Replay the same pages and report their end-to-end fault latency. lzx-note"""
        if not self.prepared:
            return "ERR reason=NOT_PREPARED\n"
        started = time.monotonic_ns()
        try:
            touched = 0
            digest = self.checksum
            if touch_file:
                file_touched, digest = self.touch_file_pages(0, self.file_bytes)
                touched += file_touched
            if touch_anon:
                touched += self.touch_anon()
            self.checksum = digest
            latency_us = (time.monotonic_ns() - started) // 1000
            self.log(command, "OK", touched, latency_us, "exact_page_replay=1")
            return (
                f"OK touched_bytes={touched} latency_us={latency_us} "
                f"checksum={digest}\n"
            )
        except (OSError, BufferError, MemoryError) as exc:
            latency_us = (time.monotonic_ns() - started) // 1000
            self.log(command, "ERROR", 0, latency_us, type(exc).__name__)
            return f"ERR reason={type(exc).__name__}\n"

    def touch(self, command: str, start: int, length: int) -> str:
        if not self.prepared:
            return "ERR reason=NOT_PREPARED\n"
        started = time.monotonic_ns()
        try:
            touched, digest = self.touch_file(start, length)
            if command == "TOUCH_HOT":
                self.hot_touches += 1
                touched += self.touch_anon()
            else:
                self.sample_touches += 1
            self.checksum = digest
            latency_us = (time.monotonic_ns() - started) // 1000
            self.log(command, "OK", touched, latency_us, f"offset={start}")
            return f"OK touched_bytes={touched} latency_us={latency_us} checksum={digest}\n"
        except (OSError, BufferError, MemoryError) as exc:
            self.log(command, "ERROR", 0, (time.monotonic_ns() - started) // 1000, type(exc).__name__)
            return f"ERR reason={type(exc).__name__}\n"

    def status(self) -> str:
        return (
            f"OK app={self.app} pid={os.getpid()} prepared={int(self.prepared)} "
            f"logical_bytes={self.logical_bytes} file_bytes={self.file_bytes} "
            f"anon_bytes={self.anon_bytes} hot_bytes={self.hot_bytes} "
            f"hot_touches={self.hot_touches} sample_touches={self.sample_touches} "
            f"bytes_read={self.bytes_read}\n"
        )

    def handle(self, line: str) -> str:
        global STOP
        parts = line.strip().split()
        if not parts:
            return "ERR reason=EMPTY_COMMAND\n"
        command = parts[0].upper()
        if command == "STATUS":
            return self.status()
        if command == "PREPARE":
            return self.prepare()
        if command == "TOUCH_HOT":
            return self.touch(command, 0, self.hot_bytes)
        if command == "TOUCH_FILE":
            return self.replay(command, True, False)
        if command == "TOUCH_ANON":
            return self.replay(command, False, True)
        if command == "TOUCH_BOTH":
            return self.replay(command, True, True)
        if command == "COLD_DIRTY_FILE":
            return self.cold_dirty_file()
        if command in {"RESIDENCY_BEFORE", "RESIDENCY_AFTER"}:
            return self.residency(command)
        if command == "TOUCH_SAMPLE" and len(parts) == 3:
            try:
                return self.touch(command, int(parts[1]), int(parts[2]))
            except ValueError:
                return "ERR reason=INVALID_INTEGER\n"
        if command == "STOP":
            STOP = True
            self.log("STOP", "OK", 0, 0, "")
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
        if self.file_map is not None:
            self.file_map.close()
        if self.anon_map is not None:
            self.anon_map.close()
        if self.fd >= 0:
            os.close(self.fd)
        self.socket_path.unlink(missing_ok=True)
        self.file_path.unlink(missing_ok=True)
        if self.log_stream is not None:
            self.log_stream.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--app", required=True)
    parser.add_argument("--socket", required=True)
    parser.add_argument("--file", required=True)
    parser.add_argument("--log", required=True)
    parser.add_argument("--file-bytes", type=int, required=True)
    parser.add_argument("--anon-bytes", type=int, default=0)
    parser.add_argument("--hot-bytes", type=int, required=True)
    parser.add_argument("--materialize-file", action="store_true")  # lzx-note
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
        print(f"memory fixture error: {exc}", file=sys.stderr)
        return 1
    finally:
        fixture.close()


if __name__ == "__main__":
    raise SystemExit(main())
