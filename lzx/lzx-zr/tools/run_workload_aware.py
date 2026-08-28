from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from runtime_monitor.detector.state_machine import StateMachine
from runtime_monitor.features.engine import Observation, extract_features
from runtime_monitor.output.snapshot import make_snapshot, write_snapshot
from predictor.workload_predictor import WorkloadPredictor
from kernel.adapters.parp_snapshot_adapter import write_hint


def load_observations(path: Path) -> list[Observation]:
    rows: list[Observation] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row: dict[str, Any] = json.loads(line)
        rows.append(Observation(
            scope_type=str(row.get("scope_type", "cgroup")),
            scope_id=str(row.get("scope_id", "")),
            window_start_ns=int(row.get("window_start_ns", 0)),
            window_end_ns=int(row.get("window_end_ns", 0)),
            timestamp_ns=int(row.get("timestamp_ns", 0)),
            sampling_interval_ms=int(row.get("sampling_interval_ms", 1000)),
            region_ids=tuple(str(value) for value in row.get("region_ids", [])),
            region_accesses=tuple(float(value) for value in row.get("region_accesses", [])),
            region_timestamps_ns=tuple(int(value) for value in row.get("region_timestamps_ns", [])),
            counters={str(key): float(value) for key, value in row.get("counters", {}).items()},
        ))
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description="Workload-Aware observe/shadow prototype")
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--mode", choices=("OBSERVE", "SHADOW"), default="OBSERVE")
    parser.add_argument("--method", choices=("rule_trend", "second_order_markov"), default="rule_trend")
    args = parser.parse_args()
    machine = StateMachine()
    predictor = WorkloadPredictor()
    history = []
    args.output_dir.mkdir(parents=True, exist_ok=True)
    with (args.output_dir / "features.jsonl").open("w", encoding="utf-8") as feature_file, (args.output_dir / "predictions.jsonl").open("w", encoding="utf-8") as prediction_file:
        for observation in load_observations(args.input):
            features = extract_features(observation)
            state = machine.update(features)
            if history:
                predictor.observe_transition(history[-1], state)
            history.append(state)
            prediction = predictor.predict_markov(history, state) if args.method == "second_order_markov" else predictor.predict_rule_trend(state)
            snapshot = make_snapshot(prediction, features, mode=args.mode, native_fallback=state.confidence_q15 == 0)
            json.dump({**features.__dict__, "state": state.__dict__}, feature_file, ensure_ascii=False)
            feature_file.write("\n")
            json.dump(snapshot, prediction_file, ensure_ascii=False)
            prediction_file.write("\n")
    last_line = next(reversed((args.output_dir / "predictions.jsonl").read_text(encoding="utf-8").splitlines()), "")
    if last_line:
        snapshot = json.loads(last_line)
        write_snapshot(args.output_dir / "prediction_snapshot.json", snapshot)
        write_hint(args.output_dir / "parp_shadow_hint.json", snapshot)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
