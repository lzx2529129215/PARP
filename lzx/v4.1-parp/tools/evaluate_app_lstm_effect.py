#!/usr/bin/env python3
"""Evaluate the App-LSTM contribution to the v4.1 PARP policy.

Inputs are deliberately plain CSV files so the evaluator can consume either
real runtime-monitor exports or a controlled fixture.  The evaluator compares
the same samples under:

* ``native``: foreground-only protection and no next-App headroom;
* ``lstm``: App-LSTM scores routed into next-App headroom and App budgets.

Neither mode writes debugfs or changes the kernel.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(ROOT / "tools"))

from v41_core import (  # noqa: E402
    evaluate_sample,
    load_app_states,
    load_json,
    load_predictions,
    load_samples,
    summarize,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", required=True, help="sample-level CSV")
    parser.add_argument("--app-states", required=True, help="per-sample App state CSV")
    parser.add_argument("--predictions", required=True, help="normalized App-LSTM prediction CSV")
    parser.add_argument("--config", default=str(ROOT / "configs" / "default.json"))
    parser.add_argument("--horizon-ms", type=int)
    parser.add_argument("--model-version", type=int)
    parser.add_argument("--output-dir", required=True)
    return parser.parse_args()


def write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fields = list(rows[0].keys())
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    args = parse_args()
    config = load_json(args.config)
    horizon_ms = args.horizon_ms or int(config.get("horizon_ms", 300_000))
    samples = load_samples(args.samples)
    states = load_app_states(args.app_states)
    predictions = load_predictions(
        args.predictions,
        horizon_ms=horizon_ms,
        model_version=args.model_version,
    )
    rows = [
        evaluate_sample(sample, states.get(sample.sample_id, []), predictions.get(sample.sample_id, []), config)
        for sample in samples
    ]
    summary = summarize(rows)
    summary.update(
        {
            "experiment": "v4.1-parp-app-inter-lstm",
            "horizon_ms": horizon_ms,
            "model_version": args.model_version,
            "policy_mode": "native_vs_lstm_counterfactual",
            "apply_enabled": False,
        }
    )

    output_dir = Path(args.output_dir)
    write_csv(output_dir / "per_sample.csv", rows)
    (output_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
