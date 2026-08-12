#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0  #lzx
from __future__ import annotations

import unittest
from importlib import import_module

pressure_lzx = import_module("tools.parp.effective_tier.pressure-lzx")
PRESSURE_POLICY_PROVENANCE = pressure_lzx.PRESSURE_POLICY_PROVENANCE
PRESSURE_POLICY_VERSION = pressure_lzx.PRESSURE_POLICY_VERSION
PressureLevel = pressure_lzx.PressureLevel
counterfactual_deltas = pressure_lzx.counterfactual_deltas
pressure_policy_ablation = pressure_lzx.pressure_policy_ablation
pressure_level_from_local_signals = pressure_lzx.pressure_level_from_local_signals
validate_engineering_policy = pressure_lzx.validate_engineering_policy
from tools.parp.effective_tier.reference import TIER_SCALE


class PressurePolicyTests(unittest.TestCase):
    def test_local_classifier_matches_c_boundary_vectors(self):
        self.assertEqual(pressure_level_from_local_signals(12, False, 0, 0),
                         PressureLevel.LOW)
        self.assertEqual(pressure_level_from_local_signals(8, False, 0, 0),
                         PressureLevel.MEDIUM)
        self.assertEqual(pressure_level_from_local_signals(4, False, 0, 0),
                         PressureLevel.HIGH)
        self.assertEqual(pressure_level_from_local_signals(2, False, 0, 0),
                         PressureLevel.CRITICAL)
        self.assertEqual(pressure_level_from_local_signals(12, True, 0, 0),
                         PressureLevel.CRITICAL)

    def test_upgrades_become_no_stronger_as_pressure_rises(self):
        deltas = [counterfactual_deltas(
            TIER_SCALE, 0, 0, False, True, PressureLevel(level)
        ).pressure_aware_delta_q8 for level in range(4)]
        self.assertEqual(deltas, [256, 192, 64, 0])

    def test_low_pressure_downgrade_is_conservative_and_boundary_only(self):
        boundary = counterfactual_deltas(-TIER_SCALE, 2, 1, False, True,
                                         PressureLevel.LOW)
        self.assertEqual(boundary.fixed_delta_q8, -TIER_SCALE)
        self.assertEqual(boundary.pressure_aware_delta_q8, -128)
        self.assertFalse(boundary.fixed_effective_protect)
        self.assertTrue(boundary.pressure_aware_effective_protect)

        strong = counterfactual_deltas(-TIER_SCALE, 3, 1, False, True,
                                       PressureLevel.HIGH)
        self.assertEqual(strong.fixed_delta_q8, 0)
        self.assertTrue(strong.pressure_aware_effective_protect)

    def test_special_native_and_critical_remain_native(self):
        special = counterfactual_deltas(-TIER_SCALE, 2, 1, True, True,
                                        PressureLevel.MEDIUM)
        critical = counterfactual_deltas(2 * TIER_SCALE, 0, 0, False, True,
                                         PressureLevel.CRITICAL)
        self.assertEqual(special.fixed_delta_q8, 0)
        self.assertEqual(critical.binary_bypass_delta_q8, 0)
        self.assertEqual(critical.pressure_aware_delta_q8, 0)

    def test_policy_is_explicitly_unvalidated_for_apply(self):
        self.assertEqual(PRESSURE_POLICY_VERSION, 1)
        self.assertEqual(PRESSURE_POLICY_PROVENANCE,
                         "ENGINEERING_PRESSURE_POLICY_UNVALIDATED")
        validate_engineering_policy()

    def test_ablation_is_counterfactual_and_keeps_critical_native(self):
        rows = []
        for number, (level, raw_delta) in enumerate((
                (PressureLevel.LOW, TIER_SCALE),
                (PressureLevel.HIGH, TIER_SCALE),
                (PressureLevel.CRITICAL, TIER_SCALE),
                (PressureLevel.MEDIUM, -TIER_SCALE))):
            native_tier, tier_idx = (2, 1) if raw_delta < 0 else (0, 0)
            cf = counterfactual_deltas(raw_delta, native_tier, tier_idx,
                                       False, True, level)
            rows.append({
                "experiment_id": "e", "session_id": "s",
                "folio_cookie": str(number), "folio_lifetime_epoch": 1,
                "batch_id": "b", "folio_nr_pages": 1,
                "native_tier": native_tier, "native_tier_idx": tier_idx,
                "native_protect": native_tier > tier_idx,
                "special_native_protect": False, "model_valid": True,
                "predictive_delta_tier_q8": raw_delta,
                "fixed_delta_q8": cf.fixed_delta_q8,
                "binary_bypass_delta_q8": cf.binary_bypass_delta_q8,
                "pressure_aware_delta_q8": cf.pressure_aware_delta_q8,
                "fixed_effective_protect": cf.fixed_effective_protect,
                "pressure_aware_effective_protect":
                    cf.pressure_aware_effective_protect,
                "pressure_level_kernel": int(level),
                "pressure_policy_version": PRESSURE_POLICY_VERSION,
                "pressure_policy_provenance": PRESSURE_POLICY_PROVENANCE,
                "page_type": "anon", "app": "WPS",
                "labels": {"reuse_within_1s": level is PressureLevel.LOW},
                "features_valid": True,
                "features": {"time_since_last_real_access_ms": 50},
                "score_threshold_cold": -48,
                "score_threshold_hot_1": 48,
                "score_threshold_hot_2": 96,
            })
        report = pressure_policy_ablation(rows)
        critical = report["policies"]["PRESSURE_AWARE_BIDIRECTIONAL"]
        self.assertEqual(report["status"],
                         "PHASE_F_PRESSURE_COUNTERFACTUALS_REPLAYED")
        self.assertTrue(report["counterfactual_only"])
        self.assertEqual(critical["critical_native_fallback_rate"], 1.0)
        self.assertFalse(report["policies"]["RANDOM_RATE_MATCHED_SHADOW"]
                         ["rate_matched"]["exact_count_matched"])


if __name__ == "__main__":
    unittest.main()
