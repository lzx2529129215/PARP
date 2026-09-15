from common import *
from offline_eval.metrics import direction
from collections import defaultdict
import itertools

def main():
    d=load_data(BASE/'dataset/test');qi=np.load(OUT/'predictions/switch-old-query-indices.npy');v=np.load(OUT/'metrics/switch-query-metrics.npy');old=np.load(BASE/'metrics/query-metrics.npy',mmap_mode='r');sel=d['eligible'][qi].sum(1)>=2
    result={'switch_minus_mixed_paired':paired(old[qi[sel],2],v[sel],d['user'][qi[sel]])};p0=np.load(BASE/'predictions/M0-test-probabilities.npy',mmap_mode='r');pm=np.load(BASE/'predictions/M1-test-probabilities.npy',mmap_mode='r');ps=np.load(OUT/'predictions/switch-probabilities.npy',mmap_mode='r');g=defaultdict(lambda:np.zeros(4))
    for row in np.flatnonzero(sel):
        q=qi[row];t=d['query_time'][q];scores=[1-p0[q,:,1].astype(float),pm[q].astype(float)@np.arange(8),ps[row].astype(float)@np.arange(8)]
        for a,b in itertools.combinations(np.flatnonzero(d['eligible'][q]),2):
            order=direction(a,b,d['remaining'][q],d['censored'][q],d['observed_s'][q])
            if order is None:continue
            ep=lambda a:int(round((t+(d['observed_s'][q] if d['censored'][q,a] else d['remaining'][q,a]))*1e6))
            key=(int(d['user'][q]),int(d['session'][q]),int(a),ep(a),bool(d['censored'][q,a]),int(b),ep(b),bool(d['censored'][q,b]));g[key]+=np.r_[1,[int(np.sign(s[a]-s[b]))==order for s in scores]]
    rows=np.array(list(g.values()));num=rows[:,1:]/rows[:,:1];users=np.array([k[0] for k in g]);result['episode_balanced']={'episode_pairs':len(g),'methods':['M0-p180','M1-mixed','M1-switch'],'poa':num.mean(0).tolist(),'switch_minus_p180':bootstrap_ratio(num[:,[0,2]],np.ones((len(num),2)),users,1000,42),'switch_minus_mixed':bootstrap_ratio(num[:,[1,2]],np.ones((len(num),2)),users,1000,42)}
    write(OUT/'metrics/switch_training_effect.json',result)
    p=OUT/'SWITCH-ONLY-ABLATION.md';p.write_text(p.read_text()+'\n## Training sampling effect (M1-S versus frozen mixed M1)\n\n'+json.dumps(result,indent=2)+'\n\nBoth query-weighted and episode-pair-balanced comparisons are retained; confidence intervals resample users.\n');print(json.dumps(result),flush=True)
if __name__=='__main__':main()
