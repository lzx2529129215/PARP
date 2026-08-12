#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0
"""Probe or decode controlled PARP DAMON collection without privilege changes."""

import argparse
import json
from pathlib import Path
import platform

from region_decode import decode_trace_line


def probe():
    paths = {
        "damon_sysfs": Path("/sys/kernel/mm/damon/admin"),
        "tracefs": Path("/sys/kernel/tracing"),
        "debug_tracefs": Path("/sys/kernel/debug/tracing"),
        "parp_debugfs": Path("/sys/kernel/debug/parp"),
    }
    def path_status(path):
        try:
            return {"exists": path.exists(), "readable": path.exists()}
        except PermissionError:
            return {"exists": None, "readable": False, "error": "EACCES"}

    status = {name: path_status(path) for name, path in paths.items()}
    trace_root = paths["tracefs"] if status["tracefs"]["exists"] else paths["debug_tracefs"]
    event = trace_root / "events/parp/parp_region_evidence"
    event_status = path_status(event)
    status.update({
        "kernel": platform.release(),
        "parp_region_tracepoint": event_status,
        "runtime_collection_ready": status["damon_sysfs"]["readable"] and
                                    event_status["readable"] and
                                    status["parp_debugfs"]["readable"],
        "policy": "no sudo, unrelated target, reclaim action, install, or boot change",
    })
    return status


def decode(source: Path, output: Path):
    count = 0
    with source.open(encoding="utf-8", errors="replace") as reader, \
            output.open("w", encoding="utf-8") as writer:
        for line in reader:
            record = decode_trace_line(line)
            if record is not None:
                writer.write(json.dumps(record, sort_keys=True) + "\n")
                count += 1
    return count


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--probe", action="store_true")
    parser.add_argument("--decode-trace", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.probe:
        print(json.dumps(probe(), indent=2, sort_keys=True))
        return
    if not args.decode_trace or not args.output:
        parser.error("--decode-trace and --output are required together")
    print(json.dumps({"decoded_records": decode(args.decode_trace, args.output)}))


if __name__ == "__main__":
    main()
