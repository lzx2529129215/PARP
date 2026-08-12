#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0
from __future__ import annotations

import inspect
import json
import re
import unittest
from dataclasses import replace
from pathlib import Path

from tools.parp.effective_tier.contracts import BASE_FEATURES
from tools.parp.effective_tier.ranking import (
    RANK_ABLATIONS,
    RANKING_HORIZON_NS,
    SUPPORTED_MAX_PAIRS_PER_GROUP,
    S32_MAX,
    SCORE_SEMANTICS,
    FloatRankModel,
    PairSample,
    QuantizedRankModel,
    RankingCandidate,
    RankingConfig,
    RankingError,
    build_pair_dataset,
    candidate_index,
    checked_s32_add,
    concordance_index,
    evaluate_ranker,
    fit_pairwise_ranker,
    fixed_native_tier_stratification,
    make_model_document,
    ndcg_at_k,
    pairwise_accuracy,
    quantize_ranker,
    quantized_ordering_consistency,
    rank_score_to_delta_q8,
    score_bucket_monotonicity,
    score_all,
    scorer_parameter_checksum,
    scorer_parameter_projection,
    select_validation_thresholds,
    spearman_rank_correlation,
    validate_model_document,
    validate_session_splits,
)


MS = 1_000_000


def candidate(
        name: str, *, delay_ms: int | None = 100,
        observed: bool = True, session_censored: bool = False,
        experiment: str = "exp", session: str = "session",
        split: str = "train", app: str = "WPS", page_type: str = "file",
        batch: str | None = "batch", epoch: str | None = "epoch",
        time_ms: int = 1000, recency_ms: int = 100,
        native_tier: int = 0, tier_idx: int = 0,
        cookie: str | None = None, lifetime: int = 1, pages: int = 1,
) -> RankingCandidate:
    features = {
        "time_since_last_real_access_ms": recency_ms,
        "previous_real_access_interval_ms": recency_ms,
        "reuse_interval_ema_ms": recency_ms,
        "consecutive_reclaim_candidate_count": 1,
        "time_in_current_generation_ms": 100,
        "access_ema_q8": 64,
    }
    return RankingCandidate(
        experiment_id=experiment,
        session_id=session,
        candidate_id=name,
        folio_cookie=cookie or name,
        folio_lifetime_epoch=lifetime,
        split=split,
        app=app,
        page_type=page_type,
        candidate_time_ns=time_ms * MS,
        batch_id=batch,
        reclaim_epoch=epoch,
        features=features,
        native_tier=native_tier,
        native_tier_idx=tier_idx,
        special_native_protect=False,
        next_reuse_delay_ns=(delay_ms * MS if observed and
                             delay_ms is not None else None),
        observed_within_horizon=observed,
        censored_by_session_end=session_censored,
        folio_nr_pages=pages,
        horizon_ns=RANKING_HORIZON_NS,
        recorded_tie_margin_ns=10 * MS,
    )


def learned_fixture():
    rows = []
    for index in range(12):
        rows.extend((
            candidate("early-%d" % index, delay_ms=10,
                      recency_ms=5, batch="b-%d" % index,
                      time_ms=1000 + index),
            candidate("late-%d" % index, delay_ms=2000,
                      recency_ms=5000, batch="b-%d" % index,
                      time_ms=1000 + index),
        ))
    pairs, manifest = build_pair_dataset(rows)
    model = fit_pairwise_ranker(
        rows, pairs, feature_names=("time_since_last_real_access_ms",),
        epochs=250)
    quantized = quantize_ranker(model)
    return rows, pairs, manifest, model, quantized


