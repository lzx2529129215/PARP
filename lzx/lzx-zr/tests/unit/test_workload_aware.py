from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from runtime_monitor.detector.state_machine import StateMachine, classify
from runtime_monitor.features.engine import Observation, extract_features, q15
from runtime_monitor.output.snapshot import make_snapshot
from predictor.workload_predictor import WorkloadPredictor
from kernel.adapters.parp_snapshot_adapter import snapshot_is_usable, to_parp_shadow_hint


class WorkloadAwareTests(unittest.TestCase):
    def test_q15_is_bounded(self) -> None:
        self.assertEqual(q15(-1), 0)
        self.assertEqual(q15(2), 32767)

    def test_missing_region_order_is_unknown(self) -> None:
        features = extract_features(Observation("cgroup", "1", 0, 1, 1, 1000))
        state = classify(features)
        self.assertEqual(state.access_order, "UNKNOWN")
        self.assertEqual(state.dominant, "UNKNOWN")
        self.assertEqual(state.confidence_q15, 0)

    def test_state_machine_requires_dwell(self) -> None:
        observation = Observation("region", "1", 0, 1, 1, 1000, ("a", "b"), (1, 1))
        machine = StateMachine(min_dwell_windows=2)
        state1 = machine.update(extract_features(observation))
        state2 = machine.update(extract_features(observation))
        self.assertEqual(state1.dominant, state2.dominant)
        self.assertFalse(state1.state_changed)

    def test_snapshot_is_shadow_only(self) -> None:
        features = extract_features(Observation("cgroup", "1", 1_000_000_000, 2_000_000_000, 2_000_000_000, 1000, ("r1", "r2"), (1, 1)))
        state = StateMachine().update(features)
        prediction = WorkloadPredictor().predict_rule_trend(state)
        snapshot = make_snapshot(prediction, features, mode="SHADOW", native_fallback=True)
        self.assertEqual(snapshot["mode"], "SHADOW")
        self.assertTrue(snapshot["native_fallback"])
        self.assertEqual(snapshot["prediction_seq"], 1)

    def test_stale_or_unknown_snapshot_falls_back_to_native(self) -> None:
        features = extract_features(Observation("cgroup", "1", 1_000_000_000, 2_000_000_000, 2_000_000_000, 1000, ("r1", "r2"), (1, 1)))
        state = StateMachine().update(features)
        prediction = WorkloadPredictor().predict_rule_trend(state)
        snapshot = make_snapshot(prediction, features, mode="SHADOW")
        self.assertFalse(snapshot_is_usable(snapshot, now_ns=8_000_000_000))
        self.assertEqual(to_parp_shadow_hint(snapshot)["mode"], "NATIVE")


if __name__ == "__main__":
    unittest.main()
