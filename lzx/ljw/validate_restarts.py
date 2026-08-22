#!/usr/bin/env python3
"""Cold-start Bilibili repeatedly and record role stability."""

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from inspect_app import snapshot


EXECUTABLE = "/opt/apps/io.github.msojocs.bilibili/files/bin/bin/bilibili"
PATTERN = r"bilibili|bili-app|io\.github\.msojocs\.bilibili"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--rounds", type=int, default=5)
    parser.add_argument("--settle", type=float, default=8.0)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8") as output:
        for round_number in range(1, args.rounds + 1):
            log_path = args.output.with_name(f"restart-{round_number:02d}.log")
            with log_path.open("w", encoding="utf-8") as log:
                process = subprocess.Popen(
                    [EXECUTABLE], stdout=log, stderr=subprocess.STDOUT,
                    start_new_session=True, text=True,
                )
                try:
                    time.sleep(args.settle)
                    value = snapshot(PATTERN)
                    value["round"] = round_number
                    value["launcher_pid"] = process.pid
                    output.write(json.dumps(value, ensure_ascii=False) + "\n")
                    output.flush()
                finally:
                    if process.poll() is None:
                        os.killpg(process.pid, signal.SIGINT)
                        try:
                            process.wait(timeout=8)
                        except subprocess.TimeoutExpired:
                            os.killpg(process.pid, signal.SIGTERM)
                            process.wait(timeout=5)
            time.sleep(2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
