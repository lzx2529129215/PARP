#!/usr/bin/env python3

from __future__ import annotations

import sys
import unittest
from pathlib import Path

TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))

from v41_core import (  # noqa: E402
    AppState,
    Prediction,
    Sample,
    compute_policy,
    evaluate_sample,
    largest_remainder_allocate,
    prediction_score_map,
    q15,
    summarize,
)


CONFIG = {
    "foreground_protection_weight": 4.0,
    "next_app_protection_weight": 3.0,
    "priority_protection_weight": 0.0,
    "metric_top_k": 3,
}


class V41CoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.sample = Sample("s1", "2026-08-05 09:00:00", 0, "WPS", 2, "腾讯QQ", 2500, 3500, 500)
        self.states = [
            AppState("s1", 0, "WPS", 100, True, True, 5000, 0),
            AppState("s1", 1, "飞书", 101, False, False, 0, 6000),
            AppState("s1", 2, "腾讯QQ", 102, True, False, 7000, 0),
        ]
        self.predictions = [
            Prediction("s1", 300000, 1, 2, "腾讯QQ", 0.8, 0.8, "softmax", 401, 30000),
            Prediction("s1", 300000, 2, 1, "飞书", 0.15, 0.15, "softmax", 401, 30000),
        ]

    def test_q15_clamps(self) -> None:
        self.assertEqual(q15(0.0), 0)
        self.assertEqual(q15(1.0), 32767)
        self.assertEqual(q15(2.0), 32767)

    def test_largest_remainder_preserves_total(self) -> None:
        allocation = largest_remainder_allocate(10, {1: 1.0, 2: 2.0, 3: 0.0})
        self.assertEqual(sum(allocation.values()), 10)
        self.assertGreater(allocation[2], allocation[1])
        self.assertEqual(allocation[3], 0)

    def test_current_foreground_is_not_next_candidate(self) -> None:
        predictions = self.predictions + [
            Prediction("s1", 300000, 3, 0, "WPS", 0.99, 0.99, "softmax", 401, 30000)
        ]
        scores = prediction_score_map(predictions, current_app_id=0)
        self.assertNotIn(0, scores)
        self.assertEqual(scores[2], 0.8)

    def test_sigmoid_scores_are_normalized(self) -> None:
        predictions = [
            Prediction("s1", 300000, 1, 1, "飞书", 0.8, 0.8, "sigmoid", 401, 30000),
            Prediction("s1", 300000, 2, 2, "腾讯QQ", 0.2, 0.2, "sigmoid", 401, 30000),
        ]
        scores = prediction_score_map(predictions, current_app_id=0)
        self.assertAlmostEqual(scores[1], 0.8)
        self.assertAlmostEqual(scores[2], 0.2)
        self.assertAlmostEqual(sum(scores.values()), 1.0)

    def test_lstm_increases_headroom_for_not_running_predicted_app(self) -> None:
        native = compute_policy(self.sample, self.states, self.predictions, CONFIG, mode="native")
        lstm = compute_policy(self.sample, self.states, self.predictions, CONFIG, mode="lstm")
        self.assertEqual(native.predicted_launch_pages, 0.0)
        self.assertGreater(lstm.predicted_launch_pages, 0.0)
        self.assertGreaterEqual(lstm.target_headroom_pages, native.target_headroom_pages)

    def test_lstm_reduces_next_app_budget_when_running(self) -> None:
        native = compute_policy(self.sample, self.states, self.predictions, CONFIG, mode="native")
        running_only_predictions = [self.predictions[0]]
        lstm = compute_policy(self.sample, self.states, running_only_predictions, CONFIG, mode="lstm")
        self.assertLessEqual(lstm.budgets[2], native.budgets[2])

    def test_evaluation_reports_prediction_and_effect(self) -> None:
        row = evaluate_sample(self.sample, self.states, self.predictions, CONFIG)
        self.assertEqual(row["hit_at_1"], 1)
        self.assertEqual(row["hit_at_k"], 1)
        self.assertNotEqual(row["actual_next_budget_reduction_pages"], 0)
        summary = summarize([row])
        self.assertEqual(summary["sample_count"], 1)
        self.assertEqual(summary["hit_at_1"], 1.0)


if __name__ == "__main__":
    unittest.main()
