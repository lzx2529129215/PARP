#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0
import json
from pathlib import Path
import unittest

from scan_budget_reference import compute_scan_budget


VECTORS = Path(__file__).parents[1] / "configs" / "scan_budget_vectors.json"


class ScanBudgetTest(unittest.TestCase):
    def test_shared_vectors(self):
        vectors = json.loads(VECTORS.read_text(encoding="utf-8"))
        for case in vectors["cases"]:
            with self.subTest(case=case["name"]):
                result = compute_scan_budget(**{
                    key: case[key] for key in
                    ("native", "score", "foreground", "pressure", "mode")
                })
                self.assertEqual(result["proposed"], case["proposed"])
                self.assertEqual(result["applied"], case["applied"])
                self.assertEqual(result["reason"], case["reason"])

    def test_probability_monotonicity(self):
        scans = [compute_scan_budget(1000, score, False, "NORMAL", "OBSERVE")
                 ["proposed"] for score in range(0, 32768, 257)]
        self.assertEqual(scans, sorted(scans, reverse=True))

    def test_pressure_and_scope_bypass(self):
        elevated = compute_scan_budget(1000, 30000, True, "ELEVATED", "APPLY")
        high = compute_scan_budget(1000, 30000, True, "HIGH", "APPLY")
        self.assertGreaterEqual(elevated["proposed"], 500)
        self.assertGreaterEqual(high["proposed"], 750)
        for scope in ("GLOBAL_KSWAPD", "GLOBAL_DIRECT", "UNKNOWN"):
            result = compute_scan_budget(1000, 30000, True, "NORMAL", "APPLY",
                                         scope=scope)
            self.assertEqual(result["applied"], 1000)

    def test_proactive_target_memcg_uses_the_target_budget(self):
        result = compute_scan_budget(
            1000, 18000, False, "NORMAL", "OBSERVE",
            scope="PROACTIVE_MEMCG")
        self.assertTrue(result["valid"])
        self.assertEqual(result["proposed"], 800)
        self.assertEqual(result["applied"], 1000)

    def test_rounding_bounds_and_small_native(self):
        self.assertEqual(compute_scan_budget(0, 0, False, "NORMAL", "APPLY")
                         ["proposed"], 0)
        result = compute_scan_budget(1, 32767, True, "NORMAL", "APPLY")
        self.assertEqual(result["proposed"], 1)
        huge = compute_scan_budget((1 << 64) - 1, 0, False, "NORMAL", "APPLY")
        self.assertLessEqual(huge["proposed"], (1 << 64) - 1)

    def test_invalid_gates_are_native(self):
        for gate in ("bind_valid", "prior_valid", "generation_valid",
                     "model_compatible", "circuit_ok"):
            kwargs = {gate: False}
            result = compute_scan_budget(1000, 30000, True, "NORMAL", "APPLY",
                                         **kwargs)
            self.assertEqual(result["applied"], 1000)


if __name__ == "__main__":
    unittest.main()
