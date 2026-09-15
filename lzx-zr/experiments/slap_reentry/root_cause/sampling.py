from common import *
from offline_eval.metrics import direction
from collections import defaultdict
import itertools

def main():
    d=load_data(BASE/'dataset/test');p0=np.load(BASE/'predictions/M0-test-probabilities.npy',mmap_mode='r');p1=np.load(BASE/'predictions/M1-test-probabilities.npy',mmap_mode='r');groups={k:defaultdict(lambda:np.zeros(3)) for k in ['all','switch','periodic']}
    for qi in np.flatnonzero(d['eligible'].sum(1)>=2):
        cand=np.flatnonzero(d['eligible'][qi]);score0=1-p0[qi,:,1].astype(float);score1=p1[qi].astype(float)@np.arange(8);q=d['query_time'][qi]
        for a,b in itertools.combinations(cand,2):
            order=direction(a,b,d['remaining'][qi],d['censored'][qi],d['observed_s'][qi])
            if order is None:continue
            endpoint=lambda app:int(round((q+(d['observed_s'][qi] if d['censored'][qi,app] else d['remaining'][qi,app]))*1e6))
            key=(int(d['user'][qi]),int(d['session'][qi]),int(a),endpoint(a),bool(d['censored'][qi,a]),int(b),endpoint(b),bool(d['censored'][qi,b]))
            x=np.array([1,int(np.sign(score0[a]-score0[b]))==order,int(np.sign(score1[a]-score1[b]))==order])
            for scope in ['all','switch' if d['trigger'][qi]==0 else 'periodic']:groups[scope][key]+=x
    result={}
    for name,g in groups.items():
        a=np.array(list(g.values()));num=a[:,1:]/a[:,:1];users=np.array([k[0] for k in g]);result[name]={'comparable_pair_rows':int(a[:,0].sum()),'unique_episode_pairs':len(a),'row_weighted_poa':(a[:,1:].sum(0)/a[:,0].sum()).tolist(),'episode_pair_balanced_poa':num.mean(0).tolist(),'paired_user_bootstrap':bootstrap_ratio(num,np.ones_like(num),users,1000,42)}
    write(OUT/'metrics/sampling_weight_audit.json',result);print(json.dumps(result),flush=True)
if __name__=='__main__':main()
