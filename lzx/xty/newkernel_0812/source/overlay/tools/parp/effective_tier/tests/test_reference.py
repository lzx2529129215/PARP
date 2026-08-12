#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0
from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from tools.parp.effective_tier.reference import (
    MAX_TIER,
    MISSING_VALUE,
    S32_MAX,
    TIER_SCALE,
    U32_MAX,
    U64_MAX,
    ModelError,
    QuantModel,
    TierAction,
    add_base_pages,
    bin_index,
    decide_effective_tier,
    deterministic_vectors,
    effective_tier_q8,
    load_model,
    score_model,
    score_to_delta_q8,
    u32_elapsed,
)


def one_feature_model(**overrides: object) -> QuantModel:
    values = {
        "model_name": "GLOBAL_REUSE_MODEL",
        "model_version": 1,
        "feature_schema_version": 1,
        "feature_names": ("access_age_ms",),
        "bias": 0,
        "cold_threshold": -48,
        "hot_threshold_1": 48,
        "hot_threshold_2": 96,
        "hot_threshold_3": 144,
        "max_upgrade_tiers": 2,
        "max_downgrade_tiers": 1,
        "bin_edges": ((10, 100),),
        "weights": ((-48, 48, 96),),
    }
    values.update(overrides)
    return QuantModel(**values)


class ScoreReferenceTests(unittest.TestCase):
    def test_default_model_is_one_global_model_without_routing(self):
        model = load_model()
        self.assertEqual(model.model_name, "GLOBAL_REUSE_MODEL")
        self.assertFalse(hasattr(model, "app_id"))
        self.assertTrue(all("frontier" not in name.lower()
                            for name in model.feature_names))

    def test_edges_belong_to_the_lower_bin(self):
        edges = (10, 100, 500)
        expected = {
            9: 0,
            10: 0,
            11: 1,
            99: 1,
            100: 1,
            101: 2,
            500: 2,
            501: 3,
        }
        for value, index in expected.items():
            with self.subTest(value=value):
                self.assertEqual(bin_index(value, edges), index)

    def test_score_is_bias_plus_selected_integer_weights(self):
        model = one_feature_model(bias=7)
        self.assertEqual(score_model(model, [0]).score, -41)
        self.assertEqual(score_model(model, [50]).score, 55)
        self.assertEqual(score_model(model, [101]).score, 103)

    def test_extreme_feature_values_select_bounded_end_bins(self):
        model = one_feature_model()
        self.assertEqual(score_model(model, [-(1 << 62)]).score, -48)
        self.assertEqual(score_model(model, [1 << 62]).score, 96)

    def test_missing_feature_invalidates_score(self):
        result = score_model(one_feature_model(), [MISSING_VALUE])
        self.assertFalse(result.valid)
        self.assertEqual(result.reason, "missing_feature")

    def test_version_schema_and_feature_count_mismatch_are_invalid(self):
        model = one_feature_model()
        self.assertEqual(score_model(model, [1], expected_model_version=2).reason,
                         "model_version")
        self.assertEqual(score_model(model, [1], expected_schema_version=2).reason,
                         "feature_schema")
        self.assertEqual(score_model(model, []).reason, "feature_count")

    def test_unsafe_runtime_model_score_range_is_invalid(self):
        model = one_feature_model(bias=S32_MAX,
                                  weights=((1, 1, 1),))
        result = score_model(model, [0])
        self.assertFalse(result.valid)
        self.assertEqual(result.reason, "model_invalid")

    def test_loader_rejects_a_model_whose_score_range_can_overflow(self):
        source = Path(__file__).parents[1] / "default_model.json"
        payload = json.loads(source.read_text(encoding="utf-8"))
        payload["model"]["bias"] = S32_MAX
        payload["model"]["weights"][0] = [1] * len(
            payload["model"]["weights"][0])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(ModelError):
                load_model(path)

    def test_loader_rejects_non_monotonic_edges_and_bad_thresholds(self):
        source = Path(__file__).parents[1] / "default_model.json"
        payload = json.loads(source.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.json"
            payload["model"]["bin_edges"][0][:2] = [10, 10]
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(ModelError):
                load_model(path)

    def test_loader_requires_checksum_and_hashes_declared_model_type(self):
        source = Path(__file__).parents[1] / "default_model.json"
        original = json.loads(source.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.json"
            payload = json.loads(json.dumps(original))
            del payload["embedded_parameter_checksum"]
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(ModelError):
                load_model(path)

            payload = json.loads(json.dumps(original))
            payload["model_type"] = "random_matched_baseline"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ModelError, "pairwise scorer"):
                load_model(path)

            payload = json.loads(json.dumps(original))
            payload["artifact_status"] = "TRAINED_OFFLINE"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ModelError, "fixture provenance"):
                load_model(path)

            payload = json.loads(source.read_text(encoding="utf-8"))
            payload["model"]["hot_threshold_1"] = payload["model"][
                "cold_threshold"]
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(ModelError):
                load_model(path)


