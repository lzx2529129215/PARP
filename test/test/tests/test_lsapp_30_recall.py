import sys
import unittest
from pathlib import Path
import numpy as np
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from evaluate_lsapp_30_recall import policy, threshold_for_recall


class RecallPolicyTests(unittest.TestCase):
    def data(self):
        return {'probabilities': np.array([[[.1,.15],[.2,.3],[.8,.9]],[[.1,.15],[.4,.5],[.9,.95]]]),
                'labels': np.array([[[1,1],[1,1],[0,0]],[[1,1],[0,0],[1,1]]]),
                'valid': np.ones((2,3,2)), 'eligible': np.array([[True,True,False],[True,True,False]])}

    def test_threshold_ties_and_validation_positive_recall(self):
        d=self.data()
        for target in (.5,.9,.95,.99):
            t=threshold_for_recall(d,target)
            self.assertGreaterEqual(policy(d,t)['hot_recall'],target)
        self.assertEqual(threshold_for_recall(d,.5),.1)

    def test_unknown_excluded_and_hot_precedes_cold(self):
        d=self.data(); d['valid'][1,0,:]=0
        r=policy(d,.1)
        self.assertEqual(r['hot_selected'],4)
        self.assertEqual(r['hot_valid_selected'],3)
        self.assertEqual(r['hot_hits'],2)
        self.assertEqual(r['cold_selected'],0)
        self.assertEqual(r['hot_recall'],1)

    def test_empty_selection_has_no_precision(self):
        r=policy(self.data(),.8)
        self.assertEqual(r['hot_selected'],0)
        self.assertIsNone(r['hot_precision'])
        self.assertEqual(r['hot_recall'],0)


if __name__=='__main__': unittest.main()
