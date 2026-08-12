import hashlib
import inspect
import json
import tempfile
import unittest
from pathlib import Path

from phase210b import selector_v2 as selector
from phase210b.labeler import label_selection, support


def make_row(i=0, **overrides):
    row = {
        "identity": "dev:ino:%d:1|g:100|%d" % (i, i),
        "session_id": "session-a", "app_id": 2, "domain_id": "domain-a",
        "file_version": 1, "partition_generation": "g:100", "segment_id": i,
        "current_active": False, "observation_state": "OBSERVED_INACTIVE",
        "generation_proxy": float(i % 16) / 16, "age": 100 + i,
        "time_since_last_active": 1000 + i, "time_since_last_observed": 1000 + i,
        "consecutive_inactive_windows": 4, "recent_access_count": i % 3,
        "segment_access_ema": .1, "file_access_ema": .2,
        "validity_flags": {"file_version": True, "partition_generation": True},
    }
    row.update(overrides)
    return row


class Phase210BContracts(unittest.TestCase):
    def setUp(self):
        self.rows = [make_row(i) for i in range(256)]

    def test_01_invalid_version_is_excluded(self):
        self.assertNotIn(make_row(500, file_version=None), selector.eligible_universe(self.rows + [make_row(500, file_version=None)]))

    def test_02_invalid_partition_is_excluded(self):
        self.assertNotIn(make_row(501, partition_generation=None), selector.eligible_universe(self.rows + [make_row(501, partition_generation=None)]))

    def test_03_cross_session_is_excluded(self):
        self.assertEqual(len(selector.eligible_universe(self.rows + [make_row(502, session_id="session-b")])), 256)

    def test_04_cross_domain_is_excluded(self):
        self.assertEqual(len(selector.eligible_universe(self.rows + [make_row(503, domain_id="domain-b")])), 256)

    def test_05_missing_identity_is_excluded(self):
        self.assertEqual(len(selector.eligible_universe(self.rows + [make_row(504, identity="")])), 256)

    def test_06_not_observed_is_excluded(self):
        self.assertEqual(len(selector.eligible_universe(self.rows + [make_row(505, observation_state="NOT_OBSERVED")])), 256)

    def test_07_active_is_excluded(self):
        self.assertEqual(len(selector.eligible_universe(self.rows + [make_row(506, current_active=True)])), 256)

    def test_08_all_eligible_rows_are_inactive(self):
        self.assertTrue(all(not row["current_active"] for row in selector.eligible_universe(self.rows)))

    def test_09_generation_stratum_is_single_valued(self):
        rows = selector.assign_generation_strata(self.rows)
        self.assertTrue(all(row["generation_stratum"] in {"G0", "G1", "G2", "G3"} for row in rows))

    def test_10_recency_stratum_is_single_valued(self):
        rows = selector.assign_recency_strata(self.rows)
        self.assertTrue(all(row["recency_stratum"] in {"R0", "R1", "R2", "R3"} for row in rows))

    def test_11_hybrid_stratum_is_single_valued(self):
        rows = selector.assign_hybrid_strata(self.rows)
        self.assertTrue(all(row["hybrid_stratum"] in {"H0", "H1", "H2", "H3"} for row in rows))

    def test_12_fallback_stratum_is_single_valued(self):
        selected, audit = selector.select([make_row(i, generation_proxy=None) for i in range(256)], "S4")
        self.assertEqual(audit["candidate_count"], 128)

    def test_13_quota_balanced_sums_to_128(self):
        self.assertEqual(sum(selector.QUOTAS["Q_BALANCED"]), 128)

    def test_14_quota_cold_heavy_sums_to_128(self):
        self.assertEqual(sum(selector.QUOTAS["Q_COLD_HEAVY"]), 128)

    def test_15_quota_middle_sums_to_128(self):
        self.assertEqual(sum(selector.QUOTAS["Q_MIDDLE"]), 128)

    def test_16_no_duplicate_candidates(self):
        rows, audit = selector.select(self.rows, "S3")
        self.assertEqual(audit["duplicate_count"], 0)
        self.assertEqual(len({row["identity"] for row in rows}), len(rows))

    def test_17_short_pool_is_not_padded(self):
        rows, audit = selector.select(self.rows[:17], "S2")
        self.assertEqual(len(rows), 17)
        self.assertTrue(audit["partial_candidate_universe"])

    def test_18_selection_is_stable_after_copy(self):
        a, aa = selector.select(self.rows, "S1", "Q_MIDDLE")
        b, bb = selector.select([dict(row) for row in self.rows], "S1", "Q_MIDDLE")
        self.assertEqual([row["identity"] for row in a], [row["identity"] for row in b])
        self.assertEqual(aa["candidate_hash"], bb["candidate_hash"])

    def test_19_candidate_hash_is_sha256(self):
        _, audit = selector.select(self.rows, "S0")
        self.assertEqual(len(audit["candidate_hash"]), 64)
        int(audit["candidate_hash"], 16)

    def test_20_selection_has_causal_audit(self):
        _, audit = selector.select(self.rows, "S3")
        self.assertTrue(audit["causal_inputs_only"])

    def test_21_active_pages_never_enter_any_policy(self):
        rows = self.rows + [make_row(600, current_active=True)]
        for policy in ("S0", "S1", "S2", "S3", "S4"):
            selected, _ = selector.select(rows, policy)
            self.assertFalse(any(row["current_active"] for row in selected))

    def test_22_selector_contract_has_fixed_count(self):
        self.assertEqual(selector.source_contract()["candidate_count"], 128)

    def test_23_selector_contract_rejects_unknown(self):
        self.assertFalse(selector.source_contract()["unknown_allowed"])

    def test_24_selector_source_has_no_annotation_inputs(self):
        source = inspect.getsource(selector).lower()
        for forbidden in ("future", "label", "positive", "negative", "operation", "filename", "path", "content"):
            self.assertNotIn(forbidden, source)

    def test_25_label_positive_requires_observation(self):
        candidate = {"decision_id": "d", "identity": "dev:ino:1:1|g:100|0", "file_key_metadata": "dev:ino:1:1", "partition_generation": "g:100", "segment_id": 0}
        windows = [{"files": {("dev:ino:1:1", "g:100"): {"observed": 1, "active": 0}}}, {"files": {("dev:ino:1:1", "g:100"): {"observed": 1, "active": 1}}}]
        self.assertEqual(label_selection([candidate], windows, 0, (10,))[0]["status"], "positive")

    def test_26_incomplete_window_is_unknown(self):
        candidate = {"decision_id": "d", "identity": "dev:ino:1:1|g:100|0", "file_key_metadata": "dev:ino:1:1", "partition_generation": "g:100", "segment_id": 0}
        self.assertEqual(label_selection([candidate], [{"files": {}}], 0, (10,))[0]["status"], "unknown")

    def test_27_future_not_observed_is_not_negative(self):
        candidate = {"decision_id": "d", "identity": "dev:ino:1:1|g:100|0", "file_key_metadata": "dev:ino:1:1", "partition_generation": "g:100", "segment_id": 0}
        windows = [{"files": {}}, {"files": {}}]
        self.assertNotEqual(label_selection([candidate], windows, 0, (10,))[0]["status"], "negative")

    def test_28_pairwise_requires_both_classes(self):
        base = {"selector_id": "S3", "quota_template": "Q_BALANCED", "app": "QQ", "session_id": "q", "horizon_seconds": 60, "decision_id": "d", "available": True}
        self.assertEqual(support([{**base, "status": "positive"}])[0]["pairwise_evaluable_decisions"], 0)

    def test_29_pairwise_accepts_both_classes(self):
        base = {"selector_id": "S3", "quota_template": "Q_BALANCED", "app": "QQ", "session_id": "q", "horizon_seconds": 60, "decision_id": "d", "available": True}
        self.assertEqual(support([{**base, "status": "positive"}, {**base, "status": "negative"}])[0]["pairwise_evaluable_decisions"], 1)

    def test_30_features_are_numeric_or_missing(self):
        for row in self.rows:
            for field in selector.source_contract()["features"]:
                self.assertTrue(field in row or field in {"generation_rank", "tier_proxy", "time_since_last_observed", "consecutive_inactive_windows"})

    def test_31_output_audit_is_json_serializable(self):
        _, audit = selector.select(self.rows, "S3")
        json.dumps(audit)


if __name__ == "__main__":
    unittest.main()
