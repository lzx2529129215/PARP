import unittest

from phase29a.contracts import (
    block_bootstrap, candidate_allowed, candidate_hashes_equal,
    censored_target, cross_matrix_complete, feature_names_allowed,
    hashes_differ, independent_models, matched_expert, next_reuse,
    no_future_slice, oracle_allowed, oracle_rank, pairwise_target,
    partition_valid, phase28_unchanged, predicted_route_uses_oracle,
    reclaim_counts_equal, refault_comparable, report_strictly_better,
    route_with_fallback, selection_sessions, soft_weights, ttl_route,
    version_valid, wrong_expert,
)


class LeakageContracts(unittest.TestCase):
    def test_01_operation_not_workload_label(self): self.assertFalse(feature_names_allowed(["operation_kind"]))
    def test_02_operation_not_workload_feature(self): self.assertFalse(feature_names_allowed(["dominant_operation"]))
    def test_03_repeat_not_feature(self): self.assertFalse(feature_names_allowed(["repeat_id"]))
    def test_04_path_name_not_feature(self): self.assertFalse(feature_names_allowed(["file_path"]))
    def test_05_future_features_causal(self): self.assertEqual(no_future_slice([1,2,3],2),[1,2])
    def test_06_oracle_usage_allowlist(self): self.assertTrue(oracle_allowed("expert_training_target")); self.assertFalse(oracle_allowed("online_feature"))
    def test_07_predicted_route_has_no_oracle(self): self.assertFalse(predicted_route_uses_oracle({"predicted":"LOCAL_REUSE"}))


class CandidatePolicyContracts(unittest.TestCase):
    def test_08_candidate_hash_equal(self): self.assertTrue(candidate_hashes_equal({"a":"x","b":"x"}))
    def test_09_reclaim_budget_equal(self): self.assertTrue(reclaim_counts_equal([10,10,10]))
    def test_10_oracle_consumes_reuse(self): self.assertEqual(oracle_rank([None,8,2]),[2,1,0])
    def test_11_oracle_differs_from_baseline(self): self.assertTrue(hashes_differ("a","b"))
    def test_20_not_observed_excluded(self): self.assertFalse(candidate_allowed("NOT_OBSERVED",True,True))
    def test_21_invalid_version_excluded(self): self.assertFalse(version_valid("v1","v2"))
    def test_22_partition_generation(self): self.assertTrue(partition_valid("4:100","4:100"))
    def test_23_next_reuse(self): self.assertEqual(next_reuse([False,True,False],5),10)
    def test_24_censored(self): self.assertEqual(censored_target([False,None],10,30),"CENSORED_NO_REUSE_WITHIN_HORIZON")
    def test_25_pairwise_direction(self): self.assertEqual(pairwise_target(5,20),1)
    def test_26_proxy_same_reclaim(self): self.assertTrue(refault_comparable({"a":8,"b":8}))


class ExpertContracts(unittest.TestCase):
    def test_12_global_expert_independent(self): self.assertTrue(independent_models("g","e"))
    def test_13_expert_hashes_different(self): self.assertTrue(hashes_differ("1","2"))
    def test_14_cross_matrix_complete(self): self.assertTrue(cross_matrix_complete(["A","B"],{"A":{"A":1,"B":2},"B":{"A":3,"B":4}}))
    def test_15_matched_index(self): self.assertEqual(matched_expert("LOCAL_REUSE"),"EXPERT_LOCAL_REUSE")
    def test_16_wrong_expert(self): self.assertNotEqual(wrong_expert("LOCAL_REUSE",["LOCAL_REUSE","IDLE_COOLING"]),"EXPERT_LOCAL_REUSE")
    def test_17_soft_weights_sum_one(self): self.assertEqual(sum(soft_weights({"A":2,"B":1}).values()),1)
    def test_18_unknown_falls_back(self): self.assertEqual(route_with_fallback({},.8,.4),"GLOBAL_EXPERT")
    def test_19_expired_falls_back_native(self): self.assertEqual(ttl_route(11,10),"BASE_NATIVE_RECENCY")


class ValidationContracts(unittest.TestCase):
    def test_27_block_bootstrap_deterministic(self): self.assertEqual(block_bootstrap({"s":[1,2]},50,29),block_bootstrap({"s":[1,2]},50,29))
    def test_28_test_not_selection(self): self.assertNotIn("wps_03",selection_sessions())
    def test_29_online_has_no_future(self): self.assertEqual(no_future_slice([1,2,3],1),[1])
    def test_30_raw_hash_equal(self): self.assertTrue(phase28_unchanged("x","x"))
    def test_31_phase28_output_unchanged(self): self.assertFalse(phase28_unchanged("x","y"))
    def test_32_equal_not_better(self): self.assertFalse(report_strictly_better(1.0,1.0,lower_is_better=True))


if __name__ == "__main__": unittest.main()
