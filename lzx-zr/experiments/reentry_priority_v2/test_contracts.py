import unittest,numpy as np,torch
from build_dataset import labels
from model import OrdinalReentry,episode_bce
from priority import risk_coldness,map_priority
from train import selected_weights
class Contracts(unittest.TestCase):
 def test_observed_47(self):
  y,m=labels(np.array([[47.]]),np.array([[False]]),np.array([[47.]]));np.testing.assert_array_equal(y[0,0],[1,1,1,0,0]);self.assertTrue(m.all())
 def test_censored_75(self):
  y,m=labels(np.array([[np.nan]]),np.array([[True]]),np.array([[75.]]));np.testing.assert_array_equal(m[0,0],[1,1,1,1,0]);self.assertTrue((y[m]==1).all())
 def test_exact_threshold(self):
  y,m=labels(np.array([[30.]]),np.array([[False]]),np.array([[30.]]));self.assertEqual(y[0,0,2],0)
 def test_masked_gradient(self):
  z=torch.zeros((1,1,5),requires_grad=True);y=torch.ones_like(z);m=torch.tensor([[[1,1,1,1,0]]],dtype=torch.bool);episode_bce(z,y,m,torch.ones((1,1))).backward();self.assertEqual(z.grad[0,0,4],0)
 def test_sampler(self):
  d={'weights':np.array([[.5,0],[.5,1]],np.float32),'episode':np.array([[0,-1],[0,1]])};w=selected_weights(d,'one_query_per_episode',np.random.default_rng(1));self.assertEqual(w[:,0].sum(),1);self.assertEqual(w[:,1].sum(),1)
 def test_replication_invariance(self):
  z=torch.tensor([[[1.,0.,-1.,-2.,-3.]]]);y=torch.ones_like(z);m=torch.ones_like(z,dtype=torch.bool)
  a=episode_bce(z,y,m,torch.ones((1,1)));b=episode_bce(z.repeat(7,1,1),y.repeat(7,1,1),m.repeat(7,1,1),torch.ones((7,1))/7);torch.testing.assert_close(a,b)
 def test_priority_decoupled(self):
  for thresholds in ([3,10,30,60,180],[1,3,10,30,60,120,180]):
   s=np.broadcast_to(np.linspace(.9,.1,len(thresholds)),(2,3,len(thresholds)));r=risk_coldness(s,thresholds);bins=map_priority(r['coldness'],np.ones((2,3),bool));self.assertEqual(bins.shape,(2,3));self.assertTrue(((bins>=0)&(bins<8)).all())
 def test_risk_protection(self):
  v=np.array([[.9,.1,.5]]);e=np.ones_like(v,bool);b=map_priority(v,e,np.array([[.8,.1,.1]]),np.array([[.9,.2,.3]]),'RiskAwareRank');self.assertLess(b[0,0],b[0,1]);self.assertLess(b[0,0],b[0,2])
 def test_monotone_head(self):
  m=OrdinalReentry(num_apps=32,num_user_groups=1,pad_id=30);f=dict(history_apps=torch.zeros((2,20),dtype=torch.long),history_durations=torch.ones((2,20)),history_mask=torch.ones((2,20),dtype=torch.bool),opened_apps=torch.ones((2,32)),current_app=torch.zeros(2,dtype=torch.long),time_feature=torch.zeros((2,3)),user_group=torch.zeros(2,dtype=torch.long),explicit=torch.zeros((2,32,14)));z=m(**f);self.assertEqual(z.shape,(2,32,5));self.assertTrue((torch.diff(z,dim=-1)<=0).all())
 def test_all_masked_queries_remain_in_sampler(self):
  d={'weights':np.array([[.5],[.5]],np.float32),'episode':np.array([[0],[0]]),'mask':np.array([[[True]*5],[[False]*5]])}
  chosen={int(np.argmax(selected_weights(d,'one_query_per_episode',np.random.default_rng(seed)))) for seed in range(20)};self.assertEqual(chosen,{0,1})
 def test_episode_metrics_ignore_replication(self):
  from evaluate import metric_rows
  d={'query_time':np.array([10.]),'episode':np.array([[0,1]]),'eligible':np.array([[True,True]]),'remaining':np.array([[20.,40.]]),'censored':np.array([[False,False]]),'observed_s':np.array([50.]),'user':np.array([1])}
  scores=[np.array([[0.,1.]]),np.array([[1.,0.]]),np.array([[0.,0.]])];r,_=metric_rows(d,scores)
  d2={k:np.repeat(v,9,axis=0) for k,v in d.items()};rr,_=metric_rows(d2,[np.repeat(v,9,axis=0) for v in scores])
  for name in r:self.assertEqual(r[name]['episode_poa'],rr[name]['episode_poa']);self.assertEqual(r[name]['episode_decisions'],rr[name]['episode_decisions'])
 def test_encoder_initialization_preserved(self):
  from models.app_lstm_visit_explicit import AppLSTMVisitExplicit
  args=dict(num_apps=32,num_user_groups=1,pad_id=30)
  torch.manual_seed(42);old=AppLSTMVisitExplicit(**args);torch.manual_seed(42);new=OrdinalReentry(**args)
  for name,value in old.state_dict().items():
   if not name.startswith(('output.','explicit_head.2.')):torch.testing.assert_close(value,new.state_dict()[name],rtol=0,atol=0)
 def test_rank_mapper_order_and_ties(self):
  cold=np.array([[.1,.1,.4,.7,.9]]);bins=map_priority(cold,np.ones_like(cold,bool));self.assertEqual(bins[0,0],bins[0,1]);self.assertTrue((np.diff(bins[0])>=0).all());self.assertEqual(bins[0,-1],7)
 def test_dvr_boundary_is_inclusive(self):
  from evaluate import metric_rows
  d={'query_time':np.array([0.]),'episode':np.array([[0,1]]),'eligible':np.array([[True,True]]),'remaining':np.array([[30.,100.]]),'censored':np.array([[False,False]]),'observed_s':np.array([110.]),'user':np.array([1])}
  r,_=metric_rows(d,[np.array([[1.,0.]])]);self.assertEqual(r['B0 Recency']['dvr30']['value'],1.)
 def test_insufficient_censor_is_not_safe(self):
  from evaluate import metric_rows
  d={'query_time':np.array([0.]),'episode':np.array([[0,1]]),'eligible':np.array([[True,True]]),'remaining':np.array([[np.nan,47.]]),'censored':np.array([[True,False]]),'observed_s':np.array([75.]),'user':np.array([1])}
  r,_=metric_rows(d,[np.array([[1.,0.]])]);self.assertEqual(r['B0 Recency']['dvr30']['value'],0.);self.assertIsNone(r['B0 Recency']['dvr180']['value'])
if __name__=='__main__':torch.set_num_threads(1);unittest.main()