class DeltaMappingTests(unittest.TestCase):
    def test_all_threshold_boundaries_have_explicit_inclusive_semantics(self):
        model = one_feature_model()
        expected = {
            -49: -TIER_SCALE,
            -48: -TIER_SCALE,
            -47: 0,
            47: 0,
            48: TIER_SCALE,
            95: TIER_SCALE,
            96: 2 * TIER_SCALE,
            97: 2 * TIER_SCALE,
            143: 2 * TIER_SCALE,
        }
        for score, delta in expected.items():
            with self.subTest(score=score):
                self.assertEqual(score_to_delta_q8(score, model), delta)

    def test_strong_upgrade_cap_supports_one_two_and_three_tier_ablations(self):
        base = one_feature_model()
        for cap in (1, 2, 3):
            with self.subTest(cap=cap):
                model = replace(base, max_upgrade_tiers=cap)
                self.assertEqual(score_to_delta_q8(48, model), TIER_SCALE)
                self.assertEqual(score_to_delta_q8(96, model),
                                 min(cap, 2) * TIER_SCALE)
                self.assertEqual(score_to_delta_q8(144, model),
                                 (3 if cap == 3 else min(cap, 2)) *
                                 TIER_SCALE)

    def test_mapping_uses_model_thresholds_instead_of_hardcoded_defaults(self):
        model = replace(one_feature_model(), cold_threshold=-7,
                        hot_threshold_1=11, hot_threshold_2=29,
                        hot_threshold_3=40)
        expected = {
            -7: -TIER_SCALE,
            -6: 0,
            10: 0,
            11: TIER_SCALE,
            28: TIER_SCALE,
            29: 2 * TIER_SCALE,
        }
        for score, delta in expected.items():
            with self.subTest(score=score):
                self.assertEqual(score_to_delta_q8(score, model), delta)

    def test_maximum_downgrade_is_one_tier(self):
        model = one_feature_model(max_downgrade_tiers=1)
        self.assertEqual(score_to_delta_q8(-10_000, model), -TIER_SCALE)


