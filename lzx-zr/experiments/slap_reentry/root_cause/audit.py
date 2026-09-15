from common import *
from collections import Counter,defaultdict
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

def episodes(d,selection,vocab):
    q,a=np.where(d['eligible']&selection[:,None]);observed=~d['censored'][q,a];q0,a0=q[observed],a[observed]
    nxt=np.rint((d['query_time'][q0]+d['remaining'][q0,a0])*1e6).astype('int64')
    keys=np.rec.fromarrays([d['user'][q0],d['session'][q0],a0,nxt],names='user,session,app,event_time_us')
    _,first,inv,counts=np.unique(keys,return_index=True,return_inverse=True,return_counts=True)
    per={}
    for name,app in vocab.items():
        if name.startswith('<'):continue
        c=counts[a0[first]==app];per[name]={'candidate_samples':int((a==app).sum()),'unique_reentries':len(c),'median_samples_per_reentry':float(np.median(c)) if len(c) else None}
    cq,ca=q[~observed],a[~observed]
    censkeys=np.rec.fromarrays([d['user'][cq],d['session'][cq],ca,np.rint((d['query_time'][cq]+d['observed_s'][cq])*1e6).astype('int64')],names='user,session,app,boundary_us')
    return {'candidate_rows':len(q),'observed_rows':len(q0),'censored_rows':len(cq),'unique_reentry_episodes':len(counts),'observed_rows_per_episode':float(len(q0)/len(counts)) if len(counts) else None,'all_rows_per_observed_episode_not_effective_n':float(len(q)/len(counts)) if len(counts) else None,'censored_groups_not_observed_episodes':len(np.unique(censkeys)),'rows_per_observed_episode':stats(counts),'per_app':per}

def distribution(d,sel):
    valid=d['label_valid']&sel[:,None];h=np.bincount(d['labels'][valid],minlength=8);return {'n':int(h.sum()),'counts':h.tolist(),'percent':(100*h/max(h.sum(),1)).tolist(),'unknown_candidates':int(((d['eligible']&~d['label_valid'])&sel[:,None]).sum())}

