from common import *
from offline_eval.metrics import direction
from collections import defaultdict
import itertools

def main():
    d=load_data(BASE/'dataset/test');p0=np.load(BASE/'predictions/M0-test-probabilities.npy',mmap_mode='r');p1=np.load(BASE/'predictions/M1-test-probabilities.npy',mmap_mode='r');names=['M0-p180','M1-mixed','AppMedian','UserAppMedian','LastReentry','EMA','AppClassPrior'];cached=defaultdict(dict)
    with (OUT/'predictions/simple-baseline-scores.csv').open() as f:
        for r in csv.DictReader(f):cached[int(r['query_index'])][int(r['app_id'])]=[float(r[k]) for k in names[2:-1]]
    prior=np.array(json.loads((OUT/'metrics/app_class_prior.json').read_text())['train_probabilities'])@np.arange(8)
    groups=defaultdict(lambda:np.zeros(8))
    for qi,appvalues in cached.items():
        cand=sorted(appvalues);scores=np.zeros((7,d['eligible'].shape[1]));scores[0]=1-p0[qi,:,1].astype(float);scores[1]=p1[qi].astype(float)@np.arange(8)
        scores[-1]=prior
        for a,v in appvalues.items():scores[2:6,a]=v
        q=d['query_time'][qi]
        for a,b in itertools.combinations(cand,2):
            order=direction(a,b,d['remaining'][qi],d['censored'][qi],d['observed_s'][qi])
            if order is None:continue
            ep=lambda a:int(round((q+(d['observed_s'][qi] if d['censored'][qi,a] else d['remaining'][qi,a]))*1e6))
            key=(int(d['user'][qi]),int(d['session'][qi]),a,ep(a),bool(d['censored'][qi,a]),b,ep(b),bool(d['censored'][qi,b]))
            groups[key]+=np.r_[1,np.sign(scores[:,a]-scores[:,b])==order]
    rows=np.array(list(groups.values()));balanced=rows[:,1:]/rows[:,:1];users=np.array([k[0] for k in groups]);result={'episode_pairs':len(rows),'poa':dict(zip(names,balanced.mean(0).tolist())),'M1_minus_baseline':{names[j]:bootstrap_ratio(balanced[:,[j,1]],np.ones((len(rows),2)),users,1000,42) for j in [0,2,3,4,5,6]}}
    previous=json.loads((OUT/'metrics/sampling_weight_audit.json').read_text());np.testing.assert_allclose(balanced.mean(0)[:2],previous['all']['episode_pair_balanced_poa'],atol=1e-12)
    write(OUT/'metrics/episode_balanced_baselines.json',result);print(json.dumps(result),flush=True)
if __name__=='__main__':main()
