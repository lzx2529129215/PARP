from common import *

def main():
    tr=load_data(BASE/'dataset/train');A=tr['eligible'].shape[1];counts=np.zeros((A,8),np.int64)
    for start in range(0,len(tr['labels']),8192):
        valid=tr['label_valid'][start:start+8192];q,a=np.where(valid);cls=tr['labels'][start:start+8192][q,a];counts+=np.bincount(a*8+cls,minlength=A*8).reshape(A,8)
    global_p=counts.sum(0)/counts.sum();probs=np.tile(global_p,(A,1));seen=counts.sum(1)>0;probs[seen]=counts[seen]/counts[seen].sum(1)[:,None];score=probs@np.arange(8)
    d=load_data(BASE/'dataset/test');qids=np.flatnonzero(d['eligible'].sum(1)>=2);v=[]
    for qi in qids:
        result=query_metrics(np.flatnonzero(d['eligible'][qi]),[score],d['remaining'][qi],d['censored'][qi],d['observed_s'][qi])[0];v.append([result[k] for k in KEYS])
    v=np.array(v);old=np.load(BASE/'metrics/query-metrics.npy',mmap_mode='r');report={'fit':'Only training classification-valid candidate rows; constant per-app empirical P(Ck); larger expected class index is colder; no sequence/context/model training','train_counts':counts.tolist(),'train_probabilities':probs.tolist(),'reports':{}}
    for scope,sel in [('all_multi',np.ones(len(qids),bool)),('switch_multi',d['trigger'][qids]==0),('periodic_multi',d['trigger'][qids]==1)]:
        report['reports'][scope]={'AppClassPrior':summary(v[sel]),'M1-mixed':summary(old[qids[sel],2])}
    report['M1_minus_prior_paired']=paired(v,old[qids,2],d['user'][qids]);write(OUT/'metrics/app_class_prior.json',report);np.save(OUT/'metrics/app-class-prior-query-metrics.npy',v)
    p=OUT/'SIMPLE-BASELINES.md';p.write_text(p.read_text()+'\n## Train-only per-app class-prior control\n\n'+table(report['reports']['all_multi'])+'\n\nThis extra count-only control matches M1’s periodic-weighted class target, to avoid equating every gain over an interval median with sequence learning. No test labels fit these priors. Details: metrics/app_class_prior.json.\n');print(json.dumps(report['reports']),flush=True)
if __name__=='__main__':main()
