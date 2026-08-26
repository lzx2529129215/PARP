#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import mmap
import os
import signal
import time


def main() -> int:
    parser = argparse.ArgumentParser(description="Deterministic anonymous-memory pressure worker")
    parser.add_argument("--mb", type=int, required=True)
    parser.add_argument("--duration", type=float, default=300)
    parser.add_argument("--touch-interval", type=float, default=1.0)
    parser.add_argument("--stride-pages", type=int, default=256)
    parser.add_argument("--mode", choices=("resident-idle", "sparse", "active"), default="resident-idle")
    parser.add_argument("--label", default="memory-worker")
    args = parser.parse_args()

    size = args.mb * 1024 * 1024
    page = mmap.PAGESIZE
    region = mmap.mmap(-1, size, access=mmap.ACCESS_WRITE)
    stop = False

    def request_stop(_signum, _frame):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    for offset in range(0, size, page):
        region[offset] = (offset // page) & 0xFF

    print(json.dumps({"event": "ready", "label": args.label, "pid": os.getpid(), "bytes": size}), flush=True)
    deadline = time.monotonic() + args.duration
    rounds = 0
    while not stop and time.monotonic() < deadline:
        if args.mode != "resident-idle":
            stride_pages = 1 if args.mode == "active" else max(args.stride_pages, 1)
            for offset in range(0, size, page * stride_pages):
                region[offset] = (region[offset] + 1) & 0xFF
            rounds += 1
        time.sleep(min(args.touch_interval, max(0.0, deadline - time.monotonic())))
    print(json.dumps({"event": "done", "label": args.label, "pid": os.getpid(), "touch_rounds": rounds}), flush=True)
    region.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
