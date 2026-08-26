#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import platform
import subprocess
import sys
import time
from pathlib import Path

from memsched_exp.protocol import read_marker, wait_for_markers, write_marker


def current_cgroup() -> Path:
    for line in Path("/proc/self/cgroup").read_text(encoding="utf-8").splitlines():
        if line.startswith("0::"):
            return Path("/sys/fs/cgroup") / line[3:].lstrip("/")
    raise RuntimeError("cgroup v2 unified entry is unavailable")


def main() -> int:
    parser = argparse.ArgumentParser(description="Generic Linux collector protocol integration smoke test")
    parser.add_argument("--output", required=True)
    parser.add_argument("--duration", type=float, default=10.0)
    parser.add_argument("--current-cgroup", action="store_true")
    parser.add_argument(
        "--require-kernel-prefix",
        help="optional release prefix for a controlled experiment; omitted by default",
    )
    args = parser.parse_args()
    if args.duration <= 0.1:
        raise ValueError("duration must be greater than 0.1 seconds")
    if args.require_kernel_prefix and not platform.release().startswith(args.require_kernel_prefix):
        raise RuntimeError(
            f"expected kernel prefix {args.require_kernel_prefix!r}, got {platform.release()}"
        )
    if not Path("/sys/fs/cgroup/cgroup.controllers").exists():
        raise RuntimeError("cgroup v2 unified hierarchy is unavailable")

    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=False)
    ready = output / "collector-ready.json"
    start = output / "workload-start.json"
    stop = output / "workload-stop.json"
    done = output / "collector-done.json"
    command = [
        sys.executable,
        "-m",
        "memsched_exp.cli",
        "collect",
        "--name",
        "linux-integration-smoke",
        "--duration",
        str(args.duration),
        "--interval",
        "0.1",
        "--output",
        str(output / "run"),
        "--ready-file",
        str(ready),
        "--start-file",
        str(start),
        "--stop-file",
        str(stop),
        "--done-file",
        str(done),
    ]
    if args.current_cgroup:
        command.extend(["--cgroup", str(current_cgroup())])
    collector = subprocess.Popen(command)
    try:
        wait_for_markers([ready], 30)
        write_marker(start, "workload_start", integration_smoke=True)
        memory = bytearray(32 * 1024 * 1024)
        workload_duration = min(2.0, args.duration * 0.5)
        deadline = time.monotonic() + workload_duration
        while time.monotonic() < deadline:
            for offset in range(0, len(memory), 4096):
                memory[offset] = (memory[offset] + 1) % 256
        write_marker(stop, "workload_stop", integration_smoke=True)
        return_code = collector.wait(timeout=30)
    finally:
        if collector.poll() is None:
            collector.terminate()
            collector.wait(timeout=5)
    if return_code != 0:
        raise RuntimeError(f"collector returned {return_code}")

    values = [read_marker(path) for path in (ready, start, stop, done)]
    timestamps = [int(value["monotonic_ns"]) for value in values]
    if timestamps != sorted(timestamps) or len(set(timestamps)) != len(timestamps):
        raise RuntimeError(f"protocol marker ordering is invalid: {timestamps}")
    before = json.loads((output / "run" / "before.json").read_text(encoding="utf-8"))
    after = json.loads((output / "run" / "after.json").read_text(encoding="utf-8"))
    summary = json.loads((output / "run" / "summary.json").read_text(encoding="utf-8"))
    if not (before["monotonic_ns"] < timestamps[0] <= timestamps[1]):
        raise RuntimeError("before snapshot was not durable before workload start")
    if not (timestamps[2] <= after["monotonic_ns"] <= timestamps[3]):
        raise RuntimeError("after snapshot was not durable before collector-done")
    if args.current_cgroup and not summary.get("cgroup", {}).get("valid"):
        raise RuntimeError(f"cgroup endpoint invalid: {summary.get('cgroup')}")
    print(json.dumps({"valid": True, "output": str(output), "timestamps": timestamps}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
