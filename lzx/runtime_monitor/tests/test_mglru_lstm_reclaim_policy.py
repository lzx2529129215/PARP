from __future__ import annotations

import json
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from runtime_monitor.core.mglru_lstm_reclaim_policy import (
    ReclaimPolicyConfig,
    calculate_scan_budget,
    choose_scan_factor,
)


DEFAULT_CONFIG = {
    "enabled": True,
    "mode": "observe",
    "foreground_scan_factor": 0.70,
    "high_probability_threshold": 0.90,
    "neutral_probability_threshold": 0.50,
    "low_probability_threshold": 0.20,
    "high_probability_scan_factor": 0.75,
    "neutral_probability_scan_factor": 1.00,
    "low_probability_scan_factor": 1.10,
    "very_low_probability_scan_factor": 1.25,
    "minimum_scan_factor": 0.70,
    "maximum_scan_factor": 1.30,
    "missing_prediction_probability": 0.30,
    "unknown_app_scan_factor": 1.00,
    "expired_prediction_scan_factor": 1.00,
    "prediction_ttl_ms": 180000,
    "minimum_scan_pages": 1,
    "maximum_extra_scan_pages_per_cycle": 4096,
    "markov_min_app_probability": 0.0,
}


def load_config(overrides: dict[str, object] | None = None) -> ReclaimPolicyConfig:
    data = dict(DEFAULT_CONFIG)
    data.update(overrides or {})
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "policy.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        return ReclaimPolicyConfig.load(path)


class MGLRULSTMReclaimPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()

    def test_probability_boundaries(self) -> None:
        cases = [
            (0.95, "high", 750),
            (0.90, "high", 750),
            (0.89, "neutral", 1000),
            (0.50, "neutral", 1000),
            (0.49, "low", 1100),
            (0.20, "low", 1100),
            (0.19, "very_low", 1250),
        ]
        for probability, bucket, factor in cases:
            with self.subTest(probability=probability):
                self.assertEqual(
                    choose_scan_factor(self.config, probability),
                    (bucket, factor),
                )

    def test_special_buckets(self) -> None:
        self.assertEqual(
            choose_scan_factor(self.config, 0.99, foreground=True),
            ("foreground", 700),
        )
        self.assertEqual(
            choose_scan_factor(self.config, None),
            ("missing", 1100),
        )
        self.assertEqual(
            choose_scan_factor(self.config, 0.99, expired=True),
            ("expired", 1000),
        )
        self.assertEqual(
            choose_scan_factor(self.config, 0.99, unknown_app=True),
            ("unknown", 1000),
        )

    def test_probability_clamp(self) -> None:
        self.assertEqual(choose_scan_factor(self.config, 2.0), ("high", 750))
        self.assertEqual(choose_scan_factor(self.config, -1.0), ("very_low", 1250))

    def test_invalid_threshold_ordering(self) -> None:
        with self.assertRaises(ValueError):
            load_config({"high_probability_threshold": 0.4, "neutral_probability_threshold": 0.5})

    def test_invalid_factor_bounds(self) -> None:
        with self.assertRaises(ValueError):
            load_config({"minimum_scan_factor": 1.4, "maximum_scan_factor": 1.3})

    def test_observe_does_not_change_actual(self) -> None:
        decision = calculate_scan_budget(1000, self.config, 0.95)
        self.assertEqual(decision.proposed, 750)
        self.assertEqual(decision.actual, 1000)

    def test_apply_uses_proposed(self) -> None:
        config = replace(self.config, mode="apply")
        decision = calculate_scan_budget(1000, config, 0.95)
        self.assertEqual(decision.actual, 750)

    def test_maximum_extra_pages_cap(self) -> None:
        config = replace(
            self.config,
            mode="apply",
            maximum_extra_pages=100,
        )
        decision = calculate_scan_budget(1000, config, 0.10)
        self.assertEqual(decision.proposed, 1100)
        self.assertEqual(decision.actual, 1100)

    def test_original_zero_stays_zero(self) -> None:
        config = replace(self.config, mode="apply")
        decision = calculate_scan_budget(0, config, 0.10)
        self.assertEqual((decision.proposed, decision.actual), (0, 0))


if __name__ == "__main__":
    unittest.main()
