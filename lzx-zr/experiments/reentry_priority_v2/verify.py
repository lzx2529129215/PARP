from common import *
import torch
from model import OrdinalReentry
from priority import risk_coldness,map_priority

def main():
 audit={}
 for split in ('train','val','test'):
  p=OUT/'dataset'/split;d={f.stem:np.load(f,mmap_mode='r') for f in p.glob('*.npy')};ep=d['episode'];q,a=np.nonzero(d['eligible']);ids=ep[q,a];ne=len(d['episode_keys']);s=np.bincount(ids,weights=d['weights'][q,a],minlength=ne);assert np.allclose(s,1,atol=1e-6)
  observed=~d['censored'][q,a];lo=np.full(ne,np.inf);hi=np.full(ne,-np.inf);end=d['query_time'][q]+d['remaining'][q,a];np.minimum.at(lo,ids[observed],end[observed]);np.maximum.at(hi,ids[observed],end[observed]);active=np.isfinite(lo);assert np.allclose(lo[active],hi[active],atol=1e-5,rtol=0)
  observed_ids=np.unique(ids[observed]);censored_ids=np.unique(ids[~observed]);assert not len(np.intersect1d(observed_ids,censored_ids));assert np.all(d['remaining'][q[observed],a[observed]]>0)
  audit[split]=dict(episodes=ne,weight_sum_max_error=float(np.max(np.abs(s-1))),observed=len(observed_ids),censored=len(censored_ids),episode_endpoint_max_error=float(np.max(hi[active]-lo[active])))
 if all((OUT/m/'checkpoint.pt').exists() for m in CFG['sampling_modes']):
  torch.set_num_threads(1);data=compact('test');ix=np.arange(min(128,len(data['query_time'])));check={}
  for mode in CFG['sampling_modes']:
   c=torch.load(OUT/mode/'checkpoint.pt',map_location='cpu',weights_only=False);m=OrdinalReentry(thresholds_s=c['thresholds_s'],**c['model_args']);m.load_state_dict(c['state_dict']);m.eval()
   with torch.no_grad():s=m(**batch(data,ix)).sigmoid().numpy()
   assert np.isfinite(s).all() and np.all(np.diff(s,axis=-1)<=1e-6);r=risk_coldness(s,TH);assert np.all(r['risk30']<=r['risk180']+1e-6)
   if (OUT/mode/'survival-test.npy').exists():err=float(np.max(np.abs(s-np.load(OUT/mode/'survival-test.npy',mmap_mode='r')[ix])));assert err<2e-5
   else:err=None
   check[mode]=dict(checkpoint_epoch=c['epoch'],monotonic=True,replay_max_error=err)
  from models.app_lstm_visit_explicit import AppLSTMVisitExplicit
  from models.app_lstm_visit_window import visit_probabilities
  from models.segmented import LSTMSegmentedReentry
  c0=torch.load(BASE/'baseline/M0/checkpoint.pt',map_location='cpu',weights_only=False);m0=AppLSTMVisitExplicit(**c0['model_args']);m0.load_state_dict(c0['model_state_dict']);m0.eval()
  c1=torch.load(BASE/'checkpoints/M1.pt',map_location='cpu',weights_only=False);m1=LSTMSegmentedReentry(num_segments=8,**c1['model_args']);m1.load_state_dict(c1['state_dict']);m1.eval()
  with torch.no_grad():p0=visit_probabilities(m0(**batch(data,ix))).numpy();p1=m1(**batch(data,ix)).softmax(-1).numpy()
  original=data['query_indices'][ix];e0=float(np.max(np.abs(p0-np.load(BASE/'predictions/M0-test-probabilities.npy',mmap_mode='r')[original])));e1=float(np.max(np.abs(p1-np.load(BASE/'predictions/M1-test-probabilities.npy',mmap_mode='r')[original])));assert max(e0,e1)<2e-5
  check['baseline_replay']={'p180_max_error':e0,'M1_max_error':e1}
  audit['checkpoints']=check
 write(OUT/'verification.json',dict(status='PASS',checks=audit));print(json.dumps(audit),flush=True)
if __name__=='__main__':main()
