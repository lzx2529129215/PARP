#!/usr/bin/env python3
"""Generate deterministic workload sequence via real memory operations.

Produces a repeatable workload state sequence that the existing
workload_classifier can observe, creating natural 2nd-order Markov contexts.

Usage:
  python3 scripts/generate_markov_workload_sequence.py \
    --sequence 0,1,2,0,1,6,0,1,2 \
    --phase-duration-s 4 \
    --output /path/to/trace.csv
"""

from __future__ import annotations

import argparse
import csv
import os
import random
import sys
import time
from pathlib import Path
from typing import Any

WORKLOAD_MAP = {
    0: ("LOW_ACTIVITY", "sleep"),
    1: ("ANON_FAULT_HEAVY", "anon_alloc_and_touch"),
    2: ("FILE_FAULT_HEAVY", "file_mmap_and_read"),
    3: ("FILE_REFAULT_HEAVY", "file_refault"),
    4: ("MAJOR_FAULT_HEAVY", "major_fault"),
    5: ("MEMORY_GROWTH_HEAVY", "memory_growth"),
    6: ("MIXED_ACTIVE", "mixed_anon_file"),
}

OUTPUT_FIELDS = [
    "phase_start_ns",
    "phase_end_ns",
    "requested_workload_id",
    "requested_workload_name",
    "actual_action",
    "allocated_bytes",
    "file_bytes",
    "generator_pid",
    "scope_path",
    "phase_index",
    "status",
]


