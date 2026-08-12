#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0
"""Submit bounded complete synthetic LSTM batches with generation resume."""

import argparse
import json
import sys

from lstm_prior_bridge import AsyncPriorBridge, BatchBuilder, DebugfsTransport


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--debugfs",
                        default="/sys/kernel/debug/parp/app_prior_batch")
    parser.add_argument("--count", type=int, default=5)
    parser.add_argument("--foreground", default="SYNTHETIC_FG")
    args = parser.parse_args()
    if not 1 <= args.count <= 32:
        parser.error("count must be between 1 and 32")
    whitelist = {
        "SYNTHETIC_FG": 9001,
        "SYNTHETIC_HIGH": 9002,
        "SYNTHETIC_MEDIUM": 9003,
        "SYNTHETIC_LOW": 9004,
    }
    rows = [
        {"app_key": "SYNTHETIC_FG", "probability": 0.95},
        {"app_key": "SYNTHETIC_HIGH", "probability": 0.80},
        {"app_key": "SYNTHETIC_MEDIUM", "probability": 0.50},
        {"app_key": "SYNTHETIC_LOW", "probability": 0.10},
    ]
    transport = DebugfsTransport(args.debugfs)
    bridge = AsyncPriorBridge(BatchBuilder(whitelist, model_version=9),
                              transport)
    try:
        for index in range(args.count):
            result = bridge.submit_event(
                "phase26-synthetic-{}".format(index), rows,
                args.foreground).result(5)
            print(json.dumps(result, sort_keys=True), flush=True)
            if not result.get("submitted"):
                raise RuntimeError(result.get("error", "batch not submitted"))
    finally:
        bridge.close()
    print(json.dumps({"source": "RUNTIME_LEVEL3A",
                      "kernel_generation_at_start":
                      bridge.kernel_generation_at_start,
                      "submitted": args.count}, sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(json.dumps({"error": str(exc), "submitted": False}),
              file=sys.stderr)
        raise
