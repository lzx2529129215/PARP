#!/usr/bin/env python3
"""Cgroup-confined, OOM-preferred anonymous burst used for peak calibration."""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path


MIB = 1024 * 1024
PAGE_SIZE = os.sysconf("SC_PAGE_SIZE")


def memtotal() -> int:
    for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
        if line.startswith("MemTotal:"):
            return int(line.split()[1]) * 1024
    raise RuntimeError("MemTotal unavailable")


def write_state(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--memory-ratio", type=float, required=True)
    parser.add_argument("--ramp-seconds", type=float, default=8.0)
    parser.add_argument("--hold-seconds", type=float, default=12.0)
    parser.add_argument("--oom-score-adj", type=int, default=1000)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not 0 < args.memory_ratio < 1:
        raise SystemExit("memory ratio must be between zero and one")
    score = min(1000, max(-1000, args.oom_score_adj))
    Path("/proc/self/oom_score_adj").write_text(f"{score}\n", encoding="ascii")
    target = int(memtotal() * args.memory_ratio)
    chunk_bytes = 16 * MIB
    chunks: list[bytearray] = []
    started = time.time_ns()
    write_state(args.output, {"status": "RAMPING", "pid": os.getpid(), "target_bytes": target, "allocated_bytes": 0, "started_ns": started})
    delay = args.ramp_seconds / max(1, (target + chunk_bytes - 1) // chunk_bytes)
    allocated = 0
    while allocated < target:
        amount = min(chunk_bytes, target - allocated)
        chunk = bytearray(amount)
        for offset in range(0, amount, PAGE_SIZE):
            chunk[offset] = (allocated // PAGE_SIZE + offset // PAGE_SIZE) & 0xFF
        chunks.append(chunk)
        allocated += amount
        if delay:
            time.sleep(delay)
    write_state(args.output, {"status": "HOLDING", "pid": os.getpid(), "target_bytes": target, "allocated_bytes": allocated, "started_ns": started})
    time.sleep(max(0.0, args.hold_seconds))
    write_state(args.output, {"status": "COMPLETE", "pid": os.getpid(), "target_bytes": target, "allocated_bytes": allocated, "started_ns": started, "finished_ns": time.time_ns()})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
