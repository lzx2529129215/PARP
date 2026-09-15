from common import *
import torch
from models.segmented import LSTMSegmentedReentry

def main():
    torch.set_num_threads(1);result={}
    for variant in ['switch','original']:
        c=torch.load(OUT/f'checkpoints/M1-{variant}.pt',map_location='cpu',weights_only=False)
        assert c['reentry_bins']==CFG['reentry_bins'] and c['history_len']==20 and c['num_segments']==8
        expected_vocab=META['app_vocab'] if variant=='switch' else json.loads(Path(CFG['raw_meta']).read_text())['app_vocab']
        assert c['app_vocab']==expected_vocab
        history=json.loads((OUT/f'metrics/{variant}-training-history.json').read_text());assert len(history)==20
        assert c['epoch']==min(history,key=lambda r:r['val_ce'])['epoch']
        for key in ['seed','lr','epochs','batch_size']:assert c['config'][key]==CFG[key]
        m=LSTMSegmentedReentry(num_segments=8,**c['model_args']);m.load_state_dict(c['state_dict'],strict=True);m.eval()
        d=load_data((BASE/'dataset' if variant=='switch' else OUT/'dataset/original')/'test');qids=np.load(OUT/f'predictions/{variant}-query-indices.npy');p=np.load(OUT/f'predictions/{variant}-probabilities.npy',mmap_mode='r');ix=np.random.default_rng(42).choice(len(qids),min(128,len(qids)),replace=False)
        with torch.no_grad():actual=torch.softmax(m(**batch(d,qids[ix])),dim=-1).numpy()
        np.testing.assert_allclose(actual,p[ix],atol=2e-6,rtol=2e-5);result[variant]={'status':'PASS','replay_rows':len(ix),'max_abs_difference':float(np.max(np.abs(actual-p[ix]))),'best_epoch':c['epoch'],'parameters':sum(x.numel() for x in m.parameters())}
        for start in range(0,len(p),4096):
            x=np.asarray(p[start:start+4096]);assert np.isfinite(x).all() and (x>=0).all() and (x<=1).all() and np.allclose(x.sum(-1),1,atol=1e-6)
    integrity=json.loads((OUT/'metrics/input-integrity.json').read_text());assert all(hashlib.sha256(Path(p).read_bytes()).hexdigest()==h for p,h in integrity['sha256'].items())
    write(OUT/'metrics/final-verification.json',{'status':'PASS','checkpoints':result,'original_inputs_unchanged':True})
    targets=[p for p in OUT.rglob('*') if p.is_file() and 'logs' not in p.parts and p.suffix in ['.md','.json','.pt','.png'] and p.name not in ['artifact-manifest.json','progress.json']]
    write(OUT/'metrics/artifact-manifest.json',{str(p.relative_to(OUT)):{'bytes':p.stat().st_size,'sha256':hashlib.sha256(p.read_bytes()).hexdigest()} for p in targets});print(json.dumps(result),flush=True)
if __name__=='__main__':main()
