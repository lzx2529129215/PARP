#!/usr/bin/env python3
"""Reload selected checkpoint and reconstruct causal test inputs for numerical replay."""
import argparse
import bisect
import csv
import datetime as dt
import json
from pathlib import Path
import sys
import numpy as np
import torch
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'lzx/tool/operation_predictor'))
from v3.models.app_lstm_visit_window import AppLSTMVisitWindow, encode_features, tensor_features, visit_probabilities
from v3.models.app_lstm_visit_explicit import AppLSTMVisitExplicit, explicit_features, FEATURE_NAMES, MODEL_TYPE


def stamp(t):
    return dt.datetime.fromisoformat(t).replace(tzinfo=dt.timezone.utc).timestamp()


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--training-dir',type=Path,required=True)
    args=p.parse_args();root=args.training_dir
    torch.set_num_threads(1)
    c=torch.load(root/'selected_checkpoint.pt',map_location='cpu',weights_only=False)
    explicit=c['explicit_features'];vocab=c['app_vocab'];h=c['history_len']
    assert c['model_type']==(MODEL_TYPE if explicit else 'app_visit_window_v1')
    assert c['feature_names']==(FEATURE_NAMES if explicit else [])
    model=(AppLSTMVisitExplicit if explicit else AppLSTMVisitWindow)(**c['model_args'])
    model.load_state_dict(c['model_state_dict'],strict=True);model.eval()
    dataset=Path(c['experiment']['dataset'])
    rows=[]
    with (dataset/'test.csv').open() as f:
        for r in csv.DictReader(f):
            rows.append(r)
            if len(rows)==128:break
    keys={r['session_id'] for r in rows};sessions={k:[] for k in keys}
    with (dataset/'segments.csv').open() as f:
        for r in csv.DictReader(f):
            if r['session_id'] in keys:
                sessions[r['session_id']].append((r['start_time'],r['app'],float(r['dwell_s'])))
    for values in sessions.values():values.sort(key=lambda x:x[0])
    starts={k:[v[0] for v in values] for k,values in sessions.items()}
    cursors={};predictions=[]
    for r in rows:
        key,t=r['session_id'],r['timestamp'];values=sessions[key]
        lo,hi=bisect.bisect_left(starts[key],t),bisect.bisect_right(starts[key],t)
        candidates=range(lo,hi) if lo<hi else [hi-1]
        expected=[a for a,m in zip(r['history_apps'].split('|'),r['history_mask'].split('|')) if float(m)]
        pt,pi=cursors.get(key,(None,-1))
        matches=[i for i in candidates if i>=0 and (pt!=t or i>pi) and [v[1] for v in values[max(0,i-4):i+1]]==expected]
        assert matches
        i=matches[0];cursors[key]=(t,i);history=values[max(0,i-h+1):i+1]
        apps=[v[1] for v in history];times=[stamp(v[0]) for v in history];anchor=stamp(t)
        durations=[v[2] for v in history[:-1]]+[max(1.,anchor-times[-1])]
        features=tensor_features(encode_features(apps,durations,[1]*len(apps),r['opened_apps'].split('|'),
            r['current_app'],t,r['user_group'],vocab,c['group_vocab'],history_len=h))
        if explicit:
            features['explicit']=torch.from_numpy(explicit_features([vocab[a] for a in apps],times,anchor,len(vocab))[None])
        with torch.no_grad():predictions.append(visit_probabilities(model(**features))[0].numpy())
    predictions=np.array(predictions)
    selected=json.loads((root/'high-recall-evaluation.json').read_text())['selected_arm']
    with np.load(root/selected/'test_predictions.npz') as data:
        expected=data['probabilities'][:len(rows)]
    np.testing.assert_allclose(predictions,expected,atol=2e-6,rtol=2e-5)
    result={'replayed_rows':len(rows),'strict_state_dict_load':True,'finite':bool(np.isfinite(predictions).all()),
        'monotonic':bool((predictions[:,:,0]<=predictions[:,:,1]).all()),'max_probability_difference':float(np.abs(predictions-expected).max()),
        'scope':'Offline replay from raw segment start times; no online service deployment.'}
    (root/'checkpoint-verification.json').write_text(json.dumps(result,indent=2)+'\n')
    idx=next((i for i,r in enumerate(rows) if len(set(r['opened_apps'].split('|'))-{r['current_app'],' ' ,''})>=2),0)
    row=rows[idx];opened=set(row['opened_apps'].split('|'));example=[]
    for app,aid in vocab.items():
        if app.startswith('<'):continue
        p30,p180=map(float,predictions[idx,aid])
        state='foreground' if app==row['current_app'] else ('hot' if p30>=.8 else 'cold' if p180<.2 else 'neutral') if app in opened else 'not_running'
        example.append({'app_id':aid,'app':app,'p_visit_30s':p30,'p_visit_180s':p180,'thermal_state':state,
            'predicted_at':row['timestamp'],'expires_at':str(dt.datetime.fromisoformat(row['timestamp'])+dt.timedelta(seconds=30)),
            'probability_source':'offline_checkpoint_replay'})
    (root/'prediction-example.json').write_text(json.dumps({'test_row':idx,'id_space':'vocabulary IDs (kernel runtime IDs are different)',
        'hot_threshold':.8,'cold_threshold':.2,'predictions':example},ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(result))


if __name__=='__main__':main()
