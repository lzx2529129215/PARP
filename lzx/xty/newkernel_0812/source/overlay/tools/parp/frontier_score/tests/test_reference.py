#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0
from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from tools.parp.frontier_score.reference import (
    MISSING_VALUE,
    ModelError,
    QuantModel,
    bin_index,
    deterministic_vectors,
    frontier_for_capacities,
    load_models,
    promotion_budget_pages,
    route_model,
    score_for_app,
    score_model,
)


class ReferenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.models = load_models()

    def test_models_validate_and_route(self):
        self.assertEqual(set(self.models), {0, 1, 2, 3})
        self.assertEqual(route_model(self.models, 1).model_name,
                         "WPS_REUSE_MODEL")
        self.assertEqual(route_model(self.models, 99).model_name,
                         "GENERIC_REUSE_MODEL")
        self.assertIsNone(route_model(self.models, 99, False))

    def test_bin_edges_belong_to_lower_bin(self):
        edges = [10, 100, 500]
        self.assertEqual(bin_index(9, edges), 0)
        self.assertEqual(bin_index(10, edges), 0)
        self.assertEqual(bin_index(11, edges), 1)
        self.assertEqual(bin_index(500, edges), 2)
        self.assertEqual(bin_index(501, edges), 3)

    def test_missing_feature_is_native(self):
        values = [1] * 8
        values[4] = MISSING_VALUE
        result = score_for_app(self.models, 1, values)
        self.assertFalse(result.valid)
        self.assertEqual(result.reason, "missing_feature")

    def test_version_and_schema_mismatch_are_native(self):
        values = [1] * 8
        self.assertEqual(score_for_app(self.models, 1, values, 2).reason,
                         "model_version")
        self.assertEqual(score_for_app(self.models, 1, values,
                                       schema_version=2).reason,
                         "feature_schema")

    def test_threshold_is_inclusive(self):
        model = QuantModel(9, "TEST", 1, 1, 7, ((0,),), ((7, 8),))
        result = score_model(model, [0], 1, 1)
        self.assertTrue(result.valid)
        self.assertTrue(result.would_promote)
        self.assertEqual(result.score, 7)

    def test_feature_count_mismatch_is_native(self):
        result = score_for_app(self.models, 2, [0] * 7)
        self.assertFalse(result.valid)
        self.assertEqual(result.reason, "feature_count")

    def test_generic_and_native_fallback(self):
        values = [50, 200, 128, 300, 1, 2, 700, 20000]
        generic = score_for_app(self.models, 99, values)
        native = score_for_app(self.models, 99, values,
                               allow_generic=False)
        self.assertTrue(generic.valid)
        self.assertEqual(generic.model_name, "GENERIC_REUSE_MODEL")
        self.assertEqual(native.reason, "no_model")

    def test_extreme_feature_values_are_bounded(self):
        for value in (-(1 << 62), 1 << 62):
            result = score_for_app(self.models, 3, [value] * 8)
            self.assertTrue(result.valid)
            self.assertGreaterEqual(result.score, -(1 << 31))
            self.assertLessEqual(result.score, (1 << 31) - 1)

    def test_budget_uses_base_pages(self):
        self.assertEqual(promotion_budget_pages(64, 32, 128, 48), 32)
        self.assertEqual(promotion_budget_pages(511, 511, 511, 511), 511)
        with self.assertRaises(ValueError):
            promotion_budget_pages(-1, 1, 1, 1)

    def test_frontier_is_first_capacity_covering_demand(self):
        self.assertEqual(frontier_for_capacities([100, 200, 300], 220,
                                                 32768), (1, 200, 80))
        self.assertEqual(frontier_for_capacities([100, 200], 100, 16384),
                         (1, 100, 50))
        self.assertIsNone(frontier_for_capacities([10], 20, 32768))
        self.assertIsNone(frontier_for_capacities([100], 10, 0))

    def test_invalid_model_shapes_rejected(self):
        source = Path(__file__).parents[1] / "default_models.json"
        payload = json.loads(source.read_text(encoding="utf-8"))
        payload["models"][0]["weights"][0] = [1]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(ModelError):
                load_models(path)

    def test_s16_weight_overflow_rejected(self):
        source = Path(__file__).parents[1] / "default_models.json"
        payload = json.loads(source.read_text(encoding="utf-8"))
        payload["models"][0]["weights"][0][0] = 1 << 15
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "overflow.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(ModelError):
                load_models(path)

    def test_python_c_scores_match(self):
        source = Path(__file__).parents[1] / "cscore.c"
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / "cscore"
            subprocess.run([
                "cc", "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror",
                str(source), "-o", str(binary),
            ], check=True)
            vectors = list(deterministic_vectors(self.models))
            request = "".join(
                f"{app_id} {' '.join(str(value) for value in values)}\n"
                for app_id, values in vectors
            )
            completed = subprocess.run([str(binary)], input=request, text=True,
                                       check=True, capture_output=True)
            actual = [tuple(int(value) for value in line.split())
                      for line in completed.stdout.splitlines()]
            expected = []
            for app_id, values in vectors:
                result = score_for_app(self.models, app_id, values)
                expected.append((result.score, int(result.would_promote)))
            self.assertEqual(actual, expected)
            self.assertGreaterEqual(len(actual), 240)


if __name__ == "__main__":
    unittest.main()
# SPDX-License-Identifier: GPL-2.0
