#!/usr/bin/env python3
"""File-only online dry-run output; intentionally has no kernel transport."""

import argparse
import json
from pathlib import Path
import time


def probability_to_q15(value):
    value = min(1.0, max(0.0, float(value)))
    return int(round(value * 32767))


class DryRunPredictor:
    def __init__(self, generation_path: Path, known_files=None):
        self.generation_path = Path(generation_path)
        self.known_files = set(known_files or ())

    def next_generation(self):
        generation = 0
        if self.generation_path.exists():
            generation = int(json.loads(
                self.generation_path.read_text()).get("generation", 0))
        generation += 1
        self.generation_path.parent.mkdir(parents=True, exist_ok=True)
        self.generation_path.write_text(json.dumps({"generation": generation}) + "\n")
        return generation

    def file_prediction(self, file_id, probabilities):
        if file_id not in self.known_files:
            return {"status": "UNKNOWN"}
        return {"status": "KNOWN", "probabilities": {
            str(horizon): probability_to_q15(value)
            for horizon, value in probabilities.items()}}

    def prediction(self, run_id, app_id, domain_id, segments,
                   anon_prediction, ttl_ns=60_000_000_000):
        return {
            "schema_version": 1, "run_id": run_id, "app_id": app_id,
            "domain_id": domain_id, "model_type": "page_state_markov",
            "model_version": 1,
            "prediction_generation": self.next_generation(),
            "generated_ns": time.time_ns(), "ttl_ns": ttl_ns,
            "file_segments": list(segments),
            "anon_prediction": dict(anon_prediction),
            "kernel_write": False,
        }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--generation-state", type=Path, required=True)
    parser.add_argument("--no-kernel-write", action="store_true", default=True)
    args = parser.parse_args()
    if not args.no_kernel_write:
        parser.error("Phase2.7 supports only --no-kernel-write")
    payload = json.loads(args.input.read_text())
    predictor = DryRunPredictor(args.generation_state,
                                payload.get("known_files", []))
    result = predictor.prediction(
        payload["run_id"], payload["app_id"], payload["domain_id"],
        payload.get("file_segments", []), payload.get("anon_prediction", {}),
        payload.get("ttl_ns", 60_000_000_000))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("a") as stream:
        stream.write(json.dumps(result, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
