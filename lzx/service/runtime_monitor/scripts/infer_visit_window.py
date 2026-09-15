#!/usr/bin/env python3
"""Predict one JSON input using the same adapter as the online monitor."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

RUNTIME = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RUNTIME))
from layout import OPERATION_PREDICTOR_ROOT as ROOT
from predictor_visit_window import OnlineVisitWindowPredictor


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-json", help="Input file; stdin when omitted.")
    parser.add_argument("--checkpoint", default=str(ROOT / "outputs/lsapp_expanded/visit_window_v1/app_lstm_visit_window.pt"))
    parser.add_argument("--app-vocab", default=str(ROOT / "data/vocab/lsapp_expanded/app_vocab_duration.json"))
    parser.add_argument("--group-vocab", default=str(ROOT / "data/vocab/lsapp_expanded/user_group_vocab.json"))
    parser.add_argument("--device", default="cpu", choices=["cpu", "cuda", "auto"])
    parser.add_argument("--hot-threshold", type=float, default=.90)
    parser.add_argument("--cold-threshold", type=float, default=.20)
    args = parser.parse_args()
    row = json.loads(Path(args.input_json).read_text()) if args.input_json else json.load(sys.stdin)
    predictor = OnlineVisitWindowPredictor(args.checkpoint, args.app_vocab, args.group_vocab,
        row.get("user_group", "通用用户"), args.device, args.hot_threshold, args.cold_threshold)
    bundle = predictor.predict_bundle(row["history_apps"], row["history_durations_s"],
        row["history_mask"], row["opened_apps"], row["current_app"], row["timestamp"])
    print(json.dumps(bundle, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
