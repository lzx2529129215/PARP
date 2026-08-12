#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0
"""Bounded anonymous and mmap file hot/cold workload for Phase 2.6."""

import argparse
import json
import mmap
import os
from pathlib import Path
import signal
import tempfile
import threading
import time

MIB = 1024 * 1024
PAGE_SIZE = mmap.PAGESIZE
ABSOLUTE_MAX_BYTES = 512 * MIB


def memtotal_bytes():
    with open("/proc/meminfo", encoding="ascii") as stream:
        for line in stream:
            if line.startswith("MemTotal:"):
                return int(line.split()[1]) * 1024
    raise RuntimeError("MemTotal is unavailable")


def emit(phase, **fields):
    record = {
        "source": "RUNTIME_LEVEL3A",
        "phase": phase,
        "pid": os.getpid(),
        "monotonic_ns": time.monotonic_ns(),
        "wall_time_ns": time.time_ns(),
    }
    record.update(fields)
    print(json.dumps(record, sort_keys=True), flush=True)


def touch_pages(buffer, start, end, stride=PAGE_SIZE):
    for offset in range(start, end, stride):
        buffer[offset] = (buffer[offset] + 1) & 0xFF


def run(args):
    safe_limit = min(ABSOLUTE_MAX_BYTES, memtotal_bytes() // 10)
    requested = args.total_mib * MIB
    if requested <= 0 or requested > safe_limit:
        raise ValueError("requested memory exceeds min(512MiB, MemTotal*10%)")
    anon_bytes = requested // 2
    file_bytes = requested - anon_bytes
    hot_anon_end = max(PAGE_SIZE, anon_bytes // 4)
    hot_file_end = max(PAGE_SIZE, file_bytes // 4)
    stop = threading.Event()

    def request_stop(signum, frame):
        del frame
        emit("signal", signal=signum)
        stop.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    anonymous = bytearray(anon_bytes)
    work_dir = Path(args.directory) if args.directory else None
    descriptor, filename = tempfile.mkstemp(prefix="parp-phase26-", suffix=".bin",
                                             dir=str(work_dir) if work_dir else None)
    mapped = None
    try:
        os.ftruncate(descriptor, file_bytes)
        mapped = mmap.mmap(descriptor, file_bytes, access=mmap.ACCESS_WRITE)
        touch_pages(anonymous, 0, anon_bytes)
        touch_pages(mapped, 0, file_bytes)
        emit("ready", total_bytes=requested, anon_bytes=anon_bytes,
             file_bytes=file_bytes, file_path=filename,
             hot_anon_bytes=hot_anon_end, hot_file_bytes=hot_file_end)
        deadline = time.monotonic() + args.duration
        cold_anon = hot_anon_end
        cold_file = hot_file_end
        iteration = 0
        while not stop.is_set() and time.monotonic() < deadline:
            touch_pages(anonymous, 0, hot_anon_end)
            touch_pages(mapped, 0, hot_file_end)
            if cold_anon < anon_bytes:
                anonymous[cold_anon] = (anonymous[cold_anon] + 1) & 0xFF
                cold_anon += PAGE_SIZE
            if cold_file < file_bytes:
                mapped[cold_file] = (mapped[cold_file] + 1) & 0xFF
                cold_file += PAGE_SIZE
            iteration += 1
            emit("access", iteration=iteration, cold_anon_offset=cold_anon,
                 cold_file_offset=cold_file)
            stop.wait(args.interval)
        emit("stopping", iterations=iteration)
    finally:
        if mapped is not None:
            mapped.flush()
            mapped.close()
        os.close(descriptor)
        try:
            os.unlink(filename)
        except FileNotFoundError:
            pass
        emit("stopped")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--total-mib", type=int, default=256)
    parser.add_argument("--duration", type=int, default=600)
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--directory")
    args = parser.parse_args()
    if args.duration <= 0 or args.interval <= 0:
        parser.error("duration and interval must be positive")
    run(args)


if __name__ == "__main__":
    main()
