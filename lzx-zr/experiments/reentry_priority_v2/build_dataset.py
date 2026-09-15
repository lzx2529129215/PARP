from common import *
import csv,bisect,collections,time

def labels(remaining,censored,observed):
 y=(remaining[...,None]>TH).astype(np.float32)
 mask=np.where(censored[...,None],observed[...,None]>=TH,True)
 y=np.where(censored[...,None],1.,y).astype(np.float32)
 return y,mask

def main():
 sessions=collections.defaultdict(list)
 with (BASE/'baseline/dataset/segments.csv').open() as f:
  for r in csv.DictReader(f):sessions[r['session_id']].append((stamp(r['start_time']),VOCAB[r['app']]))
 exits={};entries={}
 for sid,rows in sessions.items():
  rows.sort(key=lambda x:x[0]);per=collections.defaultdict(list);ent=collections.defaultdict(list)
  for t,a in rows:ent[a].append(t)
  entries[sid]={a:np.array(v) for a,v in ent.items()}
  for i,(t,a) in enumerate(rows[:-1]):
   if a<30 and rows[i+1][1]!=a:per[a].append(rows[i+1][0])
  exits[sid]={a:np.array(v) for a,v in per.items()}
 report={};allkeys={}
 for split in ('train','val','test'):
  d=load_data(BASE/'dataset'/split);qs,apps=np.nonzero(d['eligible']);leave=np.full(len(qs),np.nan);next_return=np.full(len(qs),np.inf);sq=np.asarray(d['session'][qs])
  # Only candidates with an actual causal departure define a reentry episode.
  for sn in np.unique(sq):
   take=np.flatnonzero(sq==sn);sid=META['session_ids'][int(sn)]
   for app in np.unique(apps[take]):
    ids=take[apps[take]==app];times=exits.get(sid,{}).get(int(app),np.array([]));pos=np.searchsorted(times,d['query_time'][qs[ids]],side='right')-1;ok=pos>=0
    ix=ids[ok];leave[ix]=times[pos[ok]]
    ent=entries[sid][int(app)];nxt=np.searchsorted(ent,leave[ix],side='right');present=nxt<len(ent);next_return[ix[present]]=ent[nxt[present]]
  no_departure=~np.isfinite(leave);stale=np.isfinite(leave)&(d['query_time'][qs]>=next_return);good=~no_departure&~stale;qs=qs[good];apps=apps[good];leave=leave[good];until=d['query_time'][qs]+d['observed_s'][qs]
  keys=np.column_stack([d['session'][qs],apps,np.rint(leave*1e6).astype(np.int64),np.rint(until*1e6).astype(np.int64)]).astype(np.int64)
  unique,episode,counts=np.unique(keys,axis=0,return_inverse=True,return_counts=True)
  qi,local=np.unique(qs,return_inverse=True);N=len(qi);A=len(VOCAB);ep=np.full((N,A),-1,dtype=np.int32);ep[local,apps]=episode
  eligible=ep>=0;rem=np.array(d['remaining'][qi]);cens=np.array(d['censored'][qi]);obs=np.array(d['observed_s'][qi]);y,mask=labels(rem,cens,obs[:,None]);mask=mask&eligible[...,None]
  validpair=mask.any(-1);effective=np.bincount(ep[validpair],minlength=len(unique));weights=np.zeros((N,A),np.float32);weights[eligible]=1/counts[ep[eligible]]
  sums=np.bincount(ep[eligible],weights=weights[eligible],minlength=len(unique));assert np.allclose(sums,1,atol=1e-6)
  path=OUT/'dataset'/split;path.mkdir(parents=True,exist_ok=True)
  arrays=dict(query_indices=qi,episode=ep,episode_keys=unique,episode_counts=counts,weights=weights,y=y,mask=mask,eligible=eligible,remaining=rem,censored=cens,observed_s=obs,query_time=np.array(d['query_time'][qi]),user=np.array(d['user'][qi]),session=np.array(d['session'][qi]),trigger=np.array(d['trigger'][qi]),recency=np.zeros((N,A),np.float32))
  arrays['recency'][local,apps]=d['query_time'][qs]-leave
  for k,v in arrays.items():np.save(path/f'{k}.npy',v)
  report[split]=dict(original_queries=len(d['query_time']),retained_queries=N,candidate_rows=len(qs),episodes=len(unique),supervised_episodes=int((effective>0).sum()),excluded_no_departure=int(no_departure.sum()),excluded_stale_after_return=int(stale.sum()),max_queries_per_episode=int(counts.max()),median_queries_per_episode=float(np.median(counts)),weight_sum_max_error=float(np.max(np.abs(sums-1))),observed_episode_count=int(len(np.unique(ep[eligible&~cens]))),censored_episode_count=int(len(np.unique(ep[eligible&cens]))))
  allkeys[split]=set(tuple(x[:3]) for x in unique);print(split,report[split],flush=True)
 # Crossing episodes are partition-local administrative censors, never merge across split endpoints.
 report['cross_partition_physical_episodes']={f'{a}_{b}':len(allkeys[a]&allkeys[b]) for a,b in [('train','val'),('val','test'),('train','test')]}
 report['definition']='(session, target app, last actual departure, administrative observation end); periodic queries share total supervised weight one; query labels are remaining time; no right-censored class invented'
 write(OUT/'dataset/audit.json',report)
if __name__=='__main__':main()
