#!/usr/bin/env python3
"""Convert one App-LSTM snapshot into v4-parp observe-only commands.

The command format matches the v4-parp debugfs patch:

* ``app_bind``: ``domain_id app_id ttl_ms epoch_id model_version``
* ``app_prior``: ``app_id use_score_q15 rank horizon_ms ttl_ms model_version``

The generated file is an audit artifact.  This tool never writes debugfs.
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(ROOT / "tools"))

from v41_core import load_app_states, load_json, load_predictions, q15  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sample-id", required=True)
    parser.add_argument("--app-states", required=True)
    parser.add_argument("--predictions", required=True)
    parser.add_argument("--config", default=str(ROOT / "configs" / "default.json"))
    parser.add_argument("--horizon-ms", type=int)
    parser.add_argument("--epoch-id", type=int, default=1)
    parser.add_argument("--model-version", type=int)
    parser.add_argument("--ttl-ms", type=int)
    parser.add_argument("--output", required=True)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_json(args.config)
    horizon_ms = args.horizon_ms or int(config.get("horizon_ms", 300_000))
    model_version = args.model_version
    ttl_ms = args.ttl_ms or int(config.get("ttl_ms", 30_000))
    states = load_app_states(args.app_states).get(args.sample_id, [])
    predictions = load_predictions(
        args.predictions,
        horizon_ms=horizon_ms,
        model_version=model_version,
    ).get(args.sample_id, [])
    if model_version is None:
        model_version = next((item.model_version for item in predictions if item.model_version), 401)

    lines = [
        "# v4.1-parp generated commands; observe-only audit artifact",
        "# Do not use these commands with PARP apply mode.",
        "# app_bind: domain_id app_id ttl_ms epoch_id model_version",
        "# app_prior: app_id use_score_q15 rank horizon_ms ttl_ms model_version",
    ]
    for state in sorted(states, key=lambda item: (item.domain_id, item.app_id)):
        if state.domain_id <= 0 or state.app_id <= 0:
            continue
        lines.append(
            f"echo '{state.domain_id} {state.app_id} {ttl_ms} {args.epoch_id} {model_version}' "
            "> /sys/kernel/debug/parp/app_bind"
        )
    for item in predictions:
        lines.append(
            f"echo '{item.app_id} {q15(item.use_score)} {item.rank} {horizon_ms} "
            f"{ttl_ms} {model_version}' > /sys/kernel/debug/parp/app_prior"
        )
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"saved: {args.output}")


if __name__ == "__main__":
    main()
