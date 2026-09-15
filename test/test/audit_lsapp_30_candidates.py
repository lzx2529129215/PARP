#!/usr/bin/env python3
"""Audit return-label coverage of event-derived opened candidates (not PC residency)."""
import argparse
import bisect
import csv
import json
from collections import defaultdict
from pathlib import Path
import numpy as np


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--dataset',required=True,type=Path)
    p.add_argument('--output',required=True,type=Path)
    args=p.parse_args()
    vocab=json.loads((args.dataset/'dataset_meta.json').read_text())['app_vocab']
    sessions=defaultdict(list)
    with (args.dataset/'segments.csv').open() as f:
        for r in csv.DictReader(f):
            sessions[r['session_id']].append((r['start_time'],vocab[r['app']]))
    starts={};prefix={}
    for key,values in sessions.items():
        values.sort(key=lambda x:x[0]);starts[key]=[x[0] for x in values]
        seen=0;prefix[key]=[]
        for _,aid in values:
            if aid<30:seen|=1<<aid
            prefix[key].append(seen)
    result={'definition':'Previously entered target within derived session, excluding current foreground. Includes closed/relaunched apps; not true PC-resident truth.', 'splits':{}}
    for split in ('val','test'):
        total=covered=rows_with_returns=0;cursors={}
        per_app=np.zeros((30,2),dtype=np.int64)
        with (args.dataset/f'{split}.csv').open() as f:
            for r in csv.DictReader(f):
                key,t=r['session_id'],r['timestamp'];values=sessions[key]
                lo,hi=bisect.bisect_left(starts[key],t),bisect.bisect_right(starts[key],t)
                indices=range(lo,hi) if lo<hi else [hi-1]
                expected=[vocab[a] for a,m in zip(r['history_apps'].split('|'),r['history_mask'].split('|')) if float(m)]
                pt,pi=cursors.get(key,(None,-1))
                matches=[i for i in indices if i>=0 and (pt!=t or i>pi) and [v[1] for v in values[max(0,i-4):i+1]]==expected]
                assert matches,(split,key,t)
                idx=matches[0];cursors[key]=(t,idx)
                seen=prefix[key][idx]
                current=vocab[r['current_app']]
                opened={vocab[a] for a in r['opened_apps'].split('|') if a}
                positives={vocab[a] for a in r['labels_visit_30s'].split('|') if a}
                returning={a for a in positives if a<30 and a!=current and seen&(1<<a)}
                hits=returning&opened
                total+=len(returning);covered+=len(hits);rows_with_returns+=bool(returning)
                for a in returning:per_app[a,0]+=1
                for a in hits:per_app[a,1]+=1
        result['splits'][split]={'previously_seen_background_positive_pairs':total,'eligible_positive_pairs':covered,
            'excluded_positive_pairs':total-covered,'candidate_coverage_ceiling_proxy':covered/total if total else None,
            'anchors_with_previous_return':rows_with_returns,
            'per_app':{a:{'return_pairs':int(per_app[i,0]),'eligible_pairs':int(per_app[i,1])} for a,i in vocab.items() if i<30}}
    args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({s:{k:v for k,v in r.items() if k!='per_app'} for s,r in result['splits'].items()},indent=2))


if __name__=='__main__':main()
