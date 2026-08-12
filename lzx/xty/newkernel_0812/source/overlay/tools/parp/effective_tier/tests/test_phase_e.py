#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0
from __future__ import annotations

import json
import re  #lzx
import sys  #lzx
import tempfile
import unittest
from copy import deepcopy
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from unittest.mock import patch

from tools.parp.effective_tier.analyze import (
    analyze,
    analyze_telemetry,
    main as analyze_main,
    train_ranking_ablations,
    validate_labeled,
)
from tools.parp.effective_tier.collector import build_dataset, parse_exported_trace
from tools.parp.effective_tier.contracts import (
    BASE_FEATURES,
    ContractError,
    session_key,
    validate_access,
    validate_candidate,
    write_jsonl,
)
from tools.parp.effective_tier.experiment_plan import (
    build_plan,
    checklist_markdown,
    validate_manifest,
)
from tools.parp.effective_tier.ranking import validate_model_document


HERE = Path(__file__).resolve().parents[1]


def load_tests(loader: unittest.TestLoader, tests: unittest.TestSuite,
               pattern: str | None) -> unittest.TestSuite:
    """Load the explicitly named -lzx pressure test module.  #lzx"""

    path = Path(__file__).with_name("test_pressure-lzx.py")
    spec = spec_from_file_location("parp_effective_tier_pressure_lzx_tests",
                                   path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load test_pressure-lzx.py")
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    tests.addTests(loader.loadTestsFromModule(module))
    return tests  #lzx


def candidate(session: str, cookie: str, action: str, timestamp_ns: int,
              pages: int = 1, source_seq: int = 1) -> dict:
    decisions = {
        "KEEP_RECLAIM": (0, 0, False, False, False, 0, 0),
        "PREDICTIVE_UPGRADE": (0, 0, False, False, True, 256, 256),
        "KEEP_PROTECT": (1, 0, False, True, True, 0, 256),
        "PREDICTIVE_DOWNGRADE": (1, 0, False, True, False, -256, 0),
        "SPECIAL_NATIVE_PROTECT": (0, 0, True, False, False, 0, 0),
    }
    native, tier_idx, special, native_protect, effective_protect, delta, effective = decisions[action]
    score = {
        "KEEP_RECLAIM": 0,
        "PREDICTIVE_UPGRADE": 80,
        "KEEP_PROTECT": 0,
        "PREDICTIVE_DOWNGRADE": -80,
        "SPECIAL_NATIVE_PROTECT": 0,
    }[action]
    return {
        "schema_version": 2,
        "event_kind": "tier_gate_candidate",
        "timestamp_ns": timestamp_ns,
        "experiment_id": "exp",
        "session_id": session,
        "folio_cookie": cookie,
        "folio_lifetime_epoch": 7,
        "memcg_anon_id": "memcg-1",
        "nid": 0,
        "page_type": "file" if source_seq % 2 else "anon",
        "source_seq": source_seq,
        "generation_index": 0,
        "native_tier": native,
        "native_tier_idx": tier_idx,
        "mode": "SHADOW_EFFECTIVE_TIER",
        "special_native_protect": special,
        "native_protect": native_protect,
        "model_valid": True,
        "features_valid": True,
        "model_type": "pairwise_linear_ranker",
        "model_version": 1,
        "expected_model_version": 1,
        "feature_schema_version": 1,
        "model_checksum":
            "1d5cc9918ae0e2caea316d36841fd61bef562e68f0820188c1cc0a6195f1fa5e",
        "pairwise_model_checksum":
            "1d5cc9918ae0e2caea316d36841fd61bef562e68f0820188c1cc0a6195f1fa5e",
        "model_provenance": "ENGINEERING_FIXTURE_UNTRAINED",
        "features": {
            "time_since_last_real_access_ms": 10 + source_seq * 100,
            "previous_real_access_interval_ms": 20 + source_seq * 90,
            "reuse_interval_ema_ms": 30 + source_seq * 80,
            "consecutive_reclaim_candidate_count": source_seq % 5,
            "time_in_current_generation_ms": 40 + source_seq * 70,
            "access_ema_q8": min(255, source_seq * 20),
        },
        "reuse_score": score,
        "rank_score_bin": (0 if score <= -48 else
                           1 if score < 48 else
                           2 if score < 96 else
                           3 if score < 144 else 4),
        "score_percentile": None,
        "cold_threshold": -48,
        "hot_threshold_1": 48,
        "hot_threshold_2": 96,
        "hot_threshold_3": 144,
        "score_threshold_cold": -48,
        "score_threshold_hot_1": 48,
        "score_threshold_hot_2": 96,
        "score_threshold_hot_3": 144,
        "delta_tier_q8": delta,
        "predictive_delta_tier_q8": delta,
        "effective_tier_q8": effective,
        "effective_protect": effective_protect,
        "action": action,
        "bypass_reason": "NONE",
        "folio_nr_pages": pages,
        "batch_id": "batch-1",
        "reclaim_epoch": "epoch-1",
        "priority": 12,
        "score_duration_ns": 17 + source_seq,
        "actual_native_behavior": "protect" if (special or native_protect) else "reclaim",
        "isolate_result": "not_attempted" if (special or native_protect) else "succeeded",
        "reclaimed": None,
        "putback": None,
        "activated": None,
        "gate_reached": True,
        "candidate_scope": "ALL_NATIVE_TIER_GATE_FOLIOS",
    }


def access(session: str, cookie: str, timestamp_ns: int,
           lifetime: int = 7, source: str = "PTE_YOUNG") -> dict:
    return {
        "schema_version": 2,
        "event_kind": "real_access",
        "timestamp_ns": timestamp_ns,
        "experiment_id": "exp",
        "session_id": session,
        "folio_cookie": cookie,
        "folio_lifetime_epoch": lifetime,
        "access_source": source,
        "is_real_access": True,
        "candidate_count": 1,  #lzx
    }


def session(session_id: str, split: str, count: int,
            end_ns: int = 10_000_000_000, lost: int = 0) -> dict:
    return {
        "schema_version": 1,
        "experiment_id": "exp",
        "session_id": session_id,
        "app": {"train": "WPS", "validation": "FILES", "test": "QQ"}[split],
        "workload": "fixture-" + split,
        "mode": "SHADOW_EFFECTIVE_TIER",
        "pressure_level": "P2",
        "start_ns": 1,
        "observation_end_ns": end_ns,
        "split": split,
        "tier_gate_counter": {
            "measured": True,
            "source": "exported_debug_counter",
            "before": 100,
            "after": 100 + count,
            "delta": count,
        },
        "trace_loss": {
            "measured": True,
            "source": "exported_trace_per_cpu_stats",
            "before": 3,
            "after": 3 + lost,
            "lost": lost,
            "per_cpu": {"0": lost},
        },
    }


def dataset_fixture():
    actions = ["KEEP_RECLAIM", "PREDICTIVE_UPGRADE", "KEEP_PROTECT",
               "PREDICTIVE_DOWNGRADE"]
    records = []
    sessions = {}
    sequence = 1
    for split in ("train", "validation", "test"):
        session_id = "s-" + split
        sessions[("exp", session_id)] = session(session_id, split, 8)
        for repeat in range(2):
            for action_name in actions:
                cookie = "%s-%d-%s" % (split, repeat, action_name)
                timestamp = 1_000_000_000 + sequence * 10_000_000
                pages = 4 if action_name in ("PREDICTIVE_UPGRADE",
                                             "PREDICTIVE_DOWNGRADE") else 1
                records.append(candidate(session_id, cookie, action_name,
                                         timestamp, pages, sequence))
                # Make upgrades hot and downgrades cold; keep classes mixed.
                should_reuse = action_name == "PREDICTIVE_UPGRADE" or (
                    action_name == "KEEP_PROTECT" and repeat == 0) or (
                    action_name == "KEEP_RECLAIM" and repeat == 1)
                if should_reuse:
                    records.append(access(session_id, cookie,
                                          timestamp + 50_000_000))
                sequence += 1
    return records, sessions


class PhaseECollectorTests(unittest.TestCase):
    def test_exported_kernel_trace_is_normalized_offline(self):
        lines = [
            "task: parp_effective_tier_decision: time=1000000000 "
            "experiment=1 session=2 cookie=99 lifetime=7 memcg=3 nid=0 "
            "type=1 source_seq=8 gen=0 native_tier=0 tier_idx=0 special=0 mode=1 "
            "model_type=pairwise_linear_ranker model_version=1 "
            "expected_model_version=1 feature_schema_version=1 "
            "model_checksum=1d5cc9918ae0e2caea316d36841fd61bef562e68f0820188c1cc0a6195f1fa5e "
            "model_provenance=ENGINEERING_FIXTURE_UNTRAINED "
            "model_valid=1 features_valid=1 native_protect=0 "
            "effective_protect=1 actual_tier_protect=0 "
            "score=100 score_bin=3 rank_score_bin=3 "
            "thresholds=-48/48/96/144 delta_q8=256 effective_q8=256 "
            "action=1 bypass=0 pages=4 batch=11 epoch=12 priority=10 "
            "score_ns=20 decision_ns=30 trace_seq=7 sort=0 "
            "isolate_attempted=1 isolate_result=1 "
            "features=10,20,30,1,40,50 "
            "pressure_level_kernel=1 reclaim_context=1 sc_priority=10 "
            "nr_to_reclaim=100 nr_reclaimed_before=10 "
            "epoch_reclaimed_pages=10 batch_scanned_pages=8 "
            "batch_isolated_pages=4 batch_reclaimed_pages=2 "
            "consecutive_no_progress_batches=0 fixed_delta_q8=256 "
            "binary_bypass_delta_q8=256 pressure_aware_delta_q8=192 "
            "fixed_effective_protect=1 pressure_aware_effective_protect=1 "
            "pressure_policy_version=1 "
            "pressure_policy_provenance=ENGINEERING_PRESSURE_POLICY_UNVALIDATED "
            "pressure_bypass_reason=0",  #lzx
            "task: parp_effective_tier_access: time=1050000000 trace_seq=8 "
            "experiment=1 session=2 cookie=99 "
            "lifetime=7 gen=0 type=1 event=0 candidate_count=1 real=1",  #lzx
            "task: parp_effective_tier_access: time=1055000000 trace_seq=9 "
            "experiment=1 session=2 cookie=99 "
            "lifetime=7 gen=1 type=1 event=6 real=0",
            "task: parp_effective_tier_outcome: time=1060000000 trace_seq=10 "
            "experiment=1 session=2 cookie=99 "
            "lifetime=7 action=1 actual_action=0 outcome=0",
            "task: parp_effective_tier_batch: time=1100000000 trace_seq=11 "
            "experiment=1 session=2 batch=11 "
            "epoch=12 type=1 mode=1 candidates=4 upgrades=4 downgrades=0 "
            "isolated=4 reclaimed=4 model_ns=80 lock_ns=100",
            "task: parp_effective_tier_lock: time=1100000100 trace_seq=12 experiment=1 "
            "session=2 nid=0 mode=1 scope=0 wait_ns=5 held_ns=100 "
            "irq_disabled_ns=110 irq_measured=1",
            "task: parp_effective_tier_lock: time=1100000200 trace_seq=13 "
            "experiment=1 session=2 nid=0 mode=1 scope=1 wait_ns=7 "
            "held_ns=90 irq_disabled_ns=0 irq_measured=0",
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "trace.txt"
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            records, stats = parse_exported_trace([path])
        meta = session("2", "train", 1)
        meta["experiment_id"] = "1"
        labeled, telemetry, summary = build_dataset(
            records, {("1", "2"): meta})
        self.assertTrue(labeled[0]["labels"]["reuse_within_100ms"])
        self.assertTrue(labeled[0]["reclaimed"])
        self.assertEqual(labeled[0]["pressure_level_kernel"], 1)
        self.assertEqual(labeled[0]["pressure_aware_delta_q8"], 192)
        self.assertEqual(labeled[0]["pressure_policy_provenance"],
                         "ENGINEERING_PRESSURE_POLICY_UNVALIDATED")  #lzx
        self.assertEqual(stats["policy_move_access_events_ignored"], 1)
        self.assertEqual(stats["trace_sequence_missing"], 0)
        self.assertEqual(stats["trace_sequence_events"], 7)
        self.assertEqual(stats["trace_sequence_measured"], 1)
        self.assertEqual(telemetry[0]["component"], "batch_model_total")
        lock = next(row for row in telemetry
                    if row["event_kind"] == "lock_latency")
        self.assertEqual(lock["scope"], "scan_folios")
        self.assertEqual(lock["held_ns"], 100)
        self.assertTrue(lock["wait_measured"])
        rt_lock = next(row for row in telemetry
                       if row["event_kind"] == "lock_latency" and
                       row["scope"] == "batch")
        self.assertFalse(rt_lock["irq_disabled_measured"])
        self.assertIsNone(rt_lock["irq_disabled_ns"])
        self.assertTrue(summary["tier_gate_coverage_complete"])

    def test_control_baseline_trace_keeps_actual_scorer_identity(self):
        lines = []
        for sequence, mode, model_type in (
                (1, 4, "random_matched_baseline"),
                (2, 5, "recency_baseline")):
            lines.append(
                "task: parp_effective_tier_decision: time=100000000%d "
                "experiment=1 session=2 cookie=%d lifetime=7 memcg=3 nid=0 "
                "type=1 source_seq=%d gen=0 native_tier=0 tier_idx=0 "
                "special=0 mode=%d model_type=%s model_version=1 "
                "expected_model_version=1 feature_schema_version=1 "
                "model_checksum=none model_provenance=CONTROL_BASELINE "
                "model_valid=1 features_valid=1 native_protect=0 "
                "effective_protect=0 actual_tier_protect=0 score=0 "
                "score_bin=1 rank_score_bin=1 thresholds=-48/48/96/144 "
                "delta_q8=0 effective_q8=0 action=0 bypass=0 pages=1 "
                "batch=11 epoch=12 priority=10 score_ns=20 decision_ns=30 "
                "trace_seq=%d sort=0 isolate_attempted=1 isolate_result=1 "
                "features=10,20,30,1,40,50" %
                (sequence, sequence, sequence, mode, model_type, sequence))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "trace.txt"
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            records, _stats = parse_exported_trace([path])
        self.assertEqual(len(records), 2)
        for row, expected_mode, expected_type in zip(
                records,
                ("APPLY_RANDOM_MATCHED", "APPLY_RECENCY_BASELINE"),
                ("random_matched_baseline", "recency_baseline")):
            validate_candidate(row)
            self.assertEqual(row["mode"], expected_mode)
            self.assertEqual(row["model_type"], expected_type)
            self.assertEqual(row["model_provenance"], "CONTROL_BASELINE")
            self.assertIsNone(row["model_checksum"])
            self.assertIsNone(row["pairwise_model_checksum"])

    def test_batch_reclaim_result_backfills_exported_candidate_only(self):
        lines = [
            "task: parp_effective_tier_decision: time=1000000000 "
            "experiment=1 session=2 cookie=99 lifetime=7 memcg=3 nid=0 "
            "type=1 source_seq=8 gen=0 native_tier=0 tier_idx=0 special=0 mode=1 "
            "model_type=pairwise_linear_ranker model_version=1 "
            "expected_model_version=1 feature_schema_version=1 "
            "model_checksum=1d5cc9918ae0e2caea316d36841fd61bef562e68f0820188c1cc0a6195f1fa5e "
            "model_provenance=ENGINEERING_FIXTURE_UNTRAINED model_valid=1 "
            "features_valid=1 native_protect=0 effective_protect=1 "
            "actual_tier_protect=0 score=100 score_bin=3 rank_score_bin=3 "
            "thresholds=-48/48/96/144 delta_q8=256 effective_q8=256 "
            "action=1 bypass=0 pages=4 batch=11 epoch=12 priority=10 "
            "score_ns=20 decision_ns=30 trace_seq=1 sort=0 "
            "isolate_attempted=1 isolate_result=1 features=10,20,30,1,40,50",
            "task: parp_effective_tier_batch: time=1000000010 trace_seq=2 "
            "experiment=1 session=2 batch=11 epoch=12 type=1 mode=1 candidates=4 "
            "upgrades=4 downgrades=0 isolated=4 model_ns=80 nr_to_reclaim=10 "
            "nr_reclaimed_before=0 epoch_reclaimed_pages=0 batch_scanned_pages=8 "
            "batch_isolated_pages=4 batch_reclaimed_pages=0 pressure_policy_version=1 "
            "consecutive_no_progress_batches=0 pressure_level_kernel=1 "
            "reclaim_context=1 pressure_bypass_reason=0 reclaim_result=0",
            "task: parp_effective_tier_batch: time=1000000020 trace_seq=3 "
            "experiment=1 session=2 batch=11 epoch=12 type=1 mode=1 candidates=4 "
            "upgrades=4 downgrades=0 isolated=4 model_ns=80 nr_to_reclaim=10 "
            "nr_reclaimed_before=0 epoch_reclaimed_pages=4 batch_scanned_pages=8 "
            "batch_isolated_pages=4 batch_reclaimed_pages=4 pressure_policy_version=1 "
            "consecutive_no_progress_batches=0 pressure_level_kernel=1 "
            "reclaim_context=1 pressure_bypass_reason=0 reclaim_result=1",
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "trace.txt"
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            records, stats = parse_exported_trace([path])
        candidate_row = next(row for row in records
                             if row["event_kind"] == "tier_gate_candidate")
        self.assertTrue(candidate_row["batch_reclaim_result_observed"])
        self.assertEqual(candidate_row["batch_reclaimed_pages"], 4)
        self.assertEqual(stats["decisions_with_batch_reclaim_result"], 1)  #lzx

    def test_valid_scorer_identity_cannot_claim_version_drift(self):
        row = candidate("s", "f", "KEEP_RECLAIM", 1_000_000_000)
        row["model_version"] = 2
        with self.assertRaisesRegex(ContractError, "expected model/schema"):
            validate_candidate(row)
        row["model_version"] = 1
        row["feature_schema_version"] = 2
        with self.assertRaisesRegex(ContractError, "expected model/schema"):
            validate_candidate(row)

    def test_score_delta_and_action_quadrant_corruption_is_rejected(self):
        cold_plus_three = candidate(
            "s", "cold-plus-three", "KEEP_RECLAIM", 1_000_000_000)
        cold_plus_three.update({
            "reuse_score": -80,
            "rank_score_bin": 0,
            "delta_tier_q8": 768,
            "predictive_delta_tier_q8": 768,
            "effective_tier_q8": 768,
            "effective_protect": True,
            "action": "PREDICTIVE_UPGRADE",
        })
        with self.assertRaisesRegex(ContractError,
                                    "delta disagrees with rank score bin"):
            validate_candidate(cold_plus_three)

        wrong_action = candidate(
            "s", "wrong-action", "PREDICTIVE_UPGRADE", 1_000_000_001)
        wrong_action["action"] = "KEEP_RECLAIM"
        with self.assertRaisesRegex(ContractError,
                                    "action disagrees.*quadrant"):
            validate_candidate(wrong_action)

        labeled, _telemetry, _summary = build_dataset(
            [candidate("s", "wrong-quadrant", "PREDICTIVE_UPGRADE",
                       1_000_000_002)],
            {("exp", "s"): session("s", "train", 1)})
        labeled[0]["quadrant"] = "KEEP_RECLAIM"
        with self.assertRaisesRegex(ContractError,
                                    "labeled quadrant disagrees"):
            validate_labeled(labeled[0])

    def test_invalid_model_trace_is_null_threshold_native_fallback(self):
        line = (
            "task: parp_effective_tier_decision: time=1000000000 "
            "experiment=1 session=2 cookie=99 lifetime=7 memcg=3 nid=0 "
            "type=1 source_seq=8 gen=0 native_tier=2 tier_idx=1 "
            "special=0 mode=1 model_type=pairwise_linear_ranker "
            "model_version=1 expected_model_version=1 "
            "feature_schema_version=1 "
            "model_checksum=1d5cc9918ae0e2caea316d36841fd61bef562e68f0820188c1cc0a6195f1fa5e "
            "model_provenance=ENGINEERING_FIXTURE_UNTRAINED "
            "model_valid=0 features_valid=0 native_protect=1 "
            "effective_protect=1 actual_tier_protect=1 score=0 "
            "score_bin=255 rank_score_bin=255 thresholds=0/0/0/0 "
            "delta_q8=0 effective_q8=512 action=2 bypass=2 pages=1 "
            "batch=11 epoch=12 priority=10 score_ns=0 decision_ns=30 "
            "trace_seq=1 sort=1 isolate_attempted=0 isolate_result=0 "
            "features=0,0,0,0,0,0")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "trace.txt"
            path.write_text(line + "\n", encoding="utf-8")
            records, _stats = parse_exported_trace([path])
        meta = session("2", "train", 1)
        meta["experiment_id"] = "1"
        labeled, _telemetry, _summary = build_dataset(
            records, {("1", "2"): meta})
        row = labeled[0]
        self.assertIsNone(row["score_threshold_cold"])
        self.assertIsNone(row["rank_score_bin"])
        self.assertEqual(row["predictive_delta_tier_q8"], 0)
        self.assertEqual(row["effective_tier_q8"], 2 * 256)
        self.assertEqual(row["effective_protect"], row["native_protect"])

    def test_real_access_labels_lifetime_and_all_windows(self):
        row = candidate("s", "f", "PREDICTIVE_UPGRADE", 1_000_000_000)
        meta = session("s", "train", 1)
        records = [
            row,
            access("s", "f", 1_010_000_000, lifetime=8),
            access("s", "f", 1_200_000_000, lifetime=7,
                   source="MARK_ACCESSED"),
        ]
        labeled, telemetry, summary = build_dataset(records, {("exp", "s"): meta})
        labels = labeled[0]["labels"]
        self.assertFalse(labels["reuse_within_100ms"])
        self.assertTrue(labels["reuse_within_500ms"])
        self.assertTrue(labels["reuse_within_1s"])
        self.assertTrue(labels["reuse_within_5s"])
        self.assertEqual(labeled[0]["next_real_access_source"], "MARK_ACCESSED")
        self.assertEqual(summary["trace_lost"], 0)
        self.assertTrue(summary["tier_gate_coverage_complete"])
        self.assertEqual(telemetry, [])

    def test_right_censoring_is_null_not_cold(self):
        row = candidate("s", "f", "KEEP_RECLAIM", 1_000_000_000)
        meta = session("s", "train", 1, end_ns=1_250_000_000)
        labeled, _telemetry, _summary = build_dataset(
            [row], {("exp", "s"): meta})
        labels = labeled[0]["labels"]
        self.assertFalse(labels["reuse_within_100ms"])
        self.assertIsNone(labels["reuse_within_500ms"])
        self.assertIsNone(labels["reuse_within_1s"])
        self.assertIsNone(labels["reuse_within_5s"])
        self.assertTrue(labeled[0]["censored_by_session_end"])
        self.assertFalse(labeled[0]["observed_within_horizon"])
        self.assertIsNone(labeled[0]["next_reuse_delay_ns"])

    def test_complete_ranking_horizon_is_not_session_censored(self):
        row = candidate("s", "f", "KEEP_RECLAIM", 1_000_000_000)
        meta = session("s", "train", 1, end_ns=6_000_000_000)
        labeled, _telemetry, _summary = build_dataset(
            [row], {("exp", "s"): meta})
        self.assertFalse(labeled[0]["observed_within_horizon"])
        self.assertFalse(labeled[0]["censored_by_session_end"])
        self.assertIsNone(labeled[0]["next_reuse_delay_ns"])

    def test_invalid_features_cannot_be_fabricated_zeroes(self):
        row = candidate("s", "f", "KEEP_RECLAIM", 1_000_000_000)
        row["features_valid"] = False
        row["model_valid"] = False
        row["reuse_score"] = 0
        row["rank_score_bin"] = None
        for field in ("cold_threshold", "hot_threshold_1", "hot_threshold_2",
                      "hot_threshold_3", "score_threshold_cold",
                      "score_threshold_hot_1", "score_threshold_hot_2",
                      "score_threshold_hot_3"):
            row[field] = None
        row["delta_tier_q8"] = 0
        row["predictive_delta_tier_q8"] = 0
        row["effective_tier_q8"] = row["native_tier"] * 256
        row["effective_protect"] = row["native_protect"]
        with self.assertRaises(ContractError):
            build_dataset([row], {("exp", "s"): session("s", "train", 1)})
        row["features"] = {name: None for name in BASE_FEATURES}
        labeled, _telemetry, _summary = build_dataset(
            [row], {("exp", "s"): session("s", "train", 1)})
        validate_labeled(labeled[0])

    def test_policy_move_cannot_be_a_real_access_label(self):
        event = access("s", "f", 123, source="NATIVE_GENERATION_MOVE")
        with self.assertRaises(ContractError):
            validate_access(event)

    def test_uncorrelated_real_access_cannot_be_a_future_label(self):
        event = access("s", "f", 123)
        event["candidate_count"] = 0  #lzx
        with self.assertRaisesRegex(ContractError, "candidate_count"):
            validate_access(event)

    def test_trace_loss_is_measured_not_inferred(self):
        row = candidate("s", "f", "KEEP_RECLAIM", 1_000_000_000)
        meta = session("s", "train", 1, lost=2)
        labeled, _telemetry, summary = build_dataset(
            [row], {("exp", "s"): meta})
        self.assertEqual(labeled[0]["trace_lost"], 2)
        self.assertEqual(labeled[0]["trace_loss_delta"], 2)
        self.assertFalse(labeled[0]["trace_loss_delta_zero"])
        self.assertFalse(labeled[0]["tier_gate_coverage_complete"])
        coverage = summary["sessions"][0]["coverage"]
        self.assertEqual(coverage["trace_loss_before"], 3)
        self.assertEqual(coverage["trace_loss_after"], 5)
        self.assertEqual(coverage["trace_loss_delta"], 2)
        self.assertFalse(coverage["trace_loss_delta_zero"])
        self.assertIn("trace_loss_delta_nonzero",
                      coverage["incomplete_reasons"])
        self.assertEqual(summary["trace_loss_delta"], 2)
        self.assertFalse(summary["trace_loss_delta_zero"])
        self.assertEqual(summary["status"],
                         "PARP_EFFECTIVE_TIER_OFFLINE_DATASET_INCOMPLETE")

    def test_trace_loss_before_after_delta_is_checked(self):
        row = candidate("s", "f", "KEEP_RECLAIM", 1_000_000_000)
        meta = session("s", "train", 1)
        meta["trace_loss"]["lost"] = 0
        meta["trace_loss"]["after"] = meta["trace_loss"]["before"] + 1
        with self.assertRaisesRegex(ContractError, "trace_loss delta"):
            build_dataset([row], {("exp", "s"): meta})

    def test_internal_trace_sequence_gap_invalidates_coverage(self):
        row = candidate("s", "f", "KEEP_RECLAIM", 1_000_000_000)
        row["raw_trace_lost_flag"] = True
        labeled, _telemetry, summary = build_dataset(
            [row], {("exp", "s"): session("s", "train", 1)})
        self.assertTrue(summary["trace_sequence_gap_detected"])
        self.assertFalse(summary["tier_gate_coverage_complete"])
        self.assertFalse(labeled[0]["tier_gate_coverage_complete"])
        reasons = summary["sessions"][0]["coverage"]["incomplete_reasons"]
        self.assertIn("trace_sequence_gap_detected", reasons)

    def test_special_native_protection_normalizes_to_keep_protect(self):
        row = candidate("s", "f", "SPECIAL_NATIVE_PROTECT", 1_000_000_000)
        labeled, _telemetry, _summary = build_dataset(
            [row], {("exp", "s"): session("s", "train", 1)})
        self.assertEqual(labeled[0]["quadrant"], "KEEP_PROTECT")


class PhaseEAnalysisTests(unittest.TestCase):
    def setUp(self):
        records, sessions = dataset_fixture()
        self.rows, _telemetry, _summary = build_dataset(records, sessions)

    def _gate_ready_ranking(self):
        ranking = deepcopy(train_ranking_ablations(self.rows))
        base = ranking["ranking_quality"]["ablations"]["rank_base"]
        test = base["quality"]["test"]["quantized"]
        test["pairwise"] = {
            "correct": 75,
            "pairs": 100,
            "pairwise_accuracy": 0.75,
            "score_ties": 0,
        }
        test["score_bucket_monotonicity"] = {
            "status": "COMPLETE",
            "evidence_sufficient": True,
            "monotonicity_pass": True,
        }
        test["fixed_native_tier"] = {
            "native_tier": {
                "0": {
                    "candidate_count": 100,
                    "pairwise": {"pairs": 60,
                                 "pairwise_accuracy": 0.75},
                },
                "1": {
                    "candidate_count": 80,
                    "pairwise": {"pairs": 40,
                                 "pairwise_accuracy": 0.70},
                },
            },
            "boundary_native_tier_eq_tier_idx_plus_1": {
                "candidate_count": 80,
                "spearman_observed_candidate_count": 80,
                "spearman": 0.40,
            },
        }
        base["threshold_selection"].update({
            "protect_only_gate_pass": True,
            "bidirectional_gate_pass": True,
            "session_cluster_bootstrap_ci": {
                "status": "COMPLETE",
                "gate_eligible": True,
            },
        })
        ranking["ranking_quantization"]["ablations"]["rank_base"][
            "splits"]["test"] = {
                "compared_non_tied_float_pairs": 100,
                "consistent_orderings": 100,
                "quantized_ties": 0,
                "ordering_consistency": 1.0,
            }
        return ranking

    @staticmethod
    def _set_future_reuse(row, reused):
        delay = 50_000_000 if reused else None
        row["next_real_access_delay_ns"] = delay
        row["next_reuse_delay_ns"] = delay
        row["next_real_access_source"] = "PTE_YOUNG" if reused else None
        row["observed_within_horizon"] = reused
        row["censored_by_session_end"] = False
        for label in row["labels"]:
            row["labels"][label] = reused

    def test_session_split_has_no_page_row_leakage(self):
        result = analyze(self.rows, [])
        self.assertTrue(result["summary"]["session_split_only"])
        seen = {}
        for row in self.rows:
            key = session_key(row)
            seen.setdefault(key, row["split"])
            self.assertEqual(seen[key], row["split"])

    def test_four_quadrants_and_page_weighted_action_metrics(self):
        result = analyze(self.rows, [])
        quadrants = result["tier_reclassification"]["quadrants"]
        self.assertEqual(set(quadrants), {
            "KEEP_RECLAIM", "PREDICTIVE_UPGRADE", "KEEP_PROTECT",
            "PREDICTIVE_DOWNGRADE",
        })
        upgrade = result["upgrade_analysis"]["primary"]
        downgrade = result["downgrade_analysis"]["primary"]
        self.assertEqual(upgrade["upgrade_hit_rate"], 1.0)
        self.assertEqual(upgrade["upgrade_waste_rate"], 0.0)
        self.assertEqual(downgrade["downgrade_mistake_rate"], 0.0)
        self.assertEqual(downgrade["downgrade_cold_precision"], 1.0)

    def test_five_pairwise_rank_ablations_never_route_by_app(self):
        result = analyze(self.rows, [])
        quality = result["model_quality"]
        self.assertFalse(quality["app_routing_enabled"])
        expected = {
            "rank_base",
            "rank_plus_native_tier",
            "rank_plus_native_tier_and_tier_idx",
            "recency_only_rank",
            "recent_frequency_rank",
        }
        self.assertEqual(set(quality["ablations"]), expected)
        prohibited = {"app", "app_id", "session_id", "workload"}
        for ablation_id, value in quality["ablations"].items():
            self.assertTrue(value["status"].startswith("TRAINED_OFFLINE"))
            self.assertFalse(prohibited.intersection(value["features"]))
            self.assertEqual(value["model_type"],
                             "pairwise_linear_ranker")
            self.assertEqual(value["kernel_shape_compatible_v1"],
                             ablation_id == "rank_base")
            self.assertFalse(value["kernel_deployable_v1"])
            self.assertEqual(set(value["quality"]),
                             {"train", "validation", "test"})
            self.assertFalse(value["threshold_selection"]["test_set_used"])

    def test_pairwise_outputs_are_mainline_and_probability_is_isolated(self):
        result = analyze(self.rows, [])
        summary = result["summary"]
        self.assertEqual(summary["primary_task"],
                         "pairwise_next-reuse_ranking")
        self.assertEqual(summary["model_type"],
                         "pairwise_linear_ranker")
        self.assertFalse(summary["score_is_probability"])
        self.assertFalse(summary["runtime_pairwise_comparison"])
        self.assertFalse(summary["runtime_candidate_sorting"])
        self.assertFalse(summary["runtime_sigmoid"])
        self.assertFalse(summary["probability_model_mainline"])
        self.assertEqual(result["probability_ablation"]["status"],
                         "NOT_IMPLEMENTED")
        self.assertEqual(result["probability_ablation"]["legacy_available"],
                         "LEGACY_1S_LOG_ODDS")
        for artifact in (
                "ranking_dataset", "pair_sampling", "ranking_model",
                "ranking_quality", "score_distribution",
                "score_reuse_monotonicity", "threshold_selection",
                "ranking_quantization", "probability_ablation"):
            self.assertIn(artifact, result)
        self.assertEqual(result["pair_sampling"]["split_unit"],
                         "session_before_pair_construction")
        self.assertFalse(result["threshold_selection"]["test_set_used"])
        self.assertEqual(
            result["threshold_selection"]["session_cluster_bootstrap_ci"][
                "status"],
            "REPORTED_PER_ABLATION")
        model = result["ranking_model"]
        self.assertEqual(model["artifact_kind"],
                         "no_exportable_ranking_model")
        self.assertEqual(model["training_status"],
                         "SCORER_TRAINED_POLICY_NOT_EXPORTABLE")
        self.assertFalse(model["selected_for_live_use"])
        self.assertEqual(result["pair_sampling"][
            "supported_pair_cap_ablations"], [32, 64, 128])
        tie_selection = result["pair_sampling"]["tie_margin_selection"]
        self.assertEqual(tie_selection["status"],
                         "SELECTED_ON_VALIDATION_PAIRWISE_ACCURACY")
        self.assertEqual(set(tie_selection["sensitivity"]),
                         {"0", "10000000", "50000000"})
        self.assertFalse(tie_selection["test_set_used"])
        self.assertFalse(summary["ranking_protect_only_gate"])
        self.assertFalse(summary["ranking_bidirectional_gate"])
        self.assertIn("session_cluster_bootstrap_ci",
                      summary["ranking_gate_blockers"])

    def test_ranking_gate_is_independent_of_legacy_1s_quadrant_direction(self):
        ranking = self._gate_ready_ranking()
        rows = deepcopy(self.rows)
        for row in rows:
            if row["quadrant"] in ("KEEP_RECLAIM",
                                    "PREDICTIVE_DOWNGRADE"):
                self._set_future_reuse(row, True)
            elif row["quadrant"] in ("PREDICTIVE_UPGRADE",
                                      "KEEP_PROTECT"):
                self._set_future_reuse(row, False)
        with patch(
                "tools.parp.effective_tier.analyze."
                "train_ranking_ablations", return_value=ranking):
            result = analyze(rows, [])
        gates = result["model_quality"]["deployment_gates"]
        auxiliary = gates["current_policy_auxiliary_direction"]
        self.assertFalse(auxiliary["upgrade_1s_direction_holds"])
        self.assertFalse(auxiliary["downgrade_1s_direction_holds"])
        self.assertFalse(auxiliary["used_by_ranking_gates"])
        self.assertTrue(gates["ranking_protect_only_gate"])
        self.assertTrue(gates["ranking_bidirectional_gate"])

    def test_quantized_order_reversal_blocks_ranking_gate(self):
        ranking = self._gate_ready_ranking()
        ordering = ranking["ranking_quantization"]["ablations"][
            "rank_base"]["splits"]["test"]
        ordering.update({
            "consistent_orderings": 0,
            "ordering_consistency": 0.0,
        })
        with patch(
                "tools.parp.effective_tier.analyze."
                "train_ranking_ablations", return_value=ranking):
            result = analyze(self.rows, [])
        gates = result["model_quality"]["deployment_gates"]
        self.assertFalse(gates["ranking_protect_only_gate"])
        self.assertIn("held_out_quantized_ordering_consistency",
                      gates["blockers"])

    def test_catastrophic_supported_native_tier_reversal_blocks_gate(self):
        ranking = self._gate_ready_ranking()
        fixed = ranking["ranking_quality"]["ablations"]["rank_base"][
            "quality"]["test"]["quantized"]["fixed_native_tier"]
        fixed["native_tier"]["0"]["pairwise"] = {
            "pairs": 20, "pairwise_accuracy": 0.90,
        }
        fixed["native_tier"]["1"]["pairwise"] = {
            "pairs": 1000, "pairwise_accuracy": 0.10,
        }
        with patch(
                "tools.parp.effective_tier.analyze."
                "train_ranking_ablations", return_value=ranking):
            result = analyze(self.rows, [])
        gates = result["model_quality"]["deployment_gates"]
        evidence = gates["fixed_native_tier_residual_evidence"]
        self.assertLess(evidence["pair_weighted_supported_tier_accuracy"],
                        0.5)
        self.assertEqual(evidence["minimum_supported_tier_accuracy"], 0.10)
        self.assertFalse(
            evidence["every_supported_tier_accuracy_floor_pass"])
        self.assertFalse(gates["ranking_protect_only_gate"])
        self.assertIn("fixed_native_tier_residual_discrimination",
                      gates["blockers"])

    def test_boundary_spearman_requires_its_observed_sample_support(self):
        ranking = self._gate_ready_ranking()
        boundary = ranking["ranking_quality"]["ablations"]["rank_base"][
            "quality"]["test"]["quantized"]["fixed_native_tier"][
                "boundary_native_tier_eq_tier_idx_plus_1"]
        boundary.update({
            "candidate_count": 100,
            "spearman_observed_candidate_count": 2,
            "spearman": 1.0,
        })
        with patch(
                "tools.parp.effective_tier.analyze."
                "train_ranking_ablations", return_value=ranking):
            result = analyze(self.rows, [])
        evidence = result["model_quality"]["deployment_gates"][
            "fixed_native_tier_residual_evidence"]
        self.assertFalse(evidence["boundary_support_pass"])
        self.assertFalse(result["summary"]["ranking_protect_only_gate"])

    def test_complete_validation_thresholds_export_trained_provenance(self):
        selected = {
            "selected_on_split": "validation",
            "test_set_used": False,
            "cold_threshold": -1,
            "hot_threshold_1": 0,
            "hot_threshold_2": 1,
            "threshold_provenance": {
                "cold_threshold": "VALIDATION_SELECTED",
                "hot_threshold_1": "VALIDATION_SELECTED",
                "hot_threshold_2": "VALIDATION_SELECTED",
                "hot_threshold_3": "NOT_SELECTED_EXPERIMENTAL",
            },
            "all_runtime_thresholds_validation_selected": True,
            "hot_threshold_2_evidence_pass": True,
            "protect_only_gate_pass": True,
            "bidirectional_gate_pass": True,
            "fallback": None,
            "session_cluster_bootstrap_ci": {
                "status": "COMPLETE",
                "gate_eligible": True,
            },
        }
        with patch(
                "tools.parp.effective_tier.analyze."
                "select_validation_thresholds",
                return_value=selected):
            result = analyze(self.rows, [])
        model = result["ranking_model"]
        self.assertEqual(model["model_provenance"],
                         "TRAINED_PAIRWISE_OFFLINE")
        self.assertFalse(model["selected_for_live_use"])
        self.assertEqual(model["pair_sampling"][
            "supported_pair_cap_ablations"], [32, 64, 128])
        schema = json.loads((HERE / "ranking_model.schema.json").read_text(
            encoding="utf-8"))
        self.assertTrue(set(schema["oneOf"][0]["required"]).issubset(model))

    def test_no_model_sentinel_matches_its_schema_branch(self):
        train_only = [row for row in self.rows if row["split"] == "train"]
        sentinel = analyze(train_only, [])["ranking_model"]
        validation_only = [row for row in self.rows
                           if row["split"] == "validation"]
        untrained = analyze(validation_only, [])["ranking_model"]
        self.assertEqual(untrained["training_status"], "NOT_TRAINED")
        schema = json.loads((HERE / "ranking_model.schema.json").read_text(
            encoding="utf-8"))
        branch = schema["oneOf"][1]
        self.assertTrue(set(branch["required"]).issubset(sentinel))
        self.assertEqual(sentinel["artifact_kind"],
                         branch["properties"]["artifact_kind"]["const"])
        self.assertEqual(
            sentinel["artifact_kind"],
            schema["properties"]["artifact_kind"]["const"])
        training_schema = branch["properties"]["training_status"]
        allowed = training_schema.get(
            "enum", [training_schema.get("const")])
        self.assertIn(sentinel["training_status"], allowed)
        self.assertIn(sentinel["training_status"],
                      schema["properties"]["training_status"]["enum"])

    def test_optional_draft202012_validation_covers_both_model_branches(self):
        selected = {
            "selected_on_split": "validation",
            "test_set_used": False,
            "cold_threshold": -1,
            "hot_threshold_1": 0,
            "hot_threshold_2": 1,
            "threshold_provenance": {
                "cold_threshold": "VALIDATION_SELECTED",
                "hot_threshold_1": "VALIDATION_SELECTED",
                "hot_threshold_2": "VALIDATION_SELECTED",
                "hot_threshold_3": "NOT_SELECTED_EXPERIMENTAL",
            },
            "all_runtime_thresholds_validation_selected": True,
            "hot_threshold_2_evidence_pass": True,
            "protect_only_gate_pass": True,
            "bidirectional_gate_pass": True,
            "fallback": None,
            "session_cluster_bootstrap_ci": {
                "status": "COMPLETE", "gate_eligible": True,
            },
        }
        with patch(
                "tools.parp.effective_tier.analyze."
                "select_validation_thresholds", return_value=selected):
            trained = analyze(self.rows, [])["ranking_model"]
        train_only = [row for row in self.rows if row["split"] == "train"]
        sentinel = analyze(train_only, [])["ranking_model"]
        schema = json.loads((HERE / "ranking_model.schema.json").read_text(
            encoding="utf-8"))
        try:
            import jsonschema  # type: ignore
        except ModuleNotFoundError:
            # The repository has no dependency on the optional package.  The
            # exported branch still receives authoritative procedural
            # validation, while the sentinel branch is checked against its
            # exact schema topology until a Draft 2020-12 validator is present.
            validate_model_document(trained)
            sentinel_branch = schema["oneOf"][1]
            self.assertTrue(set(sentinel_branch["required"]).issubset(
                sentinel))
            self.assertEqual(schema["$schema"],
                             "https://json-schema.org/draft/2020-12/schema")
        else:
            validator = jsonschema.Draft202012Validator(schema)
            validator.check_schema(schema)
            validator.validate(trained)
            validator.validate(sentinel)

    def test_cli_writes_ranking_first_artifacts_and_global_alias(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            samples = root / "labeled.jsonl"
            output = root / "offline-output"
            write_jsonl(samples, self.rows)
            self.assertEqual(analyze_main([
                "--samples", str(samples),
                "--output-dir", str(output),
            ]), 0)
            expected = {
                "ranking_dataset.json", "pair_sampling.json",
                "ranking_model.json", "global_model.json",
                "ranking_quality.json", "score_distribution.json",
                "score_reuse_monotonicity.json",
                "threshold_selection.json", "ranking_quantization.json",
                "probability_ablation.json", "model_quality.json",
            }
            self.assertTrue(expected.issubset(
                {path.name for path in output.iterdir()}))
            ranking_model = json.loads(
                (output / "ranking_model.json").read_text(encoding="utf-8"))
            global_model = json.loads(
                (output / "global_model.json").read_text(encoding="utf-8"))
            self.assertEqual(global_model, ranking_model)

    def test_missing_held_out_splits_produce_null_metrics_not_fixtures(self):
        train_only = [row for row in self.rows if row["split"] == "train"]
        result = analyze(train_only, [])
        summary = result["summary"]
        self.assertIsNone(summary["pairwise_accuracy"])
        self.assertIsNone(summary["ndcg"])
        self.assertIsNone(summary["c_index"])
        self.assertIsNone(summary["quantized_pairwise_accuracy"])
        self.assertFalse(summary["score_reuse_monotonicity_pass"])
        threshold = result["threshold_selection"]["ablations"]["rank_base"]
        self.assertEqual(threshold["status"],
                         "INSUFFICIENT_VALIDATION_CANDIDATES")
        no_model = result["ranking_model"]
        self.assertEqual(no_model["artifact_kind"],
                         "no_exportable_ranking_model")
        self.assertEqual(no_model["training_status"],
                         "SCORER_TRAINED_POLICY_NOT_EXPORTABLE")
        self.assertIsNone(no_model["scorer_checksum"])
        self.assertIsNone(no_model["model"])
        tie_selection = result["pair_sampling"]["tie_margin_selection"]
        self.assertEqual(
            tie_selection["status"],
            "INSUFFICIENT_VALIDATION_SUPPORT_NOT_SELECTED")
        self.assertIsNone(tie_selection["selected_tie_margin_ns"])
        self.assertEqual(tie_selection["effective_tie_margin_ns"],
                         10_000_000)
        self.assertEqual(tie_selection["fallback_tie_margin_ns"],
                         10_000_000)

    def test_session_split_leakage_is_rejected(self):
        broken = deepcopy(self.rows)
        broken[1]["session_id"] = broken[0]["session_id"]
        broken[1]["split"] = "test" if broken[0]["split"] != "test" else "train"
        with self.assertRaises(ContractError):
            analyze(broken, [])

    def test_observability_latency_efficiency_and_app_schema(self):
        base = {
            "schema_version": 2,
            "timestamp_ns": 1,
            "experiment_id": "exp",
            "session_id": "s",
            "mode": "SHADOW_EFFECTIVE_TIER",
        }
        telemetry = [
            dict(base, event_kind="score_latency", component="score",
                 duration_ns=value) for value in (10, 20, 30, 40)
        ]
        telemetry.extend((
            dict(base, event_kind="lock_latency", lock_name="lru_lock",
                 scope="scan_folios", held_ns=100, wait_ns=10,
                 irq_disabled_ns=90, wait_measured=True,
                 irq_disabled_measured=True),
            dict(base, event_kind="reclaim_latency", scope="direct_reclaim",
                 duration_ns=1000),
            dict(base, event_kind="reclaim_efficiency", scanned=100,
                 isolated=50, reclaimed=25, native_protected=10,
                 predictive_upgraded=2, predictive_downgraded=1,
                 pgscan=100, pgsteal=25, no_progress_rounds=0,
                 priority_drops=1, younger_generation_moves=2),
            dict(base, event_kind="app_latency", app="WPS", operation="save",
                 duration_ns=5000, success=False),
            dict(base, event_kind="app_session_summary", app="WPS",
                 total_duration_ns=50_000, stalls=2, timeouts=1, failures=1),
            dict(base, event_kind="vm_counter_delta",
                 counter="workingset_refault_file", delta=3),
        ))
        result = analyze_telemetry(telemetry)
        score = result["latency"]["score_and_effective_tier_ns"][
            "SHADOW_EFFECTIVE_TIER/score"]
        self.assertEqual(score["p50"], 25.0)
        efficiency = result["reclaim_efficiency"]["SHADOW_EFFECTIVE_TIER"]
        self.assertEqual(efficiency["reclaimed_per_scanned"], 0.25)
        app = result["app_latency"]["operations"][
            "SHADOW_EFFECTIVE_TIER/WPS/save"]
        self.assertEqual(app["failures"], 1)
        session_summary = result["app_latency"]["sessions"][
            "SHADOW_EFFECTIVE_TIER/WPS"]
        self.assertEqual(session_summary["timeouts"], 1)
        per_second = result["lock_latency"]["per_second_max_held_ns"][
            "SHADOW_EFFECTIVE_TIER/scan_folios"]
        self.assertEqual(per_second["seconds"][0]["max_held_ns"], 100)

    def test_unmeasured_lock_interval_must_be_null(self):
        base = {
            "schema_version": 2,
            "event_kind": "lock_latency",
            "timestamp_ns": 1,
            "experiment_id": "exp",
            "session_id": "s",
            "mode": "SHADOW_EFFECTIVE_TIER",
            "lock_name": "lru_lock",
            "scope": "scan_folios",
            "held_ns": 10,
            "wait_ns": None,
            "irq_disabled_ns": None,
            "wait_measured": False,
            "irq_disabled_measured": False,
        }
        analyze_telemetry([base])
        broken = dict(base, irq_disabled_ns=1)
        with self.assertRaises(ContractError):
            analyze_telemetry([broken])


class PhaseEPlanTests(unittest.TestCase):
    def setUp(self):
        self.manifest = json.loads(
            (HERE / "experiment_manifest.template.json").read_text(
                encoding="utf-8"))

    def test_plan_is_non_executable_and_stops_at_authorization(self):
        plan = build_plan(self.manifest)
        self.assertTrue(plan["generated_plan_only"])
        self.assertEqual(plan["runtime_actions_executed"], 0)
        self.assertEqual(plan["apply_actions_executed"], 0)
        self.assertTrue(all(cell["execution_status"] ==
                            "NOT_EXECUTED_PLAN_ONLY"
                            for cell in plan["cells"]))
        checklist = checklist_markdown(self.manifest, plan)
        self.assertIn("PARP_EFFECTIVE_TIER_LIVE_AUTH_REQUIRED", checklist)

    def test_unsafe_manifest_is_rejected(self):
        unsafe = deepcopy(self.manifest)
        unsafe["safety"]["apply_authorized"] = True
        with self.assertRaises(ContractError):
            validate_manifest(unsafe)

    def test_plus3_policy_is_separate_and_never_apply_supported(self):
        policy = self.manifest["policy_ablations"]
        self.assertEqual(policy["max_upgrade_tiers_apply"], [1, 2])
        self.assertEqual(policy["experimental_plus3"], {
            "max_upgrade_tiers": [3],
            "allowed_modes": [
                "SHADOW_EFFECTIVE_TIER", "ORACLE_OFFLINE_ONLY"],
            "apply_supported": False,
        })
        for corruption in (
                {"max_upgrade_tiers_apply": [1, 2, 3]},
                {"experimental_plus3": {
                    "max_upgrade_tiers": [3],
                    "allowed_modes": ["APPLY_BIDIRECTIONAL"],
                    "apply_supported": False,
                }},
                {"experimental_plus3": {
                    "max_upgrade_tiers": [3],
                    "allowed_modes": [
                        "SHADOW_EFFECTIVE_TIER", "ORACLE_OFFLINE_ONLY"],
                    "apply_supported": True,
                }}):
            with self.subTest(corruption=corruption):
                unsafe = deepcopy(self.manifest)
                unsafe["policy_ablations"].update(corruption)
                with self.assertRaises(ContractError):
                    validate_manifest(unsafe)
        unsafe = deepcopy(self.manifest)
        unsafe["command"] = "write something"
        with self.assertRaises(ContractError):
            validate_manifest(unsafe)

    def test_all_json_contracts_are_parseable(self):
        names = (
            "feature_schema.json",
            "raw_event.schema.json",
            "session_metadata.schema.json",
            "labeled_candidate.schema.json",
            "observability.schema.json",
            "experiment_manifest.schema.json",
            "experiment_manifest.template.json",
        )
        for name in names:
            with self.subTest(name=name):
                value = json.loads((HERE / name).read_text(encoding="utf-8"))
                self.assertIsInstance(value, dict)


class EffectiveTierTraceSourceTests(unittest.TestCase):  #lzx
    def test_decision_strings_are_tracefs_owned(self):  #lzx
        header = (HERE.parents[2] / "include" / "trace" / "events" /
                  "parp.h").read_text(encoding="utf-8")  #lzx
        decision = header.split("TRACE_EVENT(parp_effective_tier_decision,", 1)[1]  #lzx
        decision = decision.split("TRACE_EVENT(parp_effective_tier_access,", 1)[0]  #lzx
        for field in ("model_type", "model_checksum", "model_provenance",
                      "pressure_policy_provenance"):  #lzx
            with self.subTest(field=field):  #lzx
                self.assertRegex(  #lzx
                    decision, rf"__string\({field},\s*event->{field}\)")  #lzx
                self.assertIn(f"__assign_str({field});", decision)  #lzx
                self.assertIn(f"__get_str({field})", decision)  #lzx
                self.assertNotRegex(  #lzx
                    decision, rf"__array\(char,\s*{field},")  #lzx

    def test_decision_printk_has_no_late_string_argument(self):  #lzx
        header = (HERE.parents[2] / "include" / "trace" / "events" /
                  "parp.h").read_text(encoding="utf-8")  #lzx
        decision = header.split("TRACE_EVENT(parp_effective_tier_decision,", 1)[1]  #lzx
        decision = decision.split("TRACE_EVENT(parp_effective_tier_access,", 1)[0]  #lzx
        self.assertIn(  #lzx
            "model_provenance=%s pressure_policy_provenance=%s model_valid",
            decision)
        self.assertNotIn(  #lzx
            "pressure_policy_version=%u pressure_policy_provenance=%s",
            decision)


class EffectiveTierWorkspaceLayoutTests(unittest.TestCase):  #lzx
    def _module(self):  #lzx
        path = HERE / "workspace_layout-lzx.py"  #lzx
        spec = spec_from_file_location("parp_workspace_layout_lzx", path)  #lzx
        self.assertIsNotNone(spec)  #lzx
        self.assertIsNotNone(spec.loader)  #lzx
        module = module_from_spec(spec)  #lzx
        sys.modules[spec.name] = module  #lzx
        spec.loader.exec_module(module)  #lzx
        return module  #lzx

    def test_relocated_lzx_layout_is_resolved_without_absolute_path(self):  #lzx
        module = self._module()  #lzx
        with tempfile.TemporaryDirectory() as directory:  #lzx
            lzx_root = Path(directory) / "lzx"  #lzx
            source = (lzx_root / "kernel" / "v4-parp" / "work" / "linux-test" /  #lzx
                      "tools" / "parp" / "effective_tier")
            source.mkdir(parents=True)  #lzx
            (lzx_root / "tool" / "automation").mkdir(parents=True)  #lzx
            (lzx_root / "tool" / "automation" / "app_automation.py").touch()  #lzx
            bridge = (lzx_root / "tool" / "runtime_monitor" / "core" /  #lzx
                      "parp_bridge.py")  #lzx
            bridge.parent.mkdir(parents=True)  #lzx
            bridge.touch()  #lzx
            layout = module.resolve_workspace(source)  #lzx
            self.assertEqual(layout.lzx_root, lzx_root)  #lzx
            self.assertEqual(  #lzx
                layout.v4_parp_root, lzx_root / "kernel" / "v4-parp")  #lzx
            self.assertEqual(layout.missing_dependencies(), ())  #lzx


if __name__ == "__main__":
    unittest.main()
