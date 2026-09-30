#!/usr/bin/env python3
"""Controlled page-type and resident-memory workloads for phase 7 validation."""

from __future__ import annotations

import argparse
import json
import mmap
import os
import signal
import tempfile
import threading
import time
from pathlib import Path


STOP = threading.Event()
START = threading.Event()


def on_stop(_signo, _frame):
    STOP.set()


def on_start(_signo, _frame):
    START.set()


def publish_ready(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                         encoding="utf-8")
    temporary.replace(path)
    print(json.dumps({"event": "ready", **payload}, ensure_ascii=False), flush=True)


def touch_mapping(mapping: mmap.mmap, size: int, page_size: int,
                  write: bool) -> int:
    checksum = 0
    if write:
        for offset in range(0, size, page_size):
            mapping[offset] = (mapping[offset] + 1) & 0xFF
    else:
        for offset in range(0, size, page_size):
            checksum ^= mapping[offset]
    return checksum


def run_microbench(args) -> None:
    size = args.size_mib * 1024 * 1024
    page_size = os.sysconf("SC_PAGE_SIZE")
    backing_path = None
    fd = None

    if args.mode == "anon":
        mapping = mmap.mmap(-1, size, flags=mmap.MAP_PRIVATE | mmap.MAP_ANONYMOUS,
                            prot=mmap.PROT_READ | mmap.PROT_WRITE)
        write = True
    elif args.mode in {"file", "cow"}:
        backing_dir = Path(args.backing_dir)
        backing_dir.mkdir(parents=True, exist_ok=True)
        fd, raw_path = tempfile.mkstemp(prefix=f"phase7-{args.mode}-",
                                        suffix=".bin", dir=backing_dir)
        backing_path = Path(raw_path)
        os.posix_fallocate(fd, 0, size)
        if hasattr(os, "posix_fadvise"):
            os.posix_fadvise(fd, 0, size, os.POSIX_FADV_DONTNEED)
        access = mmap.ACCESS_READ if args.mode == "file" else mmap.ACCESS_COPY
        mapping = mmap.mmap(fd, size, access=access)
        write = args.mode == "cow"
    elif args.mode == "shmem":
        if not hasattr(os, "memfd_create"):
            raise RuntimeError("os.memfd_create is unavailable")
        fd = os.memfd_create("phase7-shmem", flags=0)
        os.ftruncate(fd, size)
        mapping = mmap.mmap(fd, size, flags=mmap.MAP_SHARED,
                            prot=mmap.PROT_READ | mmap.PROT_WRITE)
        write = True
    else:
        raise ValueError(args.mode)

    publish_ready(Path(args.ready_file), {
        "pid": os.getpid(), "mode": args.mode, "size_bytes": size,
        "page_size_bytes": page_size,
        "backing_path": str(backing_path) if backing_path else "",
        "waits_for_sigusr1": True,
    })
    if not START.wait(timeout=args.start_timeout):
        raise TimeoutError("SIGUSR1 was not received before timeout")

    started = time.monotonic()
    sweeps = 0
    checksum = 0
    while not STOP.is_set() and time.monotonic() - started < args.active_seconds:
        checksum ^= touch_mapping(mapping, size, page_size, write)
        sweeps += 1
        STOP.wait(args.sweep_interval)
    print(json.dumps({"event": "active_complete", "mode": args.mode,
                      "sweeps": sweeps, "checksum": checksum}), flush=True)
    while not STOP.wait(1.0):
        pass
    mapping.close()
    if fd is not None:
        os.close(fd)
    if backing_path:
        backing_path.unlink(missing_ok=True)


def run_pressure(args) -> None:
    page_size = os.sysconf("SC_PAGE_SIZE")
    size = int(args.gib * 1024 ** 3)
    mapping = mmap.mmap(-1, size, flags=mmap.MAP_PRIVATE | mmap.MAP_ANONYMOUS,
                        prot=mmap.PROT_READ | mmap.PROT_WRITE)
    started = time.monotonic()
    for offset in range(0, size, page_size):
        mapping[offset] = 1
        if STOP.is_set():
            mapping.close()
            return
    touch_seconds = time.monotonic() - started
    publish_ready(Path(args.ready_file), {
        "pid": os.getpid(), "mode": "pressure", "target_gib": args.gib,
        "target_bytes": size, "touched_bytes": size,
        "page_size_bytes": page_size, "touch_seconds": round(touch_seconds, 6),
    })
    checksum = 0
    while not STOP.wait(args.heartbeat_seconds):
        # Touch one page per heartbeat only. The allocation remains resident unless
        # the kernel reclaims it; the monitor, not this declaration, is authoritative.
        offset = ((int(time.monotonic()) * page_size) % size)
        checksum ^= mapping[offset]
    print(json.dumps({"event": "pressure_stopping", "checksum": checksum}),
          flush=True)
    mapping.close()


def parse_args():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)

    micro = sub.add_parser("microbench")
    micro.add_argument("--mode", choices=["anon", "file", "cow", "shmem"],
                       required=True)
    micro.add_argument("--size-mib", type=int, default=64)
    micro.add_argument("--ready-file", required=True)
    micro.add_argument("--backing-dir", default=str(Path.cwd() / "workload-files"))
    micro.add_argument("--active-seconds", type=float, default=7.0)
    micro.add_argument("--sweep-interval", type=float, default=0.25)
    micro.add_argument("--start-timeout", type=float, default=60.0)

    pressure = sub.add_parser("pressure")
    pressure.add_argument("--gib", type=int, choices=[2, 4, 8], required=True)
    pressure.add_argument("--ready-file", required=True)
    pressure.add_argument("--heartbeat-seconds", type=float, default=1.0)
    return parser.parse_args()


def main() -> None:
    signal.signal(signal.SIGTERM, on_stop)
    signal.signal(signal.SIGINT, on_stop)
    signal.signal(signal.SIGUSR1, on_start)
    args = parse_args()
    if args.command == "microbench":
        run_microbench(args)
    else:
        run_pressure(args)


if __name__ == "__main__":
    main()
