#!/usr/bin/env python3
"""Assess joint hot precision/recall; choose operating threshold on validation only."""
import argparse
import json
from pathlib import Path
import numpy as np


def frontier(data):
    m=data['eligible'] & data['valid'][:,:,0].astype(bool)
    p=data['probabilities'][:,:,0][m].astype(np.float64)
    y=data['labels'][:,:,0][m].astype(np.float64)
    if not len(p) or not y.sum():raise ValueError('No known candidates or positives')
    order=np.argsort(-p,kind='stable');p=p[order];y=y[order]
    ends=np.r_[np.flatnonzero(p[:-1]!=p[1:]),len(p)-1]
    tp=np.cumsum(y)[ends]
    return p[ends],tp/(ends+1),tp/y.sum(),ends+1


def at_threshold(data,threshold):
    e=data['eligible'];known=e & data['valid'][:,:,0].astype(bool)
    positive=known & (data['labels'][:,:,0]==1)
    selected=e & (data['probabilities'][:,:,0]>=threshold)
    count=int((selected & known).sum());hits=int((selected & positive).sum())
    return {'threshold':threshold,'selected':int(selected.sum()),'valid_selected':count,'hits':hits,
        'positive_pairs':int(positive.sum()),'precision':hits/count if count else None,
        'recall':hits/int(positive.sum()) if positive.any() else None,
        'candidate_pair_coverage':float(selected.sum()/e.sum()) if e.any() else None}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--arm-dir',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--recall',type=float,default=.9)
    p.add_argument('--precision',type=float,default=.8)
    a=p.parse_args()
    if not 0<a.recall<=1 or not 0<a.precision<=1:p.error('Targets must be in (0,1]')
    data={}
    for s in ('val','test'):
        with np.load(a.arm_dir/f'{s}_predictions.npz') as d:data[s]={k:d[k] for k in d.files}
    thresholds,precision,recall,count=frontier(data['val'])
    feasible=(recall>=a.recall)&(precision>=a.precision)
    indices=np.flatnonzero(recall>=a.recall)
    idx=indices[np.argmax(precision[indices])]
    threshold=float(thresholds[idx])
    result={'arm':a.arm_dir.name,'targets':{'recall':a.recall,'precision':a.precision},
        'validation_joint_target_feasible':bool(feasible.any()),
        'threshold_selection':'maximum validation precision subject to validation recall target; test never used',
        'validation_selected_operating_point':{s:at_threshold(d,threshold) for s,d in data.items()},
        'validation_frontier':[]}
    for target in (.8,.9,.95,.99):
        indices=np.flatnonzero(recall>=target);idx=indices[np.argmax(precision[indices])]
        result['validation_frontier'].append({'recall_constraint':target,'best_precision':float(precision[idx]),
            'recall':float(recall[idx]),'threshold':float(thresholds[idx]),'valid_selected':int(count[idx])})
    result['warning']='Retrospective assessment on existing LSApp holdout; not a fresh generalization test. Candidate recall excludes returns outside opened set.'
    a.output.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
