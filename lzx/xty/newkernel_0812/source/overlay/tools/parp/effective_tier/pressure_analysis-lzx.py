#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0  #lzx
"""Offline Phase-F pressure-policy replay over exported SHADOW candidates."""

from __future__ import annotations

import argparse
from importlib import import_module
import sys
from pathlib import Path
from typing import Optional, Sequence

try:
    from .contracts import ContractError, read_jsonl, reject_live_path, write_json
    pressure_policy_ablation = import_module(
        ".pressure-lzx", __package__).pressure_policy_ablation
except ImportError:  # Direct execution from this directory.
    from contracts import ContractError, read_jsonl, reject_live_path, write_json  # type: ignore
    pressure_policy_ablation = import_module(
        "pressure-lzx").pressure_policy_ablation


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Replay Phase-F pressure policies from exported JSONL only")
    parser.add_argument("--samples", required=True, type=Path,
                        help="exported labeled_candidates.jsonl")
    parser.add_argument("--output", required=True, type=Path,
                        help="pressure_policy_ablation.json")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    try:
        reject_live_path(args.samples)
        reject_live_path(args.output)
        result = pressure_policy_ablation(read_jsonl([args.samples]))
        args.output.parent.mkdir(parents=True, exist_ok=True)
        write_json(args.output, result)
    except (ContractError, OSError, ValueError) as exc:
        print("pressure-analysis: %s" % exc, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
