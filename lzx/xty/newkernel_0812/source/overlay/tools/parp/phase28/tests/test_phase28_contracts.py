import tempfile
import unittest
from pathlib import Path

from phase28.contracts import validate_feature_names, validate_online_input, validate_source_map
from phase28.core import (ApplyGuard, causal_history, cumulative_labels,
                          monotonic_probabilities, normalized_refault,
                          reclaim_comparable, stable_topk, summarize_other,
                          version_prediction_valid)
from phase28.patterns import classify_kernel_pattern
from phase28.provenance import hash_paths


class LeakageTests(unittest.TestCase):
    def test_operation_truth_rejected(self):
        with self.assertRaises(ValueError): validate_feature_names(["dominant_operation"])

    def test_automation_rejected(self):
        with self.assertRaises(ValueError): validate_feature_names(["automation_phase"])

    def test_path_and_filename_rejected(self):
        for name in ("absolute_path", "document_filename"):
            with self.assertRaises(ValueError): validate_feature_names([name])

    def test_identity_not_numeric_feature(self):
        for name in ("file_id", "inode", "dev_major"):
            with self.assertRaises(ValueError): validate_feature_names([name])

    def test_app_is_only_upper_input(self):
        self.assertTrue(validate_online_input({"foreground_app_id": 1,
            "kernel_features": {"anon_hot_ratio": .2}, "past_predictions": []}))
        with self.assertRaises(ValueError): validate_online_input({"foreground_app_id": 1,
            "kernel_features": {}, "past_predictions": [], "window_title": "x"})

    def test_source_map_complete(self):
        names=["foreground_app_id","anon_hot_ratio"]
        mapping={"foreground_app_id":{"source_type":"UPPER_LAYER_APP_ID"},
                 "anon_hot_ratio":{"source_type":"PARP_ANON"}}
        self.assertTrue(validate_source_map(names,mapping))
        del mapping["anon_hot_ratio"]
        with self.assertRaises(ValueError): validate_source_map(names,mapping)

    def test_current_truth_not_online(self):
        with self.assertRaises(ValueError): validate_online_input({"foreground_app_id":1,
            "kernel_features":{},"past_predictions":[],"current_operation":"EDIT"})

    def test_next_truth_not_online(self):
        with self.assertRaises(ValueError): validate_online_input({"foreground_app_id":1,
            "kernel_features":{},"past_predictions":[],"next_operation":"SAVE"})

    def test_future_kernel_not_online(self):
        with self.assertRaises(ValueError): validate_online_input({"foreground_app_id":1,
            "kernel_features":{},"past_predictions":[],"future_kernel_state":{}})


class PageAndHistoryTests(unittest.TestCase):
    FILES=[{"key":"b","score":1,"weighted":2,"active":3},
           {"key":"a","score":1,"weighted":2,"active":3},
           {"key":"c","score":0,"weighted":1,"active":1}]

    def test_topk_stable(self):
        self.assertEqual([x["key"] for x in stable_topk(self.FILES,2)],["a","b"])
        self.assertEqual(stable_topk(self.FILES,2),stable_topk(list(reversed(self.FILES)),2))

    def test_other_conservation(self):
        top=stable_topk(self.FILES,1); other=summarize_other(self.FILES,top)
        self.assertEqual(other["active"]+sum(x["active"] for x in top),7)
        self.assertEqual(other["count"]+len(top),3)

    def test_history_does_not_cross_session(self):
        rows=[{"session":"a","x":1},{"session":"a","x":2},{"session":"b","x":3}]
        self.assertEqual(causal_history(rows,2,6),[rows[2]])

    def test_history_does_not_read_future(self):
        rows=[{"session":"a","x":x} for x in range(5)]
        self.assertEqual([x["x"] for x in causal_history(rows,2,3)],[0,1,2])

    def test_future_label_availability(self):
        labels=cumulative_labels({1:True,2:False,3:True,4:False,5:False,6:False},0,6)
        self.assertTrue(labels[10]["available"]); self.assertTrue(labels[30]["active"])
        self.assertFalse(cumulative_labels({},6,6)[10]["available"])

    def test_probability_monotonicity(self):
        self.assertEqual(monotonic_probabilities(.8,.2,.5),(.8,.8,.8))

    def test_version_mismatch_invalidates_prediction(self):
        self.assertFalse(version_prediction_valid(1,2,1,1)); self.assertTrue(version_prediction_valid(2,2,1,1))

    def test_expired_prediction_falls_back_unknown(self):
        guard=ApplyGuard(7,100,.1)
        self.assertEqual(guard.evaluate(7,101,100,10,False),"EXPIRED")


class PatternAndSafetyTests(unittest.TestCase):
    def test_pattern_is_not_operation_alias(self):
        result=classify_kernel_pattern({"centroid_shift":2,"continuity":.9,
                                        "working_set_delta":0,"write_burst":0})
        self.assertEqual(result,"SEQUENTIAL_FORWARD")
        with self.assertRaises(TypeError): classify_kernel_pattern({}, operation="EDIT")

    def test_normalized_refault(self):
        self.assertEqual(normalized_refault(5,1000),5.0)
        self.assertIsNone(normalized_refault(5,0))

    def test_reclaim_comparability(self):
        self.assertTrue(reclaim_comparable(1000,950,.1)); self.assertFalse(reclaim_comparable(1000,700,.1))

    def test_apply_only_target_domain(self):
        guard=ApplyGuard(7,100,.1)
        self.assertEqual(guard.evaluate(8,10,100,5,True),"DOMAIN_MISMATCH")

    def test_apply_budget(self):
        guard=ApplyGuard(7,100,.1)
        self.assertEqual(guard.evaluate(7,10,100,11,True),"BUDGET_EXCEEDED")

    def test_circuit_breaker(self):
        guard=ApplyGuard(7,100,.1); guard.trip("PSI")
        self.assertEqual(guard.evaluate(7,10,100,1,True),"CIRCUIT_BREAKER")

    def test_observe_never_applies(self):
        guard=ApplyGuard(7,100,.1)
        self.assertEqual(guard.evaluate(7,10,100,1,False),"OBSERVE_ONLY")

    def test_raw_hash_mutation_detected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary); path=root/"raw"; path.write_bytes(b"a")
            before=hash_paths(root,[path]); path.write_bytes(b"b")
            self.assertNotEqual(before,hash_paths(root,[path]))


if __name__ == "__main__": unittest.main()
