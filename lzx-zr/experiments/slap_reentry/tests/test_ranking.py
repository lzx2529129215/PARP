import sys,unittest
from pathlib import Path
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from offline_eval.metrics import direction,query_metrics,earliest,bootstrap_median_difference,bootstrap_ratio


class RankingTests(unittest.TestCase):
    def test_censored_order(self):
        r=np.array([20.,np.nan,150.]);c=np.array([False,True,False])
        self.assertEqual(direction(1,0,r,c,100),1)
        self.assertIsNone(direction(1,2,r,c,100))
        self.assertEqual(earliest([0,1],r,c,100),20)
        self.assertTrue(np.isnan(earliest([1,2],r,c,100)))

    def test_perfect_reverse_and_ties(self):
        r=np.array([10.,100.,200.,500.]);c=np.zeros(4,bool)
        x=query_metrics(list(range(4)),[r,-r,np.zeros(4)],r,c,1000)
        self.assertEqual(x[0]['poa_numerator'],6);self.assertEqual(x[1]['poa_numerator'],0)
        self.assertEqual(x[2]['score_tied_pairs'],6)
        self.assertEqual(x[0]['cp3'],1);self.assertEqual(x[0]['dvr180'],0)
        self.assertEqual(x[1]['dvr30'],1);self.assertEqual(x[0]['evr2'],200)

    def test_single_candidate_and_strict_deadline(self):
        x=query_metrics([0],[np.array([1.])],np.array([30.]),np.array([False]),100)[0]
        self.assertEqual(x['poa_denominator'],0);self.assertTrue(np.isnan(x['cp1']));self.assertTrue(np.isnan(x['evr2']))
        self.assertEqual(x['dvr30'],0)

    def test_unknown_not_safe(self):
        x=query_metrics([0],[np.array([1.])],np.array([np.nan]),np.array([True]),20)[0]
        self.assertTrue(np.isnan(x['dvr30']));self.assertTrue(np.isnan(x['victim_time']))

    def test_identifiable_censored_topk_set(self):
        r=np.array([10.,100.,np.nan,np.nan]);c=np.array([False,False,True,True])
        x=query_metrics([0,1,2,3],[np.array([0.,1.,2.,3.])],r,c,200)[0]
        self.assertTrue(np.isnan(x['cp1']))
        self.assertEqual(x['cp2'],1.)
        self.assertEqual(x['cp3'],1.)

    def test_paired_user_bootstrap_shift_and_identical_methods(self):
        user=np.array([0,0,1,1,2,2]);x=np.array([1.,2.,10.,20.,50.,100.])
        result=bootstrap_median_difference(np.column_stack([x,x+7]),user,50)
        self.assertEqual(result['median_difference'],7.)
        np.testing.assert_allclose(result['ci95'],[7.,7.])
        n=np.column_stack([x,x]);d=np.ones_like(n)*100
        np.testing.assert_allclose(bootstrap_ratio(n,d,user,50)['ci95'],[0.,0.])


if __name__=='__main__':unittest.main()