class EffectiveTierTests(unittest.TestCase):
    def test_q8_tier_addition_clamps_at_both_ends(self):
        self.assertEqual(effective_tier_q8(0, -TIER_SCALE), 0)
        self.assertEqual(effective_tier_q8(MAX_TIER, TIER_SCALE),
                         MAX_TIER * TIER_SCALE)
        self.assertEqual(effective_tier_q8(1, TIER_SCALE),
                         2 * TIER_SCALE)

    def test_q8_arithmetic_handles_extreme_deltas_without_wrapping(self):
        self.assertEqual(effective_tier_q8(MAX_TIER, S32_MAX),
                         MAX_TIER * TIER_SCALE)
        self.assertEqual(effective_tier_q8(0, -(1 << 31)), 0)

    def test_native_tier_zero_cannot_be_downgraded_below_zero(self):
        decision = decide_effective_tier(one_feature_model(), [0], 0, 0)
        self.assertEqual(decision.delta_tier_q8, -TIER_SCALE)
        self.assertEqual(decision.effective_tier_q8, 0)
        self.assertFalse(decision.native_protect)
        self.assertFalse(decision.effective_protect)
        self.assertEqual(decision.action, TierAction.KEEP_RECLAIM)

    def test_equality_is_not_protection_for_native_or_effective_tier(self):
        neutral = one_feature_model(weights=((0, 0, 0),))
        for tier in range(MAX_TIER + 1):
            with self.subTest(tier=tier):
                decision = decide_effective_tier(neutral, [0], tier, tier)
                self.assertFalse(decision.native_protect)
                self.assertFalse(decision.effective_protect)

    def test_tier_idx_upper_boundary_cannot_be_exceeded_after_clamp(self):
        decision = decide_effective_tier(one_feature_model(), [101],
                                         MAX_TIER, MAX_TIER)
        self.assertEqual(decision.effective_tier_q8,
                         MAX_TIER * TIER_SCALE)
        self.assertFalse(decision.effective_protect)
        self.assertEqual(decision.action, TierAction.KEEP_RECLAIM)

    def test_positive_delta_upgrades_a_native_reclaim_decision(self):
        decision = decide_effective_tier(one_feature_model(), [50], 0, 0)
        self.assertFalse(decision.native_protect)
        self.assertTrue(decision.effective_protect)
        self.assertEqual(decision.action, TierAction.PREDICTIVE_UPGRADE)

    def test_negative_delta_downgrades_only_the_boundary_protection(self):
        decision = decide_effective_tier(one_feature_model(), [0], 2, 1)
        self.assertTrue(decision.native_protect)
        self.assertFalse(decision.effective_protect)
        self.assertEqual(decision.action, TierAction.PREDICTIVE_DOWNGRADE)

    def test_one_tier_negative_delta_cannot_cancel_strong_native_protection(self):
        decision = decide_effective_tier(one_feature_model(), [0], 3, 1)
        self.assertTrue(decision.native_protect)
        self.assertTrue(decision.effective_protect)
        self.assertEqual(decision.action, TierAction.KEEP_PROTECT)

    def test_invalid_model_forces_delta_zero_and_strict_native_tier_result(self):
        model = one_feature_model()
        for native_tier in range(MAX_TIER + 1):
            for tier_idx in range(MAX_TIER + 1):
                with self.subTest(native=native_tier, tier_idx=tier_idx):
                    decision = decide_effective_tier(
                        model, [50], native_tier, tier_idx,
                        expected_model_version=2,
                    )
                    self.assertFalse(decision.model_valid)
                    self.assertEqual(decision.delta_tier_q8, 0)
                    self.assertEqual(decision.effective_protect,
                                     decision.native_protect)

    def test_unsafe_model_shape_also_forces_delta_zero(self):
        unsafe = replace(one_feature_model(), max_downgrade_tiers=2)
        decision = decide_effective_tier(unsafe, [0], 1, 0)
        self.assertFalse(decision.model_valid)
        self.assertEqual(decision.reason, "model_invalid")
        self.assertEqual(decision.delta_tier_q8, 0)
        self.assertEqual(decision.effective_protect,
                         decision.native_protect)

    def test_special_native_protection_cannot_be_cancelled(self):
        decision = decide_effective_tier(
            one_feature_model(), [0], 1, 0,
            special_native_protect=True,
        )
        self.assertFalse(decision.effective_protect)
        self.assertTrue(decision.actual_protect)
        self.assertEqual(decision.action,
                         TierAction.SPECIAL_NATIVE_PROTECT)

    def test_invalid_tier_inputs_are_rejected(self):
        model = one_feature_model()
        for native_tier, tier_idx in ((-1, 0), (MAX_TIER + 1, 0),
                                      (0, -1), (0, MAX_TIER + 1)):
            with self.subTest(native=native_tier, tier_idx=tier_idx):
                with self.assertRaises(ValueError):
                    decide_effective_tier(model, [0], native_tier, tier_idx)

    def test_large_folio_is_accounted_in_base_pages_without_changing_score(self):
        base = decide_effective_tier(one_feature_model(), [50], 0, 0,
                                     folio_nr_pages=1)
        large = decide_effective_tier(one_feature_model(), [50], 0, 0,
                                      folio_nr_pages=512)
        self.assertEqual(large.folio_nr_pages, 512)
        self.assertEqual(large.action, base.action)
        self.assertEqual(add_base_pages(1024, large.folio_nr_pages), 1536)

    def test_base_page_accounting_detects_u64_overflow(self):
        self.assertEqual(add_base_pages(U64_MAX - 511, 511), U64_MAX)
        with self.assertRaises(OverflowError):
            add_base_pages(U64_MAX, 1)
        with self.assertRaises(ValueError):
            add_base_pages(0, 0)


