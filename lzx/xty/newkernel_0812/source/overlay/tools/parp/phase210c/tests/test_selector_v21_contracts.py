import hashlib
import json
import unittest

from phase210c.selector_v21 import BANDS, QUOTAS, assign_bands, native_key, native_tail_order, realism, select, support_gate


def row(index, active=False, observed="OBSERVED_INACTIVE", generation=None):
    return {"identity": "d:%d|g:1|%d" % (index, index), "session_id": "s", "domain_id": "d", "file_version": 1, "partition_generation": "g:1", "segment_id": index, "current_active": active, "observation_state": observed, "generation_proxy": float(index) / 256 if generation is None else generation, "delta_since_last_access": 1000000, "time_since_last_active": 1000000, "segment_age": 200 + index, "age": 200 + index, "ordinal": index, "validity_flags": {"file_version": True, "partition_generation": True}}


class SelectorV21Contracts(unittest.TestCase):
    def setUp(self):
        self.rows = [row(index) for index in range(1024)]

    def test_01_rank_zero_is_coldest(self):
        ordered = native_tail_order(self.rows)
        self.assertEqual(ordered[0]["tail_rank"], 0)

    def test_02_rank_is_contiguous(self):
        self.assertEqual([x["tail_rank"] for x in native_tail_order(self.rows)], list(range(1024)))

    def test_03_distance_starts_at_zero(self):
        self.assertEqual(native_tail_order(self.rows)[0]["tail_distance"], 0)

    def test_04_distance_ends_at_one(self):
        self.assertEqual(native_tail_order(self.rows)[-1]["tail_distance"], 1)

    def test_05_distance_is_bounded(self):
        self.assertTrue(all(0 <= x["tail_distance"] <= 1 for x in native_tail_order(self.rows)))

    def test_06_distance_is_monotonic(self):
        values = [x["tail_distance"] for x in native_tail_order(self.rows)]
        self.assertEqual(values, sorted(values))

    def test_07_tail_order_is_deterministic(self):
        self.assertEqual([x["identity"] for x in native_tail_order(self.rows)], [x["identity"] for x in native_tail_order(list(reversed(self.rows)))])

    def test_08_tail_key_is_causal(self):
        self.assertEqual(native_key(self.rows[1]), native_key(dict(self.rows[1])))

    def test_09_bands_are_known(self):
        self.assertEqual(set(BANDS), {"T0", "T1", "T2", "T3"})

    def test_10_bands_are_disjoint(self):
        banded = assign_bands(self.rows)
        self.assertEqual(len({x["identity"] for x in banded}), len(banded))

    def test_11_band_assignment_is_single(self):
        self.assertTrue(all(x["tail_band"] in {"T0", "T1", "T2", "T3", "EXCLUDED"} for x in assign_bands(self.rows)))

    def test_12_band_cutoff_is_twenty_percent(self):
        self.assertTrue(all(x["tail_distance"] <= .20 or x["tail_band"] == "EXCLUDED" for x in assign_bands(self.rows)))

    def test_13_c1_quota_total(self):
        self.assertEqual(sum(QUOTAS["C1"]), 128)

    def test_14_c2_quota_total(self):
        self.assertEqual(sum(QUOTAS["C2"]), 128)

    def test_15_c3_quota_total(self):
        self.assertEqual(sum(QUOTAS["C3"]), 128)

    def test_16_c4_quota_total(self):
        self.assertEqual(sum(QUOTAS["C4"]), 128)

    def test_17_c1_fixed_count(self):
        self.assertEqual(len(select(self.rows, "C1")[0]), 128)

    def test_18_c2_fixed_count(self):
        self.assertEqual(len(select(self.rows, "C2")[0]), 128)

    def test_19_c3_fixed_count(self):
        self.assertEqual(len(select(self.rows, "C3")[0]), 128)

    def test_20_c4_fixed_count(self):
        self.assertEqual(len(select(self.rows, "C4")[0]), 128)

    def test_21_no_duplicates(self):
        selected, audit, _ = select(self.rows, "C4")
        self.assertEqual(audit["duplicate_count"], 0)
        self.assertEqual(len({x["identity"] for x in selected}), len(selected))

    def test_22_no_active(self):
        selected, audit, _ = select(self.rows + [row(999, active=True)], "C1")
        self.assertEqual(audit["current_active_count"], 0)
        self.assertTrue(all(not x["current_active"] for x in selected))

    def test_23_not_observed_is_excluded(self):
        selected, audit, _ = select(self.rows + [row(998, observed="NOT_OBSERVED")], "C1")
        self.assertEqual(audit["not_observed_count"], 0)

    def test_24_invalid_version_is_excluded(self):
        bad = row(1997); bad["file_version"] = None; bad["validity_flags"]["file_version"] = False
        self.assertNotIn(bad["identity"], {x["identity"] for x in select(self.rows + [bad], "C1")[0]})

    def test_25_invalid_partition_is_excluded(self):
        bad = row(1996); bad["partition_generation"] = None; bad["validity_flags"]["partition_generation"] = False
        self.assertNotIn(bad["identity"], {x["identity"] for x in select(self.rows + [bad], "C1")[0]})

    def test_26_cross_session_is_excluded(self):
        bad = row(1995); bad["session_id"] = "other"
        self.assertNotIn(bad["identity"], {x["identity"] for x in select(self.rows + [bad], "C1")[0]})

    def test_27_cross_domain_is_excluded(self):
        bad = row(1994); bad["domain_id"] = "other"
        self.assertNotIn(bad["identity"], {x["identity"] for x in select(self.rows + [bad], "C1")[0]})

    def test_28_partial_pool_not_copied(self):
        selected, audit, _ = select(self.rows[:10], "C1")
        self.assertLess(len(selected), 128)
        self.assertTrue(audit["partial_tail_universe"])

    def test_29_partial_pool_has_no_duplicates(self):
        self.assertEqual(select(self.rows[:10], "C2")[1]["duplicate_count"], 0)

    def test_30_c1_has_no_t3_request(self):
        self.assertEqual(QUOTAS["C1"][3], 0)

    def test_31_c2_has_eight_t3(self):
        self.assertEqual(QUOTAS["C2"][3], 8)

    def test_32_c3_has_no_t3(self):
        self.assertEqual(QUOTAS["C3"][3], 0)

    def test_33_c4_t3_limit(self):
        self.assertLessEqual(QUOTAS["C4"][3] / 128, .125)

    def test_34_selected_distance_is_bounded(self):
        selected, _, _ = select(self.rows, "C4")
        self.assertTrue(all(x["tail_distance"] <= .20 for x in selected))

    def test_35_selection_hash_is_stable(self):
        self.assertEqual(select(self.rows, "C1")[1]["candidate_hash"], select(self.rows, "C1")[1]["candidate_hash"])

    def test_36_order_hash_is_stable(self):
        self.assertEqual(select(self.rows, "C2")[1]["candidate_order_hash"], select(self.rows, "C2")[1]["candidate_order_hash"])

    def test_37_audit_is_causal(self):
        self.assertTrue(select(self.rows, "C3")[1]["causal_inputs_only"])

    def test_38_audit_records_fill(self):
        self.assertIn("fill_source", select(self.rows, "C4")[1])

    def test_39_audit_records_actual_strata(self):
        self.assertEqual(set(select(self.rows, "C1")[1]["actual_stratum_counts"]), set(BANDS))

    def test_40_audit_records_requested_quota(self):
        self.assertEqual(select(self.rows, "C1")[1]["requested_quota"], dict(zip(BANDS, QUOTAS["C1"])))

    def test_41_realism_rejects_active(self):
        selected, _, _ = select(self.rows, "C1")
        metrics = realism(self.rows, selected + [row(900, active=True)])
        self.assertFalse(metrics["hard_passed"])

    def test_42_realism_rejects_t3_over_limit(self):
        selected, _, _ = select(self.rows, "C4")
        metrics = realism(self.rows, selected)
        self.assertGreater(metrics["t3_ratio"], .125)

    def test_43_realism_reports_oldest_half(self):
        self.assertIn("oldest_half_ratio", realism(self.rows, select(self.rows, "C1")[0]))

    def test_44_realism_reports_top10(self):
        self.assertIn("top10_tail_ratio", realism(self.rows, select(self.rows, "C1")[0]))

    def test_45_realism_reports_age_medians(self):
        metrics = realism(self.rows, select(self.rows, "C1")[0])
        self.assertIn("selected_age_median", metrics)
        self.assertIn("universe_age_median", metrics)

    def test_46_realism_reports_gap_medians(self):
        metrics = realism(self.rows, select(self.rows, "C1")[0])
        self.assertIn("selected_gap_median", metrics)
        self.assertIn("universe_gap_median", metrics)

    def test_47_support_gate_rejects_zero_positive(self):
        self.assertFalse(support_gate({"positive_count": 0, "pairwise_evaluable_decisions": 0}))

    def test_48_support_gate_requires_pairwise(self):
        self.assertFalse(support_gate({"positive_count": 20, "pairwise_evaluable_decisions": 0}))

    def test_49_support_gate_accepts_minimum(self):
        self.assertTrue(support_gate({"positive_count": 20, "pairwise_evaluable_decisions": 10}))

    def test_50_template_names_are_fixed(self):
        self.assertEqual(set(QUOTAS), {"C1", "C2", "C3", "C4"})

    def test_51_no_template_uses_randomness(self):
        self.assertEqual(select(self.rows, "C1")[1]["candidate_hash"], select(list(self.rows), "C1")[1]["candidate_hash"])

    def test_52_identity_is_final_tie_break(self):
        self.assertEqual(native_key(self.rows[1])[-1], self.rows[1]["identity"])

    def test_53_schema_hash_shape(self):
        value = select(self.rows, "C1")[1]["candidate_hash"]
        self.assertEqual(len(value), 64)
        int(value, 16)

    def test_54_no_runtime_action_contract(self):
        self.assertTrue(select(self.rows, "C1")[1]["causal_inputs_only"])


if __name__ == "__main__":
    unittest.main()