class RankingPipelineTests(unittest.TestCase):
    # 19A.1
    def test_session_and_folio_lifetime_split_leakage_is_rejected(self):
        with self.assertRaisesRegex(RankingError, "session split leakage"):
            validate_session_splits([
                candidate("a", split="train"),
                candidate("b", split="test", cookie="b"),
            ])
        with self.assertRaisesRegex(RankingError, "folio lifetime"):
            validate_session_splits([
                candidate("a", session="s1", split="train",
                          cookie="shared", lifetime=9),
                candidate("b", session="s2", split="test",
                          cookie="shared", lifetime=9),
            ])

    # 19A.2
    def test_pairs_use_primary_batch_then_bounded_epoch_fallback_context(self):
        rows = [
            candidate("p1", delay_ms=10, batch="p", page_type="file"),
            candidate("p2", delay_ms=100, batch="p", page_type="file"),
            candidate("f1", delay_ms=10, batch="one", epoch="e",
                      time_ms=2000, page_type="anon"),
            candidate("f2", delay_ms=100, batch="two", epoch="e",
                      time_ms=2050, page_type="anon"),
            candidate("far", delay_ms=100, batch="three", epoch="e",
                      time_ms=2300, page_type="anon"),
        ]
        pairs, _manifest = build_pair_dataset(rows)
        lookup = candidate_index(rows)
        self.assertEqual({pair.group_level for pair in pairs},
                         {"primary_batch", "fallback_epoch_window"})
        for pair in pairs:
            left, right = lookup[pair.left_key], lookup[pair.right_key]
            self.assertEqual(left.session_key, right.session_key)
            self.assertEqual(left.page_type, right.page_type)
            if pair.group_level == "primary_batch":
                self.assertEqual(left.batch_id, right.batch_id)
            else:
                self.assertEqual(left.reclaim_epoch, right.reclaim_epoch)
                self.assertLess(abs(left.candidate_time_ns -
                                    right.candidate_time_ns), 100 * MS)

    # 19A.3
    def test_pair_sampling_is_bounded_to_64_without_all_pairs(self):
        rows = [candidate("c-%d" % index, delay_ms=index + 1,
                          recency_ms=index + 1)
                for index in range(200)]
        pairs, manifest = build_pair_dataset(
            rows, RankingConfig(tie_margin_ns=0))
        self.assertEqual(len(pairs), 64)
        self.assertGreater(manifest["available_pair_count"], len(pairs))
        self.assertFalse(manifest["all_pairs_materialized"])
        self.assertEqual(manifest["max_pairs_per_group"], 64)
        self.assertEqual(manifest["default_max_pairs_per_group"], 64)
        self.assertEqual(manifest["supported_pair_cap_ablations"],
                         [32, 64, 128])

    def test_pair_cap_128_ablation_is_supported_but_not_the_default(self):
        rows = [candidate("c-%d" % index, delay_ms=index + 1,
                          recency_ms=index + 1)
                for index in range(200)]
        pairs, manifest = build_pair_dataset(
            rows, RankingConfig(tie_margin_ns=0,
                                max_pairs_per_group=128))
        self.assertEqual(SUPPORTED_MAX_PAIRS_PER_GROUP, (32, 64, 128))
        self.assertEqual(len(pairs), 128)
        self.assertEqual(manifest["max_pairs_per_group"], 128)
        with self.assertRaisesRegex(RankingError, "32, 64, 128"):
            RankingConfig(max_pairs_per_group=129).validate()
        with self.assertRaisesRegex(RankingError, "32, 64, 128"):
            RankingConfig(max_pairs_per_group=17).validate()

    # 19A.4
    def test_pair_sampling_seed_is_reproducible(self):
        rows = [candidate("c-%d" % index, delay_ms=20 * index + 1)
                for index in range(30)]
        config = RankingConfig(seed="fixed-seed")
        first, first_manifest = build_pair_dataset(rows, config)
        second, second_manifest = build_pair_dataset(rows, config)
        self.assertEqual(first, second)
        self.assertEqual(first_manifest, second_manifest)

    # 19A.5
    def test_ties_are_skipped_and_builder_margin_is_authoritative(self):
        rows = [candidate("a", delay_ms=20), candidate("b", delay_ms=30)]
        zero_pairs, _ = build_pair_dataset(
            rows, RankingConfig(tie_margin_ns=0))
        ten_pairs, ten = build_pair_dataset(
            rows, RankingConfig(tie_margin_ns=10 * MS))
        fifty_pairs, fifty = build_pair_dataset(
            rows, RankingConfig(tie_margin_ns=50 * MS))
        self.assertEqual(len(zero_pairs), 1)
        self.assertEqual(ten_pairs, [])
        self.assertEqual(fifty_pairs, [])
        self.assertEqual(ten["tie_pairs_skipped"], 1)
        self.assertEqual(fifty["tie_margin_authority"], "pair_builder_config")

    # 19A.6
    def test_two_horizon_censored_pages_do_not_form_a_pair(self):
        rows = [candidate("a", observed=False),
                candidate("b", observed=False)]
        pairs, manifest = build_pair_dataset(rows)
        self.assertEqual(pairs, [])
        self.assertEqual(manifest["double_censored_pairs_skipped"], 1)

    # 19A.7
    def test_observed_page_ranks_before_horizon_censored_page(self):
        observed = candidate("observed", delay_ms=500)
        censored = candidate("inf", observed=False)
        pairs, _manifest = build_pair_dataset([observed, censored])
        self.assertEqual(len(pairs), 1)
        pair = pairs[0]
        self.assertEqual(pair.earlier_key, observed.key)
        if pair.left_key == observed.key:
            self.assertEqual(pair.label, 1)
        else:
            self.assertEqual(pair.label, -1)

    # 19A.8
    def test_session_end_censor_is_never_treated_as_horizon_negative(self):
        rows = [candidate("observed", delay_ms=100),
                candidate("early-end", observed=False,
                          session_censored=True)]
        pairs, manifest = build_pair_dataset(rows)
        self.assertEqual(pairs, [])
        self.assertEqual(manifest["session_end_censored_pairs_skipped"], 1)

    # 19A.9
    def test_higher_learned_score_means_earlier_next_real_access(self):
        rows, _pairs, _manifest, model, _quantized = learned_fixture()
        lookup = candidate_index(rows)
        self.assertEqual(model.score_semantics, SCORE_SEMANTICS)
        self.assertGreater(model.score(lookup[("exp", "session", "early-0")]),
                           model.score(lookup[("exp", "session", "late-0")]))

    # 19A.10
    def test_swapping_pair_features_reverses_the_label(self):
        rows = [candidate("a", delay_ms=10), candidate("b", delay_ms=100)]
        pair = build_pair_dataset(rows)[0][0]
        swapped = pair.swapped()
        self.assertEqual(swapped.left_key, pair.right_key)
        self.assertEqual(swapped.right_key, pair.left_key)
        self.assertEqual(swapped.label, -pair.label)
        self.assertEqual(swapped.earlier_key, pair.earlier_key)

    # 19A.11
    def test_training_exports_an_independent_single_folio_score(self):
        rows, pairs, _manifest, model, _quantized = learned_fixture()
        scores = score_all(model, rows)
        self.assertEqual(model.bias, 0.0)
        self.assertTrue(all(isinstance(value, float)
                            for value in scores.values()))
        self.assertEqual(len(RANK_ABLATIONS), 5)
        pair = pairs[0]
        margin = scores[pair.left_key] - scores[pair.right_key]
        self.assertGreater(pair.label * margin, 0.0)

    def test_training_feature_set_is_an_exact_declared_allowlist(self):
        rows = [candidate("a", delay_ms=10),
                candidate("b", delay_ms=100)]
        rows = [replace(row, features=dict(
            row.features, next_reuse_delay_ns=10)) for row in rows]
        pairs, _manifest = build_pair_dataset(rows)
        with self.assertRaisesRegex(RankingError, "declared ranking ablation"):
            fit_pairwise_ranker(
                rows, pairs, feature_names=("next_reuse_delay_ns",),
                bin_boundaries={"next_reuse_delay_ns": (1, 2, 3)})

    # 19A.12
    def test_runtime_scorers_and_contract_do_not_use_sigmoid_sort_or_pairs(self):
        rows, _pairs, manifest, _model, quantized = learned_fixture()
        document = make_model_document(
            quantized, {"cold_threshold": -1, "hot_threshold_1": 1,
                        "hot_threshold_2": 2}, manifest)
        float_source = inspect.getsource(FloatRankModel.score)
        quant_source = inspect.getsource(QuantizedRankModel.score)
        for source in (float_source, quant_source):
            self.assertNotIn("math.exp", source)
            self.assertNotIn("_training_derivative", source)
            self.assertNotIn("sorted(", source)
            self.assertNotIn("PairSample", source)
        self.assertFalse(document["runtime_sigmoid"])
        self.assertFalse(document["runtime_sorting"])
        self.assertFalse(document["runtime_pairwise_comparison"])
        self.assertFalse(document["score_is_probability"])
        self.assertIsInstance(quantized.score(rows[0]), int)

    # 19A.13
    def test_quantized_and_float_pair_orderings_are_consistent(self):
        rows, pairs, _manifest, model, quantized = learned_fixture()
        result = quantized_ordering_consistency(
            pairs, score_all(model, rows), score_all(quantized, rows))
        self.assertEqual(result["ordering_consistency"], 1.0)
        self.assertEqual(result["quantized_ties"], 0)

    # 19A.14
    def test_score_threshold_selection_rejects_non_validation_rows(self):
        rows = [
            candidate("r0", split="validation", app="FILES",
                      observed=False, native_tier=0, tier_idx=0),
            candidate("r1", split="validation", app="FILES", delay_ms=50,
                      native_tier=0, tier_idx=0),
            candidate("r2", split="validation", app="FILES", delay_ms=10,
                      native_tier=0, tier_idx=0),
            candidate("p0", split="validation", app="FILES",
                      observed=False, native_tier=1, tier_idx=0),
            candidate("p1", split="validation", app="FILES", delay_ms=10,
                      native_tier=1, tier_idx=0),
        ]
        scores = {row.key: score for row, score in
                  zip(rows, (0, 10, 20, -20, -10))}
        selected = select_validation_thresholds(rows, scores)
        self.assertEqual(selected["selected_on_split"], "validation")
        self.assertFalse(selected["test_set_used"])
        with self.assertRaisesRegex(RankingError, "validation"):
            select_validation_thresholds(
                rows + [candidate("train", split="train")],
                dict(scores, **{}))

    def test_validation_thresholds_use_session_cluster_bootstrap_gates(self):
        rows = []
        scores = {}
        for session_index in range(4):
            session_name = "validation-%d" % session_index
            for label, score, observed, delay_ms in (
                    ("r-cold", 0, False, None),
                    ("r-warm", 10, True, 500),
                    ("r-hot", 20, True, 100),
                    ("r-hottest", 30, True, 10)):
                row = candidate(
                    "%s-%d" % (label, session_index),
                    session=session_name, split="validation",
                    observed=observed, delay_ms=delay_ms,
                    native_tier=0, tier_idx=0)
                rows.append(row)
                scores[row.key] = score
            for label, score, observed, delay_ms in (
                    ("p-coldest", -30, False, None),
                    ("p-cold", -20, False, None),
                    ("p-hot", -10, True, 10)):
                row = candidate(
                    "%s-%d" % (label, session_index),
                    session=session_name, split="validation",
                    observed=observed, delay_ms=delay_ms,
                    native_tier=1, tier_idx=0)
                rows.append(row)
                scores[row.key] = score
        selected = select_validation_thresholds(
            rows, scores, max_upgrade_coverage=0.75,
            max_downgrade_mistake_ci95_upper=1.0)
        bootstrap = selected["session_cluster_bootstrap_ci"]
        self.assertEqual(bootstrap["status"], "COMPLETE")
        self.assertTrue(bootstrap["gate_eligible"])
        self.assertTrue(selected["all_runtime_thresholds_validation_selected"])
        self.assertTrue(selected["protect_only_gate_pass"])
        self.assertTrue(selected["bidirectional_gate_pass"])
        self.assertGreater(
            bootstrap["upgrade_rate_difference"]["ci95"][0], 0.0)
        self.assertLess(
            bootstrap["downgrade_rate_difference"]["ci95"][1], 0.0)

    def test_disjoint_arm_sessions_are_ineligible_for_difference_bootstrap(self):
        rows = []
        scores = {}
        for session_index in range(2):
            for score in (20, 30):
                row = candidate(
                    "selected-%d-%d" % (session_index, score),
                    session="selected-only-%d" % session_index,
                    split="validation", delay_ms=10, observed=True)
                rows.append(row)
                scores[row.key] = score
        for session_index in range(2):
            for score in (0, 10):
                row = candidate(
                    "baseline-%d-%d" % (session_index, score),
                    session="baseline-only-%d" % session_index,
                    split="validation", delay_ms=None, observed=False)
                rows.append(row)
                scores[row.key] = score
        selected = select_validation_thresholds(
            rows, scores, max_upgrade_coverage=0.75)
        bootstrap = selected["session_cluster_bootstrap_ci"][
            "upgrade_rate_difference"]
        self.assertEqual(bootstrap["status"],
                         "INSUFFICIENT_PAIRED_SESSIONS")
        self.assertEqual(bootstrap["paired_session_count"], 0)
        self.assertEqual(bootstrap["selected_only_session_count"], 2)
        self.assertEqual(bootstrap["baseline_only_session_count"], 2)
        self.assertFalse(bootstrap["gate_eligible"])
        self.assertFalse(selected["protect_only_gate_pass"])

    def test_threshold_evidence_is_base_page_weighted_for_mixed_orders(self):
        rows = []
        scores = {}
        for session_index in range(4):
            session_name = "mixed-order-%d" % session_index
            for label, score, observed, delay_ms, pages in (
                    ("r-cold", 0, False, None, 1),
                    ("r-warm", 10, True, 500, 3),
                    ("r-hot", 20, True, 100, 4),
                    ("r-hottest", 30, True, 10, 2)):
                row = candidate(
                    "%s-%d" % (label, session_index),
                    session=session_name, split="validation",
                    observed=observed, delay_ms=delay_ms,
                    native_tier=0, tier_idx=0, pages=pages)
                rows.append(row)
                scores[row.key] = score
            for label, score, observed, delay_ms, pages in (
                    ("p-coldest", -30, False, None, 4),
                    ("p-cold", -20, False, None, 2),
                    ("p-hot", -10, True, 10, 4)):
                row = candidate(
                    "%s-%d" % (label, session_index),
                    session=session_name, split="validation",
                    observed=observed, delay_ms=delay_ms,
                    native_tier=1, tier_idx=0, pages=pages)
                rows.append(row)
                scores[row.key] = score
        selected = select_validation_thresholds(
            rows, scores, max_upgrade_coverage=0.75,
            max_downgrade_mistake_ci95_upper=1.0)
        upgrade = selected["upgrade"]
        self.assertIsNotNone(upgrade)
        hot = upgrade["selected"]
        baseline = upgrade["keep_reclaim"]
        self.assertEqual(hot["candidate_count"], 8)
        self.assertEqual(hot["base_pages"], 24)
        self.assertEqual(hot["candidate_coverage"], 0.5)
        self.assertEqual(hot["base_page_coverage"], 0.6)
        self.assertEqual(hot["coverage"], 0.6)
        # One cold candidate and one warm candidate exist per session, but
        # the three-page warm folio makes the baseline reuse rate 3/4.
        self.assertEqual(baseline["future_reuse_rate_5s"], 0.75)
        self.assertEqual(baseline["base_pages"], 16)
        self.assertTrue(selected["protect_only_gate_pass"])

    def test_one_distinct_score_cannot_vacuously_pass_monotonicity(self):
        rows = [candidate("a", delay_ms=10),
                candidate("b", delay_ms=100)]
        result = score_bucket_monotonicity(
            rows, {row.key: 7 for row in rows})
        self.assertEqual(result["distinct_score_count"], 1)
        self.assertFalse(result["evidence_sufficient"])
        self.assertFalse(result["median_delay_nonincreasing"])
        self.assertFalse(result["monotonicity_pass"])

    # 19A.15
    def test_cold_and_hot_threshold_equality_has_explicit_semantics(self):
        expected = {
            -10: -256,
            -9: 0,
            9: 0,
            10: 256,
            19: 256,
            20: 512,
        }
        for score, delta in expected.items():
            with self.subTest(score=score):
                self.assertEqual(rank_score_to_delta_q8(score, -10, 10, 20),
                                 delta)
        with self.assertRaises(RankingError):
            rank_score_to_delta_q8(0, 0, 0, 1)

    # 19A.16
    def test_score_overflow_is_detected_not_wrapped(self):
        self.assertEqual(checked_s32_add(S32_MAX - 1, 1), S32_MAX)
        with self.assertRaises(OverflowError):
            checked_s32_add(S32_MAX, 1)

    # 19A.17
    def test_model_json_schema_and_checksum_enforce_rank_contract(self):
        _rows, _pairs, manifest, _model, quantized = learned_fixture()
        document = make_model_document(
            quantized, {"cold_threshold": -1, "hot_threshold_1": 1,
                        "hot_threshold_2": 2}, manifest)
        validate_model_document(document)
        schema_path = Path(__file__).parents[1] / "ranking_model.schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        self.assertEqual(schema["properties"]["schema_version"]["const"], 2)
        self.assertEqual(schema["properties"]["model_type"]["const"],
                         "pairwise_linear_ranker")
        self.assertFalse(schema["properties"]["score_is_probability"]["const"])
        self.assertEqual(set(schema["oneOf"][0]["required"]), set(document))
        feature_options = schema["properties"]["feature_names"]["oneOf"]
        self.assertEqual(
            {tuple(item["const"]) for item in feature_options},
            set(RANK_ABLATIONS.values()))
        self.assertEqual(
            set(schema["properties"]["model_provenance"]["enum"]),
            {None, "SYNTHETIC_TEST_FIXTURE",
             "ENGINEERING_FIXTURE_UNTRAINED",
             "TRAINED_PAIRWISE_OFFLINE"})
        self.assertEqual(
            schema["properties"]["kernel_deployable_v1"]["const"], False)
        self.assertFalse(document["selected_for_live_use"])
        self.assertFalse(document["kernel_shape_compatible_v1"])
        self.assertFalse(document["kernel_deployable_v1"])
        self.assertEqual(document["model_provenance"],
                         "SYNTHETIC_TEST_FIXTURE")
        self.assertEqual(document["model_version"], 1)
        self.assertEqual(document["feature_schema_version"], 1)
        self.assertRegex(document["scorer_checksum"], r"^[0-9a-f]{64}$")
        self.assertEqual(document["score_threshold_cold"],
                         document["cold_threshold"])
        self.assertEqual(document["predictive_delta_tier_q8"],
                         document["delta_tier_q8"])
        self.assertIsNone(document["score_threshold_hot_3"])
        self.assertNotIn("hot_3", document["delta_tier_q8"])
        self.assertEqual(document["experimental_plus3"]["status"],
                         "NOT_SELECTED")
        self.assertFalse(document["experimental_plus3"]["default_enabled"])
        broken = dict(document)
        broken["score_is_probability"] = True
        with self.assertRaises(RankingError):
            validate_model_document(broken)
        for field, value in (
                ("score_q_format", "float32"),
                ("cold_threshold", -2),
                ("delta_tier_q8", {"cold": 0}),
                ("model_version", "parp-rank-v1")):
            with self.subTest(field=field):
                broken = dict(document)
                broken[field] = value
                broken["checksum"] = ""
                with self.assertRaises(RankingError):
                    validate_model_document(broken)
        broken = dict(document)
        broken["unexpected"] = True
        with self.assertRaisesRegex(RankingError, "unexpected"):
            validate_model_document(broken)

    def test_trained_export_rejects_test_use_and_fallback_provenance(self):
        _rows, _pairs, manifest, _model, quantized = learned_fixture()
        complete = {
            "selected_on_split": "validation",
            "test_set_used": False,
            "cold_threshold": -1,
            "hot_threshold_1": 1,
            "hot_threshold_2": 2,
            "all_runtime_thresholds_validation_selected": True,
            "threshold_provenance": {
                "cold_threshold": "VALIDATION_SELECTED",
                "hot_threshold_1": "VALIDATION_SELECTED",
                "hot_threshold_2": "VALIDATION_SELECTED",
            },
        }
        make_model_document(
            quantized, complete, manifest,
            model_provenance="TRAINED_PAIRWISE_OFFLINE")
        test_touched = dict(complete, test_set_used=True)
        with self.assertRaisesRegex(RankingError, "untouched test data"):
            make_model_document(
                quantized, test_touched, manifest,
                model_provenance="TRAINED_PAIRWISE_OFFLINE")
        fallback = dict(complete)
        fallback["threshold_provenance"] = dict(
            complete["threshold_provenance"],
            cold_threshold="DISABLED_FALLBACK_BELOW_MIN_SCORE")
        with self.assertRaisesRegex(RankingError, "exact validation"):
            make_model_document(
                quantized, fallback, manifest,
                model_provenance="TRAINED_PAIRWISE_OFFLINE")

    def test_scorer_checksum_recomputes_default_kernel_identity(self):
        root = Path(__file__).parents[4]
        payload = json.loads((Path(__file__).parents[1] /
                              "default_model.json").read_text(
                                  encoding="utf-8"))
        raw = payload["model"]
        names = payload["feature_names"]
        projection = scorer_parameter_projection(
            payload["model_type"], raw["model_version"], names,
            dict(zip(names, raw["bin_edges"])),
            dict(zip(names, raw["weights"])), raw["bias"])
        digest = scorer_parameter_checksum(projection)
        self.assertEqual(digest, payload["embedded_parameter_checksum"])
        header = (root / "include/linux/parp.h").read_text(encoding="utf-8")
        match = re.search(
            r"PARP_TIER_MODEL_CHECKSUM.*?\"([0-9a-f]{64})\"",
            header, re.DOTALL)
        self.assertIsNotNone(match)
        self.assertEqual(digest, match.group(1))
        self.assertRegex(
            header,
            r'PARP_TIER_MODEL_TYPE\s+"pairwise_linear_ranker"')

        core = (root / "mm/parp/core/effective_tier.c").read_text(
            encoding="utf-8")
        initializer = re.search(
            r"static const struct parp_global_reuse_model "
            r"parp_global_model\s*=\s*\{(.*?)\n\};",
            core, re.DOTALL)
        self.assertIsNotNone(initializer)
        body = initializer.group(1)

        def rows_between(start: str, end: str):
            section = re.search(start + r"\s*=\s*\{(.*?)" + end,
                                body, re.DOTALL)
            self.assertIsNotNone(section)
            return [
                [int(value) for value in re.findall(r"-?\d+", row)]
                for row in re.findall(r"\{([^{}]+)\}", section.group(1))
            ]

        kernel_edges = rows_between(
            r"\.bin_edges", r"\n\s*\},\n\s*\.weights")
        kernel_weights = rows_between(r"\.weights", r"\n\s*\},\s*$")
        bias_match = re.search(r"\.bias\s*=\s*(-?\d+)", body)
        self.assertIsNotNone(bias_match)
        kernel_projection = scorer_parameter_projection(
            "pairwise_linear_ranker", raw["model_version"], names,
            dict(zip(names, kernel_edges)),
            dict(zip(names, kernel_weights)), int(bias_match.group(1)))
        self.assertEqual(scorer_parameter_checksum(kernel_projection), digest)

        c_oracle = (Path(__file__).parents[1] / "cscore.c").read_text(
            encoding="utf-8")
        c_initializer = re.search(
            r"static const struct global_model model\s*=\s*\{(.*?)\n\};",
            c_oracle, re.DOTALL)
        self.assertIsNotNone(c_initializer)
        c_body = c_initializer.group(1)

        def c_rows_between(start: str, end: str):
            section = re.search(start + r"\s*=\s*\{(.*?)" + end,
                                c_body, re.DOTALL)
            self.assertIsNotNone(section)
            return [
                [int(value) for value in re.findall(r"-?\d+", row)]
                for row in re.findall(r"\{([^{}]+)\}", section.group(1))
            ]

        c_edges = c_rows_between(r"\.edges", r"\n\s*\},\n\s*\.weights")
        c_weights = c_rows_between(r"\.weights", r"\n\s*\},\s*$")
        c_bias = re.search(r"\.bias\s*=\s*(-?\d+)", c_body)
        self.assertIsNotNone(c_bias)
        c_projection = scorer_parameter_projection(
            "pairwise_linear_ranker", raw["model_version"], names,
            dict(zip(names, c_edges)), dict(zip(names, c_weights)),
            int(c_bias.group(1)))
        self.assertEqual(scorer_parameter_checksum(c_projection), digest)

    def test_rank_base_is_shape_compatible_but_not_yet_deployable(self):
        rows, pairs, manifest, _model, _quantized = learned_fixture()
        float_model = fit_pairwise_ranker(rows, pairs)
        document = make_model_document(
            quantize_ranker(float_model),
            {"cold_threshold": -1, "hot_threshold_1": 1,
             "hot_threshold_2": 2}, manifest)
        self.assertTrue(document["kernel_shape_compatible_v1"])
        self.assertFalse(document["kernel_deployable_v1"])

    # 19A.18
    def test_each_app_has_an_independent_pair_cap_per_split(self):
        rows = []
        for app, session in (("WPS", "wps"), ("FILES", "files")):
            for index in range(8):
                rows.extend((
                    candidate("%s-a-%d" % (app, index), delay_ms=10,
                              app=app, session=session,
                              batch="b-%d" % index),
                    candidate("%s-b-%d" % (app, index), delay_ms=100,
                              app=app, session=session,
                              batch="b-%d" % index),
                ))
        pairs, manifest = build_pair_dataset(
            rows, RankingConfig(app_pair_cap=3))
        self.assertEqual(len(pairs), 6)
        self.assertEqual(manifest["pairs_by_app"], {"WPS": 3, "FILES": 3})
        self.assertGreater(manifest["pairs_not_sampled_by_app_cap"], 0)

    # 19A.19
    def test_quality_is_stratified_at_fixed_native_tier(self):
        rows = [
            candidate("t1-fast", delay_ms=10, native_tier=1,
                      tier_idx=0, batch="t1"),
            candidate("t1-slow", delay_ms=100, native_tier=1,
                      tier_idx=0, batch="t1"),
            candidate("t2-fast", delay_ms=10, native_tier=2,
                      tier_idx=1, batch="t2"),
            candidate("t2-slow", delay_ms=100, native_tier=2,
                      tier_idx=1, batch="t2"),
        ]
        pairs, _manifest = build_pair_dataset(rows)
        scores = {row.key: -int(row.next_reuse_delay_ns) for row in rows}
        result = fixed_native_tier_stratification(rows, pairs, scores)
        self.assertEqual(result["native_tier"]["1"]["pairwise"]
                         ["pairwise_accuracy"], 1.0)
        self.assertEqual(result["native_tier"]["2"]["pairwise"]
                         ["pairwise_accuracy"], 1.0)
        self.assertEqual(result["boundary_native_tier_eq_tier_idx_plus_1"]
                         ["candidate_count"], 4)
        self.assertEqual(result["boundary_native_tier_eq_tier_idx_plus_1"]
                         ["spearman_observed_candidate_count"], 4)

    # 19A.20
    def test_pairwise_accuracy_ndcg_c_index_and_rank_correlation(self):
        rows = [candidate("fast", delay_ms=10),
                candidate("slow", delay_ms=100),
                candidate("inf", observed=False)]
        pairs, _manifest = build_pair_dataset(rows)
        scores = {
            rows[0].key: 3.0,
            rows[1].key: 2.0,
            rows[2].key: 1.0,
        }
        self.assertEqual(pairwise_accuracy(pairs, scores)
                         ["pairwise_accuracy"], 1.0)
        self.assertEqual(ndcg_at_k(rows, scores, 3), 1.0)
        self.assertEqual(concordance_index(pairs, scores), 1.0)
        self.assertEqual(spearman_rank_correlation(rows, scores), 1.0)
        quality = evaluate_ranker(rows, pairs, scores, ndcg_k=3)
        self.assertEqual(quality["pairwise"]["pairwise_accuracy"], 1.0)
        self.assertEqual(quality["ndcg_at_3"], 1.0)
        self.assertEqual(quality["c_index"], 1.0)
        self.assertIn("exp/session", quality["by_session"])


if __name__ == "__main__":
    unittest.main()