def main():
    effective={};dist={}
    for split in ['train','val','test']:
        d=load_data(BASE/'dataset'/split);n=len(d['query_time'])
        for name,sel in [('all',np.ones(n,bool)),('switch',d['trigger']==0),('periodic',d['trigger']==1)]:
            effective[f'{split}/{name}']=episodes(d,sel,META['app_vocab']);dist[f'{split}/{name}']=distribution(d,sel)
        print('audited',split,flush=True)
    write(OUT/'metrics/effective_samples.json',effective);write(OUT/'metrics/true_class_distribution.json',dist)
    lines=['# Effective sample audit','','Unique episode=(user, derived session, candidate app, strict-future entry event). Event uses source timestamp at microsecond resolution; consecutive same-app segments are merged by the frozen pipeline. Censored groups are reported separately and never invented as known reentries. Distinct episodes still share users/history: episode count is not an IID effective sample size.','', '| Split / trigger | Candidate rows | Observed rows | Unique observed episodes | Observed rows/episode | Censored rows |','|---|---:|---:|---:|---:|---:|']
    for k,v in effective.items():lines.append(f'| {k} | {v["candidate_rows"]} | {v["observed_rows"]} | {v["unique_reentry_episodes"]} | {v["observed_rows_per_episode"]:.2f} | {v["censored_rows"]} |')
    lines+=['','The switch and periodic episode sets overlap; do not add their counts. Per-app counts and median multiplicity are in metrics/effective_samples.json. All rows/observed episode is also available but mixes censored rows into its numerator and is not the main multiplicity measure.']
    (OUT/'EFFECTIVE-SAMPLE-AUDIT.md').write_text('\n'.join(lines)+'\n')
    d=load_data(BASE/'dataset/test');p=np.load(BASE/'predictions/M1-test-probabilities.npy',mmap_mode='r');comparison={}
    for name,sel in [('all',np.ones(len(p),bool)),('switch',d['trigger']==0),('periodic',d['trigger']==1)]:
        good=d['label_valid']&sel[:,None];probs=np.array(p[good],dtype=float);true=np.bincount(d['labels'][good],minlength=8)/good.sum();arg=np.bincount(probs.argmax(1),minlength=8)/len(probs);mean=probs.mean(0)
        comparison[name]={'n_matched_valid':len(probs),'true_fraction':true.tolist(),'argmax_fraction':arg.tolist(),'mean_probability':mean.tolist(),'R57_argmax':float(arg[[5,7]].sum()/true[[5,7]].sum()),'R57_mean_mass':float(mean[[5,7]].sum()/true[[5,7]].sum())}
    values=np.load(BASE/'metrics/query-metrics.npy',mmap_mode='r');q=np.flatnonzero(d['eligible'].sum(1)>=2);victim=values[q,2,KEYS.index('victim')].astype(int);probs=np.array(p[q,victim],float);h=-(probs*np.log(np.maximum(probs,1e-30))).sum(1);maxp=probs.max(1);entropy={};plot=[]
    for w in [30,180]:
        risk=values[q,2,KEYS.index(f'dvr{w}')]
        for desc,sel in [(f'dangerous_lt{w}',risk==1),(f'known_safe_ge{w}',risk==0),(f'unknown_at{w}',~np.isfinite(risk))]:
            entropy[desc]={'entropy_nats':stats(h[sel]),'normalized_entropy':stats(h[sel]/np.log(8)),'max_probability':stats(maxp[sel]),'fraction_max_p_ge_0.8':float((maxp[sel]>=.8).mean()) if sel.any() else None};plot.append((desc,h[sel]))
    write(OUT/'metrics/class_collapse.json',comparison);write(OUT/'metrics/prediction_entropy.json',entropy)
    fig,ax=plt.subplots();x=np.arange(8);r=comparison['all']
    for offset,k in [(-.25,'true_fraction'),(0,'argmax_fraction'),(.25,'mean_probability')]:ax.bar(x+offset,r[k],width=.25,label=k)
    ax.set(xlabel='Class',ylabel='Fraction on same valid test candidates');ax.legend();fig.tight_layout();fig.savefig(OUT/'figures/true_vs_pred_class_distribution.png');plt.close(fig)
    fig,ax=plt.subplots()
    for name,a in plot:
        if name.startswith('unknown') or not len(a):continue
        a=np.sort(a/np.log(8));ax.step(a,np.arange(1,len(a)+1)/len(a),label=f'{name} n={len(a)}')
    ax.set(xlabel='Normalized entropy H/log(8)',ylabel='CDF');ax.legend(fontsize=8);fig.tight_layout();fig.savefig(OUT/'figures/prediction_entropy.png');plt.close(fig)
    lines=['# Class distribution diagnosis','','True/pred comparisons use IDENTICAL classification-valid candidates. Unknown labels are excluded from both sides; previous 95.73% used all candidates and is not directly the denominator here. Full train/val/test and switch/periodic true counts and percentages: metrics/true_class_distribution.json.','', '| Class | True % | Argmax % | Mean predicted probability |','|---|---:|---:|---:|']
    for k in range(8):lines.append(f'| C{k} | {100*r["true_fraction"][k]:.3f} | {100*r["argmax_fraction"][k]:.3f} | {r["mean_probability"][k]:.5f} |')
    lines+=['',f'R57(argmax)={r["R57_argmax"]:.4f}; R57(mean probability mass)={r["R57_mean_mass"]:.4f}. Argmax concentration alone does not establish low entropy or absence of ranking information.','', '## Victim entropy','',json.dumps(entropy,indent=2),'','Dangerous <30 is a subset of dangerous <180. Safe means known not to return before the stated threshold, not permanently cold. Unknown remains separate. Entropy is uncertainty of the 8-class distribution, not a calibrated probability that the victim decision is correct. No thresholds were fitted to test outcomes.']
    lines += ['', '## True labels by split and query type', '',
              '| Scope | Valid n | Unknown n | C0 | C1 | C2 | C3 | C4 | C5 | C6 | C7 |',
              '|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|']
    for scope, values in dist.items():
        cells = [f'{count} ({pct:.2f}%)' for count, pct in zip(values['counts'], values['percent'])]
        lines.append('| ' + ' | '.join([scope, str(values['n']), str(values['unknown_candidates']), *cells]) + ' |')
    (OUT/'CLASS-DISTRIBUTION-DIAGNOSIS.md').write_text('\n'.join(lines)+'\n')
    print('audits PASS',flush=True)
if __name__=='__main__':main()
