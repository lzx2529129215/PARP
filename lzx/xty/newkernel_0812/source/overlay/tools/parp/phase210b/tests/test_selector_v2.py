import copy
import hashlib
import inspect
import unittest

from phase210b import selector_v2 as s


def candidate(index, generation, recency, active=False, observed=True):
    return {
        "identity": "8:0:%d:1|%d:100|%d" % (index, generation, index),
        "file_key_metadata": "8:0:%d:1" % index,
        "partition_generation": "%d:100" % generation,
        "segment_id": index,
        "generation_proxy": float(generation),
        "age": float(100 + index),
        "time_since_last_active": float(recency),
        "time_since_last_observed": float(recency),
        "consecutive_inactive_windows": min(12, int(recency // 10)),
        "recent_access_count": max(0, 10 - int(recency // 10)),
        "segment_access_ema": 0.1,
        "file_access_ema": 0.1,
        "current_active": active,
        "observation_state": "OBSERVED_ACTIVE" if active else ("OBSERVED_INACTIVE" if observed else "NOT_OBSERVED"),
        "file_version": 1,
        "validity_flags": {"file_version": True, "partition_generation": True},
    }


class SelectorV2Contracts(unittest.TestCase):
    def setUp(self):
        self.universe = [candidate(i, i % 4, 10 + (i % 100) * 10) for i in range(256)]

    def test_hard_eligibility_excludes_active_and_unobserved(self):
        rows = self.universe + [candidate(1000, 0, 100, active=True), candidate(1001, 0, 100, observed=False)]
        eligible = s.eligible_universe(rows)
        self.assertEqual(len(eligible), 256)
        self.assertTrue(all(not row["current_active"] for row in eligible))
        self.assertTrue(all(row["observation_state"] != "NOT_OBSERVED" for row in eligible))

    def test_generation_strata_are_mutually_exclusive(self):
        rows = s.assign_generation_strata(self.universe)
        self.assertEqual(len({row["generation_stratum"] for row in rows}), 4)
        self.assertEqual(sum(row["generation_stratum"] == "G0" for row in rows), 64)
        self.assertTrue(all(row.get("generation_stratum") in {"G0", "G1", "G2", "G3"} for row in rows))

    def test_recency_strata_are_mutually_exclusive(self):
        rows = s.assign_recency_strata(self.universe)
        self.assertEqual({row["recency_stratum"] for row in rows}, {"R0", "R1", "R2", "R3"})
        self.assertEqual(sum(row["recency_stratum"] == "R0" for row in rows), 64)

    def test_hybrid_strata_are_mutually_exclusive(self):
        rows = s.assign_hybrid_strata(self.universe)
        self.assertEqual(len({row["hybrid_stratum"] for row in rows}), 4)
        self.assertTrue(all(row["hybrid_stratum"] in {"H0", "H1", "H2", "H3"} for row in rows))

    def test_all_selector_candidates_are_unique_and_fixed(self):
        for selector in ("S0", "S1", "S2", "S3"):
            selected, audit = s.select(self.universe, selector, "Q_BALANCED")
            self.assertEqual(len(selected), 128)
            self.assertEqual(len({row["identity"] for row in selected}), 128)
            self.assertEqual(audit["candidate_count"], 128)

    def test_s4_is_only_generation_missing_fallback(self):
        rows = [dict(row, generation_proxy=None) for row in self.universe]
        selected, audit = s.select(rows, "S4", "Q_BALANCED")
        self.assertEqual(len(selected), 128)
        self.assertTrue(audit["fallback_only_generation_missing"])
        selected, audit = s.select(self.universe, "S4", "Q_BALANCED")
        self.assertEqual(len(selected), 0)
        self.assertTrue(audit["fallback_only_generation_missing"])

    def test_short_universe_is_partial_without_duplication(self):
        selected, audit = s.select(self.universe[:40], "S3", "Q_BALANCED")
        self.assertEqual(len(selected), 40)
        self.assertTrue(audit["partial_candidate_universe"])

    def test_quota_templates_are_deterministic(self):
        for quota in ("Q_BALANCED", "Q_COLD_HEAVY", "Q_MIDDLE"):
            left, left_audit = s.select(self.universe, "S3", quota)
            right, right_audit = s.select(copy.deepcopy(self.universe), "S3", quota)
            self.assertEqual([x["identity"] for x in left], [x["identity"] for x in right])
            self.assertEqual(left_audit["candidate_hash"], right_audit["candidate_hash"])

    def test_selector_does_not_read_label_or_future_fields(self):
        source = inspect.getsource(s)
        for forbidden in ("future", "label", "positive", "negative", "operation", "content", "filename", "path"):
            self.assertNotIn(forbidden, source.lower())

    def test_selector_uses_no_active_page(self):
        selected, _ = s.select(self.universe + [candidate(999, 0, 999, active=True)], "S3", "Q_BALANCED")
        self.assertTrue(all(not row["current_active"] for row in selected))

    def test_file_identity_is_only_tie_break(self):
        rows = s.assign_hybrid_strata(self.universe)
        self.assertTrue(all("identity" in row for row in rows))
        selected, audit = s.select(rows, "S3", "Q_BALANCED")
        self.assertEqual(audit["stable_sort_contract"], "generation_age_recency_inactivity_identity")


if __name__ == "__main__":
    unittest.main()
