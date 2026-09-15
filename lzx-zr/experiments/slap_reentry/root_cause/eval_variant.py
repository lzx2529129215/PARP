import argparse
from common import *
import torch
from models.segmented import LSTMSegmentedReentry

def main():
    p=argparse.ArgumentParser();p.add_argument('variant',choices=['switch','original']);args=p.parse_args();v=args.variant
    torch.set_num_threads(CFG['threads']);c=torch.load(OUT/f'checkpoints/M1-{v}.pt',map_location='cpu',weights_only=False);m=LSTMSegmentedReentry(num_segments=8,**c['model_args']);m.load_state_dict(c['state_dict']);m.eval()
    d=load_data((BASE/'dataset' if v=='switch' else OUT/'dataset/original')/'test');qids=np.flatnonzero((d['eligible'].sum(1)>0)&((d['trigger']==0) if v=='switch' else True));oldids=qids if v=='switch' else d['old_query_index'][qids]
    pred=np.lib.format.open_memmap(OUT/f'predictions/{v}-probabilities.npy',mode='w+',dtype='float32',shape=(len(qids),d['eligible'].shape[1],8));vals=np.full((len(qids),len(KEYS)),np.nan)
    with torch.no_grad():
        for start in range(0,len(qids),2048):pred[start:start+2048]=torch.softmax(m(**batch(d,qids[start:start+2048])),dim=-1).numpy()
    pred.flush();assert np.isfinite(pred).all() and np.allclose(pred.sum(-1),1,atol=1e-6)
    inv={i:a for a,i in c['app_vocab'].items()}
    with (OUT/f'predictions/{v}-rankings.csv').open('w') as f:
        wr=csv.writer(f);wr.writerow(['old_query_index','query_time','ranked_apps','scores','remaining','censored'])
        for row,qi in enumerate(qids):
            cand=np.flatnonzero(d['eligible'][qi]);score=pred[row].astype(float)@np.arange(8);mtr=query_metrics(cand,[score],d['remaining'][qi],d['censored'][qi],d['observed_s'][qi])[0];vals[row]=[mtr[k] for k in KEYS]
            rank=sorted(cand,key=lambda a:(-score[a],a));wr.writerow([int(oldids[row]),float(d['query_time'][qi]),json.dumps([inv[a] for a in rank]),json.dumps(score[rank].tolist()),json.dumps([None if d['censored'][qi,a] else float(d['remaining'][qi,a]) for a in rank]),json.dumps(d['censored'][qi,rank].tolist())])
    np.save(OUT/f'predictions/{v}-query-indices.npy',qids);np.save(OUT/f'predictions/{v}-old-query-indices.npy',oldids);np.save(OUT/f'metrics/{v}-query-metrics.npy',vals)
    old=load_data(BASE/'dataset/test');oldval=np.load(BASE/'metrics/query-metrics.npy',mmap_mode='r');ownmulti=d['eligible'][qids].sum(1)>=2;shared=ownmulti&(old['eligible'][oldids].sum(1)>=2)
    reports={};pairedresults={}
    for scope,sel in [('own_multi',ownmulti),('shared_multi',shared),('shared_switch',shared&(old['trigger'][oldids]==0)),('shared_periodic',shared&(old['trigger'][oldids]==1))]:
        reports[scope]={'M0-p180':summary(oldval[oldids[sel],1]),'M1-mapped-mixed':summary(oldval[oldids[sel],2]),f'M1-{v}':summary(vals[sel])}
        pairedresults[scope]=paired(oldval[oldids[sel],1 if v=='switch' else 2],vals[sel],old['user'][oldids[sel]])
    report={'variant':v,'best_epoch':c['epoch'],'best_validation_ce':c['val_ce'],'reports':reports,'paired_comparison':pairedresults,'predicted_queries':len(qids),'own_multi':int(ownmulti.sum()),'shared_multi':int(shared.sum())};write(OUT/f'metrics/{v}-evaluation.json',report)
    if v=='switch':
        lines=['# Switch-only ablation','','Only training query selection changes to switch. Validation remains the same full validation partition to isolate this change; test table is switch-only and multi-candidate. Same seed/20 epochs/Adam/lr/encoder/8 bins/CE. Checkpoint selection uses validation loss only. M1-mapped-mixed is the frozen existing model.','',table(reports['shared_multi']),'',f'Best epoch={c["epoch"]}, validation CE={c["val_ce"]:.6f}. Paired user-bootstrap CIs and full probability/ranking arrays saved.','', 'This experiment isolates training sampling within the existing segmented objective. Comparing M1-S to frozen M0 still compares different historical training sampling, so a gain is not a pure causal estimate of head-only impact.']
        (OUT/'SWITCH-ONLY-ABLATION.md').write_text('\n'.join(lines)+'\n')
    print(json.dumps(report),flush=True)
if __name__=='__main__':main()
