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


def read_current(path: Path) -> int:
    """Read one cgroup-v2 byte counter with an explicit error boundary."""
    # lzx-note: Predictive-reclaim tests stop on remaining headroom, not on a
    # guessed allocation size that changes when GUI RSS changes between boots.
    return int((path / "memory.current").read_text(encoding="ascii").strip())


def read_memavailable() -> int:
    """Return host MemAvailable in bytes for bounded global reclaim. lzx-note"""
    for line in Path("/proc/meminfo").read_text(encoding="ascii").splitlines():
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) * 1024
    raise RuntimeError("MemAvailable is unavailable")


def read_memfree() -> int:
    """Return host MemFree in bytes for global reclaim entry. lzx-note"""
    for line in Path("/proc/meminfo").read_text(encoding="ascii").splitlines():
        if line.startswith("MemFree:"):
            return int(line.split()[1]) * 1024
    raise RuntimeError("MemFree is unavailable")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--target-mib", type=int)
    target.add_argument(
        "--target-headroom-mib", type=int,
        help="stop ramping when the limiting cgroup has at most this much headroom",
    )
    target.add_argument(
        "--target-memavailable-mib", type=int,
        help="stop ramping at this host MemAvailable threshold (global reclaim)",
    )  # lzx-note
    target.add_argument(
        "--target-memfree-mib", type=int,
        help="stop ramping at host MemFree, then run the bounded reclaim probe",
    )  # lzx-note
    parser.add_argument("--max-allocate-mib", type=int, default=28672)  # lzx-note
    parser.add_argument("--chunk-mib", type=int, default=64)
    parser.add_argument(
        "--reclaim-probe-mib", type=int, default=0,
        help="after reaching headroom, charge this many extra MiB to force bounded memcg reclaim",
    )
    parser.add_argument("--ramp-interval", type=float, default=0.25)
    parser.add_argument("--hold-seconds", type=float, default=600.0)
    parser.add_argument("--oom-score-adj", type=int, default=1000)
    parser.add_argument("--seed", type=int, default=20260821)
    parser.add_argument("--state", type=Path, required=True)
    args = parser.parse_args()
    if (args.target_mib is not None and args.target_mib <= 0) or (
        args.target_headroom_mib is not None and args.target_headroom_mib < 0
    ) or (args.target_memavailable_mib is not None and
          args.target_memavailable_mib <= 0) or (
        args.target_memfree_mib is not None and args.target_memfree_mib <= 0
    ) or args.chunk_mib <= 0 or (
        args.reclaim_probe_mib < 0 or args.max_allocate_mib <= 0
    ):
        raise SystemExit("pressure target/chunk must be positive and reclaim probe non-negative")
    if args.reclaim_probe_mib and not (
        args.target_headroom_mib is not None or args.target_memfree_mib is not None
    ):
        raise SystemExit("reclaim-probe-mib requires target-headroom-mib or target-memfree-mib")

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    score = min(1000, max(-1000, args.oom_score_adj))
    Path("/proc/self/oom_score_adj").write_text(f"{score}\n", encoding="ascii")

    cgroup = current_cgroup()
    memory_max, memory_max_owner = effective_limit(cgroup, "memory.max")  # lzx-note
    swap_max, swap_max_owner = effective_limit(cgroup, "memory.swap.max")  # lzx-note
    target_bytes = int(args.target_mib or 0) * MIB
    target_headroom_bytes = int(args.target_headroom_mib or 0) * MIB
    target_memavailable_bytes = int(args.target_memavailable_mib or 0) * MIB
    target_memfree_bytes = int(args.target_memfree_mib or 0) * MIB
    max_allocate_bytes = int(args.max_allocate_mib) * MIB
    headroom_mode = args.target_headroom_mib is not None  # lzx-note
    memavailable_mode = args.target_memavailable_mib is not None  # lzx-note
    memfree_mode = args.target_memfree_mib is not None  # lzx-note
    reclaim_probe_bytes = int(args.reclaim_probe_mib) * MIB
    chunk_bytes = args.chunk_mib * MIB
    if headroom_mode and (memory_max is None or memory_max_owner is None):
        raise SystemExit("target-headroom-mib requires a finite inherited memory.max")
    limit_owner = Path(memory_max_owner) if memory_max_owner else None
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
        "target_headroom_bytes": target_headroom_bytes,
        "target_memavailable_bytes": target_memavailable_bytes,
        "target_memfree_bytes": target_memfree_bytes,
        "max_allocate_bytes": max_allocate_bytes,
        "reclaim_probe_bytes": reclaim_probe_bytes,
        "chunk_bytes": chunk_bytes,
        "started_ns": started_ns,
    }

    def observed() -> dict[str, int]:
        result = {
            "host_memavailable_bytes": read_memavailable(),
            "host_memfree_bytes": read_memfree(),
        }
        if limit_owner is not None and memory_max is not None:
            usage = read_current(limit_owner)
            result.update({
                "limit_owner_current_bytes": usage,
                "limit_owner_headroom_bytes": max(0, memory_max - usage),
            })
        return result

    write_state(args.state, {
        **common, **observed(), "status": "RAMPING", "allocated_bytes": 0,
    })

    chunks: list[mmap.mmap] = []
    allocated = 0
    chunk_index = 0

    def allocate_amount(amount: int) -> None:
        """Charge and touch one deterministic anonymous region."""
        nonlocal allocated, chunk_index
        # lzx-note: Private RSS plus oom_score_adj=1000 keeps this disposable
        # worker the preferred victim if the bounded cgroup ever reaches OOM.
        region = mmap.mmap(
            -1,
            amount,
            flags=mmap.MAP_PRIVATE | mmap.MAP_ANONYMOUS,
            prot=mmap.PROT_READ | mmap.PROT_WRITE,
        )
        for offset in range(0, amount, PAGE_SIZE):
            region[offset] = (args.seed + chunk_index + offset // PAGE_SIZE) & 0xFF
        chunks.append(region)
        allocated += amount
        chunk_index += 1

    try:
        while not STOP:
            if headroom_mode:
                usage = read_current(limit_owner)  # type: ignore[arg-type]
                headroom = max(0, int(memory_max) - usage)
                if headroom <= target_headroom_bytes:
                    break
                wanted = headroom - target_headroom_bytes
                amount = min(chunk_bytes, wanted)
                amount = max(PAGE_SIZE, amount // PAGE_SIZE * PAGE_SIZE)
            elif memavailable_mode:
                available = read_memavailable()
                if available <= target_memavailable_bytes:
                    break
                if allocated >= max_allocate_bytes:
                    write_state(args.state, {
                        **common, **observed(), "status": "TARGET_NOT_REACHED",
                        "allocated_bytes": allocated, "finished_ns": time.time_ns(),
                    })
                    return 4
                wanted = available - target_memavailable_bytes
                amount = min(chunk_bytes, wanted, max_allocate_bytes - allocated)
                amount = max(PAGE_SIZE, amount // PAGE_SIZE * PAGE_SIZE)
            elif memfree_mode:
                free_bytes = read_memfree()
                if free_bytes <= target_memfree_bytes:
                    break
                if allocated >= max_allocate_bytes:
                    write_state(args.state, {
                        **common, **observed(), "status": "TARGET_NOT_REACHED",
                        "allocated_bytes": allocated, "finished_ns": time.time_ns(),
                    })
                    return 4
                wanted = free_bytes - target_memfree_bytes
                amount = min(chunk_bytes, wanted, max_allocate_bytes - allocated)
                amount = max(PAGE_SIZE, amount // PAGE_SIZE * PAGE_SIZE)
            else:
                if allocated >= target_bytes:
                    break
                amount = min(chunk_bytes, target_bytes - allocated)
            allocate_amount(amount)
            write_state(args.state, {
                **common, **observed(), "status": "RAMPING",
                "allocated_bytes": allocated, "reclaim_probe_allocated_bytes": 0,
            })
            if args.ramp_interval > 0 and (
                headroom_mode or memavailable_mode or memfree_mode or allocated < target_bytes
            ):
                time.sleep(args.ramp_interval)

        # lzx-note: Stopping below memory.max performs no reclaim. This bounded
        # post-prediction charge evicts file cache, then remains resident so the
        # target application's later hot-page access can refault under pressure.
        probe_allocated = 0
        while not STOP and (headroom_mode or memfree_mode) and probe_allocated < reclaim_probe_bytes:
            amount = min(chunk_bytes, reclaim_probe_bytes - probe_allocated)
            amount = max(PAGE_SIZE, amount // PAGE_SIZE * PAGE_SIZE)
            allocate_amount(amount)
            probe_allocated += amount
            write_state(args.state, {
                **common, **observed(), "status": "RECLAIM_PROBE",
                "allocated_bytes": allocated,
                "reclaim_probe_allocated_bytes": probe_allocated,
            })
            if args.ramp_interval > 0 and probe_allocated < reclaim_probe_bytes:
                time.sleep(args.ramp_interval)

        if STOP:
            write_state(args.state, {
                **common, **observed(), "status": "STOPPED",
                "allocated_bytes": allocated, "finished_ns": time.time_ns(),
            })
            return 0

        write_state(args.state, {
            **common, **observed(), "status": "HOLDING",
            "allocated_bytes": allocated,
            "reclaim_probe_allocated_bytes": probe_allocated,
            "holding_ns": time.time_ns(),
        })
        deadline = time.monotonic() + max(0.0, args.hold_seconds)
        while not STOP and time.monotonic() < deadline:
            time.sleep(min(0.5, max(0.0, deadline - time.monotonic())))
        write_state(args.state, {
            **common, **observed(),
            "status": "COMPLETE" if not STOP else "STOPPED",
            "allocated_bytes": allocated,
            "reclaim_probe_allocated_bytes": probe_allocated,
            "finished_ns": time.time_ns(),
        })
        return 0
    except (MemoryError, OSError) as exc:
        write_state(args.state, {
            **common, **observed(), "status": "ALLOCATION_ERROR",
            "allocated_bytes": allocated, "finished_ns": time.time_ns(),
            "error": f"{type(exc).__name__}: {exc}",
        })
        return 3
    finally:
        for region in chunks:
            region.close()


if __name__ == "__main__":
    raise SystemExit(main())
