#!/usr/bin/env python3
"""Launch a GUI and exact-reuse fixture in one application cgroup. lzx-note"""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path


STOP = False


def stop(_signum: int, _frame: object) -> None:
    global STOP
    STOP = True


def terminate(process: subprocess.Popen[bytes] | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def parse_args() -> tuple[argparse.Namespace, list[str]]:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--app", required=True)
    parser.add_argument("--socket", type=Path, required=True)
    parser.add_argument("--file", type=Path, required=True)
    parser.add_argument("--log", type=Path, required=True)
    parser.add_argument("--file-bytes", type=int, required=True)
    parser.add_argument("--anon-bytes", type=int, required=True)
    parser.add_argument("--hot-bytes", type=int, required=True)
    parser.add_argument("--materialize-file", action="store_true")
    args, gui = parser.parse_known_args()
    if gui and gui[0] == "--":
        gui = gui[1:]
    if not gui:
        parser.error("GUI command is required after --")
    return args, gui


def main() -> int:
    args, gui = parse_args()
    fixture_command = [
        sys.executable, str(args.fixture), "--app", args.app,
        "--socket", str(args.socket), "--file", str(args.file),
        "--log", str(args.log), "--file-bytes", str(args.file_bytes),
        "--anon-bytes", str(args.anon_bytes), "--hot-bytes", str(args.hot_bytes),
    ]
    if args.materialize_file:
        fixture_command.append("--materialize-file")

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    fixture: subprocess.Popen[bytes] | None = None
    application: subprocess.Popen[bytes] | None = None
    try:
        fixture = subprocess.Popen(fixture_command, env=os.environ.copy())
        deadline = time.monotonic() + 30
        while not args.socket.exists():
            if STOP or fixture.poll() is not None or time.monotonic() >= deadline:
                return 2
            time.sleep(0.05)
        application = subprocess.Popen(gui, env=os.environ.copy())
        while not STOP and application.poll() is None:
            time.sleep(0.1)
        if STOP:
            terminate(application)
        return application.returncode if application and application.returncode is not None else 0
    finally:
        terminate(application)
        terminate(fixture)


if __name__ == "__main__":
    raise SystemExit(main())
