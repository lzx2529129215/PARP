from common import *
from model import OrdinalReentry
from priority import risk_coldness,map_priority
from offline_eval.metrics import direction,query_metrics
import torch,collections,itertools,time
NAMES=['B0 Recency','B1 p180','B2 flat M1','B3 M2 RankOnly','B4 M2 RiskAwareRank']
METRICS=['cp1','cp3','dvr30','dvr180','victim_time','evr2','evr4']

def numeric(v):return float(v) if np.isfinite(v) else None

def metric_rows(d,scores):
 n=len(d['query_time']);values=np.full((n,len(scores),len(METRICS)),np.nan);nums=np.zeros((n,len(scores)));dens=np.zeros(n);ties=np.zeros((n,len(scores)));groups={}
 ep=d['episode'];eligible=d['eligible'];counts=eligible.sum(1)
 for qi in np.flatnonzero(counts>=2):
  cand=np.flatnonzero(eligible[qi]);rem=d['remaining'][qi];cens=d['censored'][qi];obs=d['observed_s'][qi]
  pairs=[]
  for a,b in itertools.combinations(cand,2):
   sign=direction(a,b,rem,cens,obs)
   if sign is None:continue
   key=(int(ep[qi,a]),int(ep[qi,b]));cor=np.array([float(np.sign(s[qi,a]-s[qi,b])==sign) if s[qi,a]!=s[qi,b] else .5 for s in scores]);nums[qi]+=cor;dens[qi]+=1;ties[qi]+=[s[qi,a]==s[qi,b] for s in scores]
   if key not in groups:groups[key]=[int(d['user'][qi]),0,np.zeros(len(scores))]
   groups[key][1]+=1;groups[key][2]+=cor
  rows=query_metrics(cand,[s[qi] for s in scores],rem,cens,obs)
  for mi,r in enumerate(rows):
   # New risk convention is T<=h, matching P(T<=h), including exact edges.
   ranked=sorted(cand,key=lambda a:(-float(scores[mi][qi,a]),int(a)));v=ranked[0]
   for h in (30,180):r[f'dvr{h}']=float(rem[v]<=h) if not cens[v] else (0. if obs>=h else np.nan)
   values[qi,mi]=[r[k] for k in METRICS]
 # Non-POA headlines: exactly one causal, first available decision per focal episode.
 ne=int(ep.max())+1;anchor=np.full(ne,n,np.int64);qi,app=np.nonzero(eligible);np.minimum.at(anchor,ep[qi,app],qi);anchor=anchor[anchor<n];anchor=anchor[counts[anchor]>=2]
 pair_user=np.array([v[0] for v in groups.values()]);pair_values=np.array([v[2]/v[1] for v in groups.values()]);report={}
 for mi,name in enumerate(NAMES[:len(scores)]):
  r={'episode_poa':numeric(pair_values[:,mi].mean()) if len(pair_values) else None,'unique_episode_pairs':len(groups),'episode_decisions':len(anchor),'first_anchor_poa':numeric(np.mean(nums[anchor,mi][dens[anchor]>0]/dens[anchor][dens[anchor]>0])),'query_row_poa':numeric(nums[:,mi].sum()/dens.sum()),'query_comparable_pairs':int(dens.sum()),'bin_or_score_tie_fraction':numeric(ties[:,mi].sum()/dens.sum())}
  for k in METRICS:
   a=values[anchor,mi,METRICS.index(k)];a=a[np.isfinite(a)];r[k]={'n':len(a),'value':numeric(np.median(a) if k in ('victim_time','evr2','evr4') else a.mean()) if len(a) else None}
  report[name]=r
 return report,dict(values=values,anchor=anchor,pair_values=pair_values,pair_user=pair_user,anchor_user=d['user'][anchor],anchor_poa=np.divide(nums[anchor],dens[anchor,None],out=np.full_like(nums[anchor],np.nan),where=dens[anchor,None]>0))

