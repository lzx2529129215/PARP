from common import *
from collections import defaultdict

def histories():
    sessions=read_segments(BASE/'baseline/dataset/segments.csv',META['app_vocab']);completed=defaultdict(list);exits=defaultdict(list)
    for sid,rows in sessions.items():
        previous_exit={}
        for i,r in enumerate(rows):
            a,u,t=r['a'],r['user'],r['t']
            if a in previous_exit:completed[(u,a)].append((t,t-previous_exit[a]))
            if i+1<len(rows):previous_exit[a]=rows[i+1]['t'];exits[(sid,a)].append(rows[i+1]['t'])
    result={};app=defaultdict(list);ua=defaultdict(list);pool=[];cut=stamp(META['splits']['val']['start'])
    for key,items in completed.items():
        items.sort();ema=[];current=None
        for t,value in items:
            assert value>=0;current=value if current is None else .5*value+.5*current;ema.append(current)
            if t<cut:ua[key].append(value);app[key[1]].append(value);pool.append(value)
        result[key]=(np.array([t for t,v in items]),np.array([v for t,v in items]),np.array(ema))
    return result,exits,{a:float(np.median(v)) for a,v in app.items()},{k:float(np.median(v)) for k,v in ua.items()},float(np.median(pool)),len(pool)

def main():
    history,exits,app,ua,global_fallback,nfit=histories();d=load_data(BASE/'dataset/test');sids=json.loads((BASE/'dataset/meta.json').read_text())['session_ids'];qids=np.flatnonzero(d['eligible'].sum(1)>=2)
    names=['AppMedian','UserAppMedian','LastReentry','EMA','AppMedian_remaining','UserAppMedian_remaining','LastReentry_remaining','EMA_remaining'];values=np.full((len(qids),len(names),len(KEYS)),np.nan);fallback=defaultdict(int)
    with (OUT/'predictions/simple-baseline-scores.csv').open('w') as f:
        wr=csv.writer(f);wr.writerow(['query_index','app_id']+names+['previous_interval_available','age_since_exit'])
        for row,qi in enumerate(qids):
            cands=np.flatnonzero(d['eligible'][qi]);scores=np.zeros((8,d['eligible'].shape[1]));q=float(d['query_time'][qi]);u=int(d['user'][qi]);sid=sids[int(d['session'][qi])]
            for a in cands:
                am=app.get(a,global_fallback);um=ua.get((u,a),am);item=history.get((u,a));pos=np.searchsorted(item[0],q,side='left')-1 if item is not None else -1
                last=item[1][pos] if pos>=0 else um;ema=item[2][pos] if pos>=0 else um
                if pos<0:fallback['last_or_ema']+=1
                if a not in app:fallback['app_median']+=1
                if (u,a) not in ua:fallback['user_app_median']+=1
                e=exits.get((sid,a),[]);j=bisect.bisect_right(e,q)-1;age=q-e[j] if j>=0 else 0
                scores[:4,a]=[am,um,last,ema];scores[4:,a]=np.maximum(0,scores[:4,a]-age)
                wr.writerow([int(qi),int(a)]+scores[:,a].tolist()+[pos>=0,age])
            for mi,v in enumerate(query_metrics(cands,scores,d['remaining'][qi],d['censored'][qi],d['observed_s'][qi])):values[row,mi]=[v[k] for k in KEYS]
    np.save(OUT/'metrics/simple-query-metrics.npy',values);np.save(OUT/'metrics/simple-query-indices.npy',qids)
    old=np.load(BASE/'metrics/query-metrics.npy',mmap_mode='r');reports={scope:{} for scope in ['all_multi','switch_multi','periodic_multi']}
    for scope,sel in [('all_multi',np.ones(len(qids),bool)),('switch_multi',d['trigger'][qids]==0),('periodic_multi',d['trigger'][qids]==1)]:
        for mi,name in enumerate(names):reports[scope][name]=summary(values[sel,mi])
        for mi,name in enumerate(['Recency','M0-p180','M1-mixed']):reports[scope][name]=summary(old[qids[sel],mi])
    comparisons={name:paired(values[:,mi],old[qids,2],d['user'][qids]) for mi,name in enumerate(names)}
    write(OUT/'metrics/simple_baselines.json',{'reports':reports,'M1_minus_baseline_paired':comparisons,'fallback_candidate_counts':dict(fallback),'training_unique_completed_intervals':nfit,'global_train_median':global_fallback})
    lines=['# Simple temporal baselines','','Medians fit one completed background episode (foreground exit → next entry) once, with next entry strictly before validation boundary. User-App falls back to App then global training median. Last/EMA update only from entries strictly before each query (never from the query future); EMA alpha=0.5. Session gaps are excluded. Closed is not process residency.','', 'The first four methods directly rank the historical interval as requested. They are interval heuristics, not literal remaining-time estimates. The four *_remaining variants subtract elapsed background age and clamp to zero, to expose this semantic distinction. Missing-history counts are recorded; absence is not fabricated as a very long interval.','',table(reports['all_multi']),'','## Switch only','',table(reports['switch_multi']),'','## Periodic only','',table(reports['periodic_multi']),'','User-cluster paired bootstrap comparison of M1 against EACH baseline is in metrics/simple_baselines.json. No winner is selected to tune M1. POA ties are counted incorrect, same as the frozen evaluation; constant-score baselines can therefore score below a randomized 50% ordering. Observed victim median is conditional on a known return.']
    (OUT/'SIMPLE-BASELINES.md').write_text('\n'.join(lines)+'\n');print('Simple baselines PASS',flush=True)
if __name__=='__main__':main()
