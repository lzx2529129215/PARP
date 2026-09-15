#!/usr/bin/env python3
"""Offline checkpoint replay and single-query CPU overhead, with no sink."""
import argparse,json,sys,time
from pathlib import Path
import numpy as np
import torch
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from scripts.train import load_data,batch
from models.segmented import LSTMSegmentedReentry,predict,MODEL_TYPE
from models.app_lstm_visit_explicit import FEATURE_NAMES
from dataset.build import write


def main():
    p=argparse.ArgumentParser();p.add_argument('--config',type=Path,required=True);a=p.parse_args();cfg=json.loads(a.config.read_text());out=Path(cfg['output']);torch.set_num_threads(cfg['threads'])
    c=torch.load(out/'checkpoints/M1.pt',map_location='cpu',weights_only=False)
    assert c['model_type']==MODEL_TYPE and c['history_len']==20 and c['feature_names']==FEATURE_NAMES
    assert c['reentry_bins']==cfg['reentry_bins'] and c['num_segments']==len(c['reentry_bins'])+1
    model=LSTMSegmentedReentry(num_segments=c['num_segments'],**c['model_args']);model.load_state_dict(c['state_dict'],strict=True);model.eval()
    d=load_data(out/'dataset/test');ids=np.flatnonzero(d['eligible'].sum(1)>=2)[:128]
    with torch.no_grad():probs,classes,scores=predict(model(**batch(d,ids)))
    expected=np.load(out/'predictions/M1-test-probabilities.npy',mmap_mode='r')[ids]
    np.testing.assert_allclose(probs.numpy(),expected,atol=2e-6,rtol=2e-5)
    assert torch.isfinite(probs).all() and ((scores>=0)&(scores<=7)).all()
    one=batch(d,ids[:1]);times=[]
    with torch.no_grad():
        for _ in range(20):predict(model(**one))
        for _ in range(200):
            start=time.perf_counter_ns();predict(model(**one));times.append((time.perf_counter_ns()-start)/1e6)
    result={'status':'PASS','model_type':MODEL_TYPE,'checkpoint_epoch':c['epoch'],'replayed_rows':len(ids),'max_abs_error':float(np.abs(probs.numpy()-expected).max()),
        'parameters':sum(v.numel() for v in model.parameters()),'parameter_bytes':sum(v.numel()*v.element_size() for v in model.parameters()),
        'single_query_cpu_inference_ms':{'mean':float(np.mean(times)),'p50':float(np.percentile(times,50)),'p95':float(np.percentile(times,95)),'p99':float(np.percentile(times,99))},
        'scope':'Offline CPU encoder/head/softmax timing only; excludes event collection, feature building, sink, memcg lookup and reclaim. No end-to-end system overhead claim.'}
    write(out/'metrics/checkpoint-verification.json',result);print(json.dumps(result))


if __name__=='__main__':main()