def bootstrap(a,user,median=False):
 # Both columns already restricted to common known support; cluster by user.
 if not len(a):return dict(n=0,delta=None,ci95=[None,None])
 point=np.median(a,axis=0) if median else a.mean(0);users,inv=np.unique(user,return_inverse=True);rng=np.random.default_rng(CFG['gate']['bootstrap_seed']);reps=CFG['gate']['bootstrap_replicates'];deltas=[]
 if not median:
  sums=np.zeros((len(users),2));np.add.at(sums,inv,a);counts=np.bincount(inv)
  for _ in range(reps):
   ids=rng.integers(len(users),size=len(users));m=sums[ids].sum(0)/counts[ids].sum();deltas.append(m[1]-m[0])
 else:
  orders=[np.argsort(a[:,m]) for m in range(2)]
  for _ in range(reps):
   w=np.bincount(rng.integers(len(users),size=len(users)),minlength=len(users));med=[]
   for m in range(2):
    order=orders[m];cw=np.cumsum(w[inv[order]]);tot=cw[-1];lo=np.searchsorted(cw,(tot+1)//2);hi=np.searchsorted(cw,(tot+2)//2);med.append((a[order[lo],m]+a[order[hi],m])/2)
   deltas.append(med[1]-med[0])
 return dict(n=len(a),users=len(users),baseline=float(point[0]),m2=float(point[1]),delta=float(point[1]-point[0]),ci95=np.quantile(deltas,[.025,.975]).tolist())

def gate_for(result,mi):
 checks={};a=result['pair_values'][:,[1,mi]];checks['episode_poa']=bootstrap(a,result['pair_user']);a=result['anchor_poa'][:,[1,mi]];good=np.isfinite(a).all(1);checks['first_anchor_poa']=bootstrap(a[good],result['anchor_user'][good])
 for k in METRICS:
  a=result['values'][result['anchor']][:,[1,mi],METRICS.index(k)];good=np.isfinite(a).all(1);checks[k]=bootstrap(a[good],result['anchor_user'][good],k in ('victim_time','evr2','evr4'))
 improves=lambda k:checks[k]['ci95'][0] is not None and checks[k]['ci95'][0]>0
 nonworse=lambda k:checks[k]['ci95'][1] is not None and checks[k]['delta']<=0 and checks[k]['ci95'][1]<=0
 conditions=dict(poa=improves('episode_poa'),dvr30=nonworse('dvr30'),dvr180=nonworse('dvr180'),other=any(improves(k) for k in ('cp1','cp3','victim_time')),no_periodic_dependence=improves('first_anchor_poa'))
 return dict(status='PASS' if all(conditions.values()) else 'FAIL',conditions=conditions,checks=checks)

def main():
 torch.set_num_threads(CFG['threads']);d=compact('test');qi=d['query_indices'];m0=np.load(BASE/'predictions/M0-test-probabilities.npy',mmap_mode='r')[qi];m1=np.load(BASE/'predictions/M1-test-probabilities.npy',mmap_mode='r')[qi];baseline=[d['recency'],1-m0[:,:,1].astype(float),m1@np.arange(8)];allreports={};gates={};bs=CFG['batch_size']
 for mode in CFG['sampling_modes']:
  c=torch.load(OUT/mode/'checkpoint.pt',map_location='cpu',weights_only=False);model=OrdinalReentry(thresholds_s=c['thresholds_s'],**c['model_args']);model.load_state_dict(c['state_dict']);model.eval();survival=np.empty((len(qi),len(VOCAB),len(TH)),np.float32)
  with torch.no_grad():
   for begin in range(0,len(qi),bs):
    ix=np.arange(begin,min(len(qi),begin+bs));survival[ix]=model(**batch(d,ix)).sigmoid().numpy()
  np.save(OUT/mode/'survival-test.npy',survival);r=risk_coldness(survival,TH);bins=[map_priority(r['coldness'],d['eligible'],r['risk30'],r['risk180'],kind) for kind in ('RankOnly','RiskAwareRank')]
  np.savez_compressed(OUT/mode/'risk-priority-test.npz',**r,rank_bins=bins[0],risk_bins=bins[1]);report,result=metric_rows(d,baseline+bins);allreports[mode]=report;gates[mode]={name:gate_for(result,mi) for mi,name in [(3,'RankOnly'),(4,'RiskAwareRank')]}
  np.savez_compressed(OUT/mode/'episode-metric-support.npz',**result);write(OUT/mode/'offline.json',report);write(OUT/mode/'gate.json',gates[mode]);print(mode,json.dumps(report),flush=True)
 passed=[name for name in ('RankOnly','RiskAwareRank') if all(gates[m][name]['status']=='PASS' for m in CFG['sampling_modes'])]
 write(OUT/'offline.json',allreports);write(OUT/'gate2-offline-checks.json',dict(status='PASS' if passed else 'FAIL',passing_mappers=passed,definition=CFG['gate'],sampling_gates=gates,action='STOP; runtime/kernel not modified'))
if __name__=='__main__':main()