class TimeTests(unittest.TestCase):
    def test_u32_elapsed_handles_normal_and_wrapped_timestamps(self):
        self.assertEqual(u32_elapsed(110, 100), 10)
        self.assertEqual(u32_elapsed(0x10, 0xFFFFFFF0), 0x20)
        self.assertEqual(u32_elapsed(4, U32_MAX), 5)

    def test_u32_elapsed_masks_inputs_like_unsigned_kernel_arithmetic(self):
        self.assertEqual(u32_elapsed(U32_MAX + 2, U32_MAX), 2)
        self.assertEqual(u32_elapsed(0, -1), 1)


class COracleParityTests(unittest.TestCase):
    def test_python_and_standalone_c_oracle_match(self):
        source = Path(__file__).parents[1] / "cscore.c"
        model = load_model()
        requests = []
        expected = []

        for vector in deterministic_vectors(model):
            result = score_model(model, vector)
            requests.append("S " + " ".join(str(value) for value in vector))
            expected.append(f"S {int(result.valid)} {result.score}")
        missing = [0] * model.nr_features
        missing[2] = MISSING_VALUE
        requests.append("S " + " ".join(str(value) for value in missing))
        expected.append("S 0 0")

        boundary_scores = (-49, -48, -47, 47, 48, 95, 96, 97,
                           143, 144, 145)
        for cap in (1, 2, 3):
            capped = replace(model, max_upgrade_tiers=cap)
            for score in boundary_scores:
                requests.append(f"D {score} {cap}")
                expected.append(
                    f"D 1 {score_to_delta_q8(score, capped)}")
        requests.extend((f"D {S32_MAX + 1} 2", "D 0 0"))
        expected.extend(("D 0 0", "D 0 0"))

        deltas = (-(1 << 31), -3 * TIER_SCALE, -TIER_SCALE,
                  -1, 0, 1, TIER_SCALE, 3 * TIER_SCALE, S32_MAX)
        for native_tier in range(MAX_TIER + 1):
            for tier_idx in range(MAX_TIER + 1):
                for delta in deltas:
                    effective = effective_tier_q8(native_tier, delta)
                    requests.append(f"T {native_tier} {tier_idx} {delta}")
                    expected.append(
                        f"T 1 {effective} {int(native_tier > tier_idx)} "
                        f"{int(effective > tier_idx * TIER_SCALE)}")
        requests.extend(("T -1 0 0", f"T 0 {MAX_TIER + 1} 0"))
        expected.extend(("T 0 0 0 0", "T 0 0 0 0"))

        time_vectors = ((110, 100), (0x10, 0xFFFFFFF0),
                        (4, U32_MAX), (0, -1))
        for now, then in time_vectors:
            requests.append(f"U {now} {then}")
            expected.append(f"U {u32_elapsed(now, then)}")

        page_vectors = ((0, 512), (1024, 512),
                        (U64_MAX - 511, 511))
        for total, pages in page_vectors:
            requests.append(f"B {total} {pages}")
            expected.append(f"B 1 {add_base_pages(total, pages)}")
        requests.extend(("B 0 0", f"B {U64_MAX} 1"))
        expected.extend(("B 0 0", "B 0 0"))

        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / "cscore"
            subprocess.run([
                "cc", "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror",
                str(source), "-o", str(binary),
            ], check=True)
            completed = subprocess.run(
                [str(binary)], input="\n".join(requests) + "\n", text=True,
                check=True, capture_output=True,
            )
        actual = completed.stdout.splitlines()
        self.assertEqual(actual, expected)
        self.assertGreaterEqual(len(actual), 250)


if __name__ == "__main__":
    unittest.main()
