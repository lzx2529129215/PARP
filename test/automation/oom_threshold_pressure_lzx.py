#!/usr/bin/env python3
"""Deterministic bounded anonymous pressure for the OOM-THRESHOLD scenario."""

from __future__ import annotations

import argparse
import json
import mmap
import os
import signal
import time
from pathlib import Path


MIB = 1024 * 1024
PAGE_SIZE = os.sysconf("SC_PAGE_SIZE")
STOP = False


def request_stop(_signum: int, _frame: object) -> None:
    global STOP
    STOP = True


def current_cgroup() -> Path:
    for line in Path("/proc/self/cgroup").read_text(encoding="utf-8").splitlines():
        fields = line.split(":", 2)
        if len(fields) == 3 and fields[0] == "0":
            return Path("/sys/fs/cgroup") / fields[2].lstrip("/")
    raise RuntimeError("unified cgroup v2 path unavailable")


def read_limit(path: Path) -> int | None:
    value = path.read_text(encoding="ascii").strip()
    return None if value == "max" else int(value)


def effective_limit(cgroup: Path, filename: str) -> tuple[int | None, str | None]:
    """Return the tightest finite inherited cgroup-v2 limit and its owner."""
    root = Path("/sys/fs/cgroup")
    finite: list[tuple[int, Path]] = []
    cursor = cgroup
    while True:
        candidate = cursor / filename
        if candidate.exists():
            value = read_limit(candidate)
            if value is not None:
                finite.append((value, cursor))
        if cursor == root:
            break
        if root not in cursor.parents:
            break
        cursor = cursor.parent
    if not finite:
        return None, None
    value, owner = min(finite, key=lambda item: item[0])
    return value, str(owner)


def write_state(path: Path, payload: dict[str, object]) -> None:
    # lzx-note: atomic replacement prevents the out-of-cgroup collector from
    # observing a partially written JSON object if this process is OOM-killed.
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target-mib", type=int, required=True)
    parser.add_argument("--chunk-mib", type=int, default=64)
    parser.add_argument("--ramp-interval", type=float, default=0.25)
    parser.add_argument("--hold-seconds", type=float, default=600.0)
    parser.add_argument("--oom-score-adj", type=int, default=1000)
    parser.add_argument("--seed", type=int, default=20260821)
    parser.add_argument("--state", type=Path, required=True)
    args = parser.parse_args()
    if args.target_mib <= 0 or args.chunk_mib <= 0:
        raise SystemExit("target-mib and chunk-mib must be positive")

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    score = min(1000, max(-1000, args.oom_score_adj))
    Path("/proc/self/oom_score_adj").write_text(f"{score}\n", encoding="ascii")

    cgroup = current_cgroup()
    memory_max, memory_max_owner = effective_limit(cgroup, "memory.max")  # lzx-note
    swap_max, swap_max_owner = effective_limit(cgroup, "memory.swap.max")  # lzx-note
    target_bytes = args.target_mib * MIB
    chunk_bytes = args.chunk_mib * MIB
    started_ns = time.time_ns()
    common: dict[str, object] = {
        "pid": os.getpid(),
        "seed": args.seed,
        "cgroup": str(cgroup),
        "memory_max": memory_max,
        "memory_max_owner": memory_max_owner,
        "memory_swap_max": swap_max,
        "memory_swap_max_owner": swap_max_owner,
        "target_bytes": target_bytes,
        "chunk_bytes": chunk_bytes,
        "started_ns": started_ns,
    }
    write_state(args.state, {**common, "status": "RAMPING", "allocated_bytes": 0})

    chunks: list[mmap.mmap] = []
    allocated = 0
    chunk_index = 0
    try:
        while allocated < target_bytes and not STOP:
            amount = min(chunk_bytes, target_bytes - allocated)
            # Private anonymous RSS participates directly in OOM badness;
            # together with oom_score_adj=1000 this makes this disposable
            # worker the deterministic victim instead of a GUI/fixture. # lzx-note
            region = mmap.mmap(
                -1,
                amount,
                flags=mmap.MAP_PRIVATE | mmap.MAP_ANONYMOUS,
                prot=mmap.PROT_READ | mmap.PROT_WRITE,
            )
            # Touch every page. The seed changes contents, not allocation order,
            # so OFF and APPLY have byte-identical deterministic pressure. # lzx-note
            for offset in range(0, amount, PAGE_SIZE):
                region[offset] = (args.seed + chunk_index + offset // PAGE_SIZE) & 0xFF
            chunks.append(region)
            allocated += amount
            chunk_index += 1
            write_state(args.state, {**common, "status": "RAMPING", "allocated_bytes": allocated})
            if args.ramp_interval > 0 and allocated < target_bytes:
                time.sleep(args.ramp_interval)

        if STOP:
            write_state(args.state, {**common, "status": "STOPPED", "allocated_bytes": allocated, "finished_ns": time.time_ns()})
            return 0

        write_state(args.state, {**common, "status": "HOLDING", "allocated_bytes": allocated, "holding_ns": time.time_ns()})
        deadline = time.monotonic() + max(0.0, args.hold_seconds)
        while not STOP and time.monotonic() < deadline:
            time.sleep(min(0.5, max(0.0, deadline - time.monotonic())))
        write_state(args.state, {**common, "status": "COMPLETE" if not STOP else "STOPPED", "allocated_bytes": allocated, "finished_ns": time.time_ns()})
        return 0
    except MemoryError:
        write_state(args.state, {**common, "status": "MEMORY_ERROR", "allocated_bytes": allocated, "finished_ns": time.time_ns()})
        return 3
    finally:
        for region in chunks:
            region.close()


if __name__ == "__main__":
    raise SystemExit(main())
