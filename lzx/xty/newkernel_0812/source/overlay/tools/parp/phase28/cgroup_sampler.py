#!/usr/bin/env python3
"""Causal kernel/cgroup metrics sampler for a single isolated app domain."""

import argparse
import json
import os
from pathlib import Path
import signal
import time


STOP = False


def request_stop(_signum, _frame):
    global STOP
    STOP = True


def read_text(path):
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None


def flat_key_values(text):
    if text is None:
        return None
    values = {}
    for line in text.splitlines():
        fields = line.split()
        if len(fields) >= 2:
            value = fields[1]
            try:
                value = int(value)
            except ValueError:
                try:
                    value = float(value)
                except ValueError:
                    pass
            values[fields[0]] = value
    return values


def pressure(text):
    if text is None:
        return None
    output = {}
    for line in text.splitlines():
        fields = line.split()
        if not fields:
            continue
        output[fields[0]] = {}
        for field in fields[1:]:
            key, value = field.split("=", 1)
            output[fields[0]][key] = float(value) if "." in value else int(value)
    return output


def io_stat(text):
    if text is None:
        return None
    rows = []
    for line in text.splitlines():
        fields = line.split()
        if not fields:
            continue
        row = {"device": fields[0]}
        for field in fields[1:]:
            key, value = field.split("=", 1)
            row[key] = int(value)
        rows.append(row)
    return rows


def pid_snapshot(cgroup):
    pids = set()
    for path in cgroup.rglob("cgroup.procs"):
        text = read_text(path)
        if text:
            pids.update(int(item) for item in text.split() if item.isdigit())
    aggregate = {
        "pid_count": len(pids), "rss_anon_kib": 0, "rss_file_kib": 0,
        "vm_swap_kib": 0, "referenced_kib": 0, "pss_kib": 0,
        "sched_runtime_ns": 0, "sched_wait_ns": 0, "sched_timeslices": 0,
        "unreadable_pids": 0,
    }
    for pid in sorted(pids):
        status = flat_key_values(read_text(Path("/proc") / str(pid) / "status"))
        sched = read_text(Path("/proc") / str(pid) / "schedstat")
        smaps = flat_key_values(read_text(Path("/proc") / str(pid) / "smaps_rollup"))
        if status is None:
            aggregate["unreadable_pids"] += 1
            continue
        aggregate["rss_anon_kib"] += int(status.get("RssAnon:", 0))
        aggregate["rss_file_kib"] += int(status.get("RssFile:", 0))
        aggregate["vm_swap_kib"] += int(status.get("VmSwap:", 0))
        if smaps:
            aggregate["referenced_kib"] += int(smaps.get("Referenced:", 0))
            aggregate["pss_kib"] += int(smaps.get("Pss:", 0))
        if sched:
            fields = sched.split()
            if len(fields) >= 3 and all(item.isdigit() for item in fields[:3]):
                aggregate["sched_runtime_ns"] += int(fields[0])
                aggregate["sched_wait_ns"] += int(fields[1])
                aggregate["sched_timeslices"] += int(fields[2])
    return aggregate


def snapshot(cgroup, sequence):
    scalar_names = (
        "memory.current", "memory.peak", "memory.swap.current", "memory.swap.peak",
        "memory.low", "memory.high", "memory.max", "pids.current",
    )
    scalars = {name.replace(".", "_"): read_text(cgroup / name) for name in scalar_names}
    for key, value in list(scalars.items()):
        if value is not None and value.isdigit():
            scalars[key] = int(value)
    metric_files = list(scalar_names) + [
        "memory.stat", "memory.events", "memory.events.local", "cpu.stat", "io.stat",
        "memory.pressure", "cpu.pressure", "io.pressure",
    ]
    return {
        "schema_version": 1,
        "source": "RUNTIME_PHASE28_REAL_FRESH",
        "sequence": sequence,
        "monotonic_ns": time.clock_gettime_ns(time.CLOCK_MONOTONIC),
        "wall_time_ns": time.time_ns(),
        "cgroup_inode": cgroup.stat().st_ino,
        "cgroup_metrics": scalars,
        "memory_stat": flat_key_values(read_text(cgroup / "memory.stat")),
        "memory_events": flat_key_values(read_text(cgroup / "memory.events")),
        "memory_events_local": flat_key_values(read_text(cgroup / "memory.events.local")),
        "cpu_stat": flat_key_values(read_text(cgroup / "cpu.stat")),
        "io_stat": io_stat(read_text(cgroup / "io.stat")),
        "pressure": {
            name: pressure(read_text(cgroup / (name + ".pressure")))
            for name in ("memory", "cpu", "io")
        },
        "process_aggregate": pid_snapshot(cgroup),
        "system_pressure": {
            name: pressure(read_text(Path("/proc/pressure") / name))
            for name in ("memory", "cpu", "io")
        },
        "availability": {name: (cgroup / name).is_file() for name in metric_files},
        "labels_present": False,
        "automation_fields_present": False,
        "future_features_used": False,
    }


def run(cgroup, output, interval_ms):
    if not cgroup.is_dir() or not (cgroup / "cgroup.procs").exists():
        raise SystemExit("not a cgroup v2 directory: %s" % cgroup)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name + ".partial")
    sequence = 0
    deadline = time.monotonic()
    with temporary.open("w", encoding="utf-8", buffering=1) as stream:
        while not STOP:
            stream.write(json.dumps(snapshot(cgroup, sequence), sort_keys=True) + "\n")
            sequence += 1
            deadline += interval_ms / 1000.0
            remaining = deadline - time.monotonic()
            if remaining > 0:
                time.sleep(remaining)
            else:
                deadline = time.monotonic()
    os.replace(temporary, output)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cgroup", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--interval-ms", type=int, default=1000)
    args = parser.parse_args()
    if not 100 <= args.interval_ms <= 10000:
        raise SystemExit("interval-ms must be in [100, 10000]")
    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    run(args.cgroup, args.output, args.interval_ms)


if __name__ == "__main__":
    main()