def generate_sequence(
    sequence: list[int],
    phase_duration_s: float,
    anon_mb: int,
    file_mb: int,
    hold_after_s: float,
    output: Path,
    max_memory_mb: int,
    seed: int | None,
) -> dict[str, Any]:
    random.seed(seed)
    trace_rows: list[dict[str, Any]] = []
    tmp_dir = output.parent / "workload_tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)

    # Track allocated memory to stay under max
    allocated: list[bytearray] = []
    file_paths: list[Path] = []

    for phase_idx, wl_id in enumerate(sequence):
        wl_name, action = WORKLOAD_MAP.get(wl_id, (f"UNKNOWN_{wl_id}", "unknown"))
        phase_start = time.time_ns()
        allocated_bytes = 0
        file_bytes = 0
        status = "ok"

        print(f"[Phase {phase_idx + 1}/{len(sequence)}] wl={wl_id} {wl_name} ({action})", flush=True)

        if wl_id == 0:  # LOW_ACTIVITY
            # Release all allocations
            allocated.clear()
            for fp in file_paths:
                try:
                    fp.unlink(missing_ok=True)
                except OSError:
                    pass
            file_paths.clear()
            time.sleep(phase_duration_s)

        elif wl_id == 1:  # ANON_FAULT_HEAVY
            # Check memory budget
            current_total = sum(len(a) for a in allocated)
            if current_total + anon_mb * 1024 * 1024 > max_memory_mb * 1024 * 1024:
                allocated.clear()
            # Allocate and touch anonymous memory
            size = anon_mb * 1024 * 1024
            try:
                data = bytearray(size)
                for i in range(0, size, 4096):
                    data[i] = 1
                allocated.append(data)
                allocated_bytes = size
            except MemoryError:
                status = "allocation_failed"
                allocated_bytes = 0
            hold_start = time.time()
            while time.time() - hold_start < phase_duration_s:
                # Keep touching pages
                for a in allocated[-1:]:
                    for i in range(0, len(a), 4096):
                        a[i] = (a[i] + 1) & 0xFF
                time.sleep(0.5)

        elif wl_id == 2:  # FILE_FAULT_HEAVY
            # Create temp file and read it
            fp = tmp_dir / f"wl_file_{phase_idx}_{os.getpid()}.dat"
            size = file_mb * 1024 * 1024
            try:
                data = bytes([random.randint(0, 255) for _ in range(min(size, 4096))])
                # Write file with repeated block pattern
                with fp.open("wb") as f:
                    for _ in range(0, size, 4096):
                        f.write(data * (4096 // len(data) + 1))
                        f.write(data)
                fp_st = fp.stat()
                file_bytes = fp_st.st_size
                file_paths.append(fp)
            except OSError as e:
                status = f"file_error:{e}"
                file_bytes = 0

            hold_start = time.time()
            while time.time() - hold_start < phase_duration_s:
                # Read file sequentially to trigger file faults
                try:
                    with fp.open("rb") as f:
                        while f.read(65536):
                            pass
                except OSError:
                    pass
                time.sleep(0.5)

            # Clean up
            try:
                fp.unlink(missing_ok=True)
            except OSError:
                pass
            if fp in file_paths:
                file_paths.remove(fp)

        elif wl_id == 6:  # MIXED_ACTIVE
            # Small anonymous allocation + small file access
            anon_sz = min(anon_mb // 2, 32) * 1024 * 1024
            try:
                data = bytearray(anon_sz)
                for i in range(0, anon_sz, 4096):
                    data[i] = 1
                allocated.append(data)
                allocated_bytes = anon_sz
            except MemoryError:
                status = "partial_allocation"
                allocated_bytes = 0

            fp = tmp_dir / f"wl_mixed_{phase_idx}_{os.getpid()}.dat"
            sz = min(file_mb // 2, 32) * 1024 * 1024
            try:
                with fp.open("wb") as f:
                    f.write(b"X" * sz)
                file_bytes = sz
                file_paths.append(fp)
            except OSError:
                file_bytes = 0

            hold_start = time.time()
            while time.time() - hold_start < phase_duration_s:
                for a in allocated[-1:]:
                    for i in range(0, len(a), 8192):
                        a[i] = (a[i] + 1) & 0xFF
                try:
                    with fp.open("rb") as f:
                        f.read(32768)
                except OSError:
                    pass
                time.sleep(0.5)

            try:
                fp.unlink(missing_ok=True)
            except OSError:
                pass
            if fp in file_paths:
                file_paths.remove(fp)

        else:
            time.sleep(phase_duration_s)
            status = f"unknown_workload_{wl_id}"

        phase_end = time.time_ns()
        trace_rows.append({
            "phase_start_ns": phase_start,
            "phase_end_ns": phase_end,
            "requested_workload_id": wl_id,
            "requested_workload_name": wl_name,
            "actual_action": action,
            "allocated_bytes": allocated_bytes,
            "file_bytes": file_bytes,
            "generator_pid": os.getpid(),
            "scope_path": os.environ.get("MARKOV_SCOPE_PATH", ""),
            "phase_index": phase_idx,
            "status": status,
        })

    # Keep the systemd scope alive while the parent triggers bounded reclaim.
    if hold_after_s > 0:
        time.sleep(hold_after_s)

    # Cleanup
    allocated.clear()
    for fp in file_paths:
        try:
            fp.unlink(missing_ok=True)
        except OSError:
            pass
    try:
        tmp_dir.rmdir()
    except OSError:
        pass

    # Write trace
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=OUTPUT_FIELDS)
        writer.writeheader()
        for row in trace_rows:
            writer.writerow(row)

    print(f"\nTrace written to {output}: {len(trace_rows)} phases", flush=True)
    return {"phases": len(trace_rows), "output": str(output)}


def main() -> int:
    p = argparse.ArgumentParser(description="Generate Markov workload sequence")
    p.add_argument("--sequence", default="0,1,2,0,1,6,0,1,2")
    p.add_argument("--phase-duration-s", type=float, default=4)
    p.add_argument("--anon-mb", type=int, default=80)
    p.add_argument("--file-mb", type=int, default=80)
    p.add_argument("--hold-after-s", type=float, default=20)
    p.add_argument("--output", default="outputs/mglru/workload_generator_trace.csv")
    p.add_argument("--max-memory-mb", type=int, default=256)
    p.add_argument("--seed", type=int, default=42)
    args = p.parse_args()

    sequence = [int(x.strip()) for x in args.sequence.split(",") if x.strip()]
    print(f"Sequence: {sequence}")
    print(f"Phase duration: {args.phase_duration_s}s")
    print(f"Total approx: {len(sequence) * args.phase_duration_s}s + hold {args.hold_after_s}s")
    print()

    generate_sequence(
        sequence=sequence,
        phase_duration_s=args.phase_duration_s,
        anon_mb=args.anon_mb,
        file_mb=args.file_mb,
        hold_after_s=args.hold_after_s,
        output=Path(args.output),
        max_memory_mb=args.max_memory_mb,
        seed=args.seed,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
