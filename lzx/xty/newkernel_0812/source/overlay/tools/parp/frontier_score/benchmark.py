#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0
"""No-reclaim microbenchmark for the pure integer reference scorer."""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from tools.parp.frontier_score.reference import load_models, score_model


def percentile(values, quantile):
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int((len(ordered) - 1) * quantile))]


def run(iterations):
    models = load_models()
    features = [50, 200, 128, 300, 1, 2, 700, 20000]
    output = {"iterations": iterations, "clock": "perf_counter_ns",
              "measurements": {}}
    for app_id in (0, 1, 2, 3):
        model = models[app_id]
        samples = []
        for _ in range(iterations):
            started = time.perf_counter_ns()
            score_model(model, features, 1, 1)
            samples.append(time.perf_counter_ns() - started)
        output["measurements"][model.model_name] = {
            "p50_ns": percentile(samples, .50),
            "p95_ns": percentile(samples, .95),
            "p99_ns": percentile(samples, .99),
            "max_ns": max(samples),
            "mean_ns": statistics.fmean(samples),
        }
    disabled = []
    for _ in range(iterations):
        started = time.perf_counter_ns()
        enabled = False
        if enabled:  # pragma: no cover - intentionally measures OFF branch
            score_model(models[0], features, 1, 1)
        disabled.append(time.perf_counter_ns() - started)
    output["measurements"]["OFF_BRANCH"] = {
        "p50_ns": percentile(disabled, .50),
        "p95_ns": percentile(disabled, .95),
        "p99_ns": percentile(disabled, .99),
        "max_ns": max(disabled),
        "mean_ns": statistics.fmean(disabled),
    }
    return output


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--iterations", type=int, default=20000)
    parser.add_argument("--output")
    args = parser.parse_args()
    payload = run(args.iterations)
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if args.output:
        with open(args.output, "w", encoding="utf-8") as stream:
            stream.write(text)
    else:
        print(text, end="")


if __name__ == "__main__":
    main()
