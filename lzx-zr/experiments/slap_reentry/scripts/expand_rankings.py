#!/usr/bin/env python3
"""Append complete ranked app lists to the per-query metric CSV."""
import argparse,csv,itertools,json
from pathlib import Path


def main():
    p=argparse.ArgumentParser();p.add_argument('--config',type=Path,required=True);a=p.parse_args();out=Path(json.loads(a.config.read_text())['output'])
    vocab=json.loads((out/'baseline/dataset/dataset_meta.json').read_text())['app_vocab'];target=out/'predictions/ranking_results.csv';tmp=target.with_suffix('.csv.tmp')
    with (out/'predictions/predictions.csv').open() as pf,target.open() as rf,tmp.open('w') as wf:
        old=csv.DictReader(rf)
        if 'ranked_apps' in old.fieldnames:tmp.unlink();return
        writer=csv.DictWriter(wf,fieldnames=old.fieldnames+['ranked_apps','ranked_scores','actual_remaining_in_rank_order','censored_in_rank_order']);writer.writeheader()
        score_keys={'Recency':'recency_score','p180':'p180_coldness','SLAP-style':'coldness_score'}
        for qi,group in itertools.groupby(csv.DictReader(pf),key=lambda r:r['query_index']):
            candidates=list(group)
            for method,key in score_keys.items():
                row=next(old);assert row['query_index']==qi and row['method']==method
                ranked=sorted(candidates,key=lambda r:(-float(r[key]),vocab[r['candidate_app']]))
                row.update(ranked_apps=json.dumps([r['candidate_app'] for r in ranked]),ranked_scores=json.dumps([float(r[key]) for r in ranked]),
                    actual_remaining_in_rank_order=json.dumps([float(r['remaining_sec']) if r['remaining_sec'] else None for r in ranked]),
                    censored_in_rank_order=json.dumps([r['is_censored']=='True' for r in ranked]));writer.writerow(row)
        assert next(old,None) is None
    tmp.replace(target);print('Full ranked lists added')


if __name__=='__main__':main()
