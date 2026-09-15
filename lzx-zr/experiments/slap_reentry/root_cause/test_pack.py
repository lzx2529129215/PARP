import unittest
from common import *
from audit import episodes
from offline_eval.metrics import query_metrics

class PackTests(unittest.TestCase):
    def test_episode_dedup_and_censored_separation(self):
        d={'eligible':np.ones((4,1),bool),'censored':np.array([[0],[0],[0],[1]],bool),'query_time':np.array([0.,30.,60.,90.]),'remaining':np.array([[120.],[90.],[60.],[np.nan]]),'observed_s':np.array([150.,120.,90.,60.]),'session':np.array([1]*4),'user':np.array([7]*4)}
        r=episodes(d,np.ones(4,bool),{'A':0});self.assertEqual(r['unique_reentry_episodes'],1);self.assertEqual(r['observed_rows_per_episode'],3);self.assertEqual(r['censored_groups_not_observed_episodes'],1)
        d['session'][2]=2;r=episodes(d,np.ones(4,bool),{'A':0});self.assertEqual(r['unique_reentry_episodes'],2)
    def test_same_support_argmax_is_not_probability_mass(self):
        p=np.tile([.45,.55],(100,1));self.assertEqual((p.argmax(1)==1).mean(),1);self.assertAlmostEqual(p[:,1].mean(),.55)
    def test_safety_and_rank_can_disagree(self):
        remaining=np.array([10.,100.,200.,300.,400.]);good=query_metrics(range(5),[np.array([0.,4.,3.,2.,1.]),np.array([5.,1.,2.,3.,4.])],remaining,np.zeros(5,bool),500)
        self.assertGreater(good[1]['poa_numerator'],good[0]['poa_numerator']);self.assertGreater(good[1]['dvr30'],good[0]['dvr30'])
    def test_training_and_holdout_boundaries(self):
        manifest=json.loads((OUT/'HOLDOUT-MANIFEST.json').read_text());sets=[set(x['session_ids']) for x in manifest['partitions'].values()]
        for i,a in enumerate(sets):
            for b in sets[i+1:]:self.assertFalse(a&b)
        self.assertEqual(manifest['final_holdout']['existing_clean_sessions'],0)
if __name__=='__main__':unittest.main()
