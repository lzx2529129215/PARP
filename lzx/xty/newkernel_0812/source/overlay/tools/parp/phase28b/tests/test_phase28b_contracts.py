import unittest

from phase28b.contracts import (
    WindowKey, align_start, assign_window, causal_slice, classify_quality,
    cumulative_future, enforce_monotonic, feature_source_complete,
    future_available, label_pair_inventory, no_forbidden_features,
    normalize_refault_proxy, operation_split, partition_valid,
    raw_hash_equal, stable_topk, summarize_other, topk_conserves,
    version_valid,
)
from phase28b.segments import average_precision


class InventoryTests(unittest.TestCase):
    def test_01_five_sessions(self):
        self.assertEqual(len(operation_split()), 5)

    def test_02_189_repeats(self):
        self.assertTrue(label_pair_inventory(189, 189, 189))

    def test_03_start_done_pair(self):
        self.assertFalse(label_pair_inventory(2, 2, 1))


class WindowTests(unittest.TestCase):
    def test_04_2s_deterministic(self):
        self.assertEqual(align_start(5_900_000_000, 2), 4_000_000_000)

    def test_05_5s_deterministic(self):
        self.assertEqual(align_start(9_999_999_999, 5), 5_000_000_000)

    def test_06_10s_deterministic(self):
        self.assertEqual(align_start(19_000_000_000, 10), 10_000_000_000)

    def test_07_no_cross_session(self):
        a=WindowKey('a',1,1,0,2); b=WindowKey('b',1,1,0,2)
        self.assertNotEqual(a,b)

    def test_08_no_future(self):
        self.assertEqual(causal_slice([1,2,3,4],2,2),[2,3])

    def test_09_assign_boundary(self):
        self.assertEqual(assign_window(10_000_000_000,10),(10_000_000_000,20_000_000_000))


class VectorTests(unittest.TestCase):
    def setUp(self):
        self.rows=[{'key':'b','score':1,'weighted':2,'active':3},{'key':'a','score':1,'weighted':2,'active':3},{'key':'c','score':0,'weighted':1,'active':1}]

    def test_10_topk_stable(self):
        self.assertEqual([x['key'] for x in stable_topk(self.rows,2)],['a','b'])

    def test_11_other_conservation(self):
        top=stable_topk(self.rows,1); other=summarize_other(self.rows,top)
        self.assertTrue(topk_conserves(self.rows,top,other))

    def test_12_fileid_not_feature(self):
        self.assertFalse(no_forbidden_features(['file_id_hash']))

    def test_13_path_not_feature(self):
        self.assertFalse(no_forbidden_features(['file_path']))

    def test_14_operation_not_feature(self):
        self.assertFalse(no_forbidden_features(['operation_probability']))

    def test_15_repeat_not_feature(self):
        self.assertFalse(no_forbidden_features(['repeat_id']))

    def test_16_source_complete(self):
        self.assertTrue(feature_source_complete(['x'],{'x':{'source_type':'PARP_FILE'}}))


class SplitAndLabelTests(unittest.TestCase):
    def test_17_scaler_train_only(self):
        self.assertEqual(operation_split()['wps_01'],'train')

    def test_18_test_not_selection(self):
        self.assertEqual(operation_split()['wps_03'],'test')

    def test_19_quality(self):
        self.assertEqual([classify_quality(x) for x in (.9,.6,.2)],['PURE','MIXED','LOW_CONFIDENCE'])

    def test_20_pattern_not_operation(self):
        self.assertFalse(no_forbidden_features(['action_pattern']))

    def test_21_next_online_no_truth(self):
        self.assertTrue(no_forbidden_features(['predicted_transition_probability']))

    def test_22_segment_no_truth(self):
        self.assertTrue(no_forbidden_features(['kernel_embedding']))


class FutureTests(unittest.TestCase):
    def test_23_future_availability(self):
        self.assertFalse(future_available(8,10,3))

    def test_24_not_observed_not_negative(self):
        self.assertIsNone(cumulative_future([None,None],0,1)[0])

    def test_25_probability_monotonic(self):
        self.assertEqual(enforce_monotonic(.8,.2,.4),(.8,.8,.8))

    def test_26_version_invalid(self):
        self.assertFalse(version_valid(1,2))

    def test_27_partition_invalid(self):
        self.assertFalse(partition_valid(1,2))

    def test_28_online_causal(self):
        self.assertEqual(causal_slice([1,2,3],1,6),[1,2])

    def test_29_refault_normalization(self):
        self.assertEqual(normalize_refault_proxy(10,100),100.0)

    def test_30_raw_hash(self):
        self.assertFalse(raw_hash_equal({'a':'1'},{'a':'2'}))

    def test_31_ap_ties_do_not_use_truth(self):
        self.assertEqual(average_precision([False, True], [.5, .5]), .5)


if __name__ == '__main__': unittest.main()
