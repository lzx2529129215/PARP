#!/usr/bin/env python3
"""Independent vector label checks and source event spot-check report."""
import argparse,bisect,csv,json,sys
from collections import defaultdict
from pathlib import Path
import numpy as np
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from dataset.build import stamp,iso,write


def main():
    p=argparse.ArgumentParser();p.add_argument('--config',type=Path,required=True);a=p.parse_args();cfg=json.loads(a.config.read_text());out=Path(cfg['output']);dest=out/'dataset'
    meta=json.loads((dest/'meta.json').read_text());vocab=meta['source_meta']['app_vocab'];inv={v:k for k,v in vocab.items()};bins=np.array(cfg['reentry_bins']);rng=np.random.default_rng(731)
    src=defaultdict(list)
    with (out/'baseline/dataset/segments.csv').open() as f:
        for r in csv.DictReader(f):src[r['session_id']].append((stamp(r['start_time']),vocab[r['app']]))
    for v in src.values():v.sort(key=lambda x:x[0])
    checks={};examples=[]
    for split in ['train','val','test']:
        d={key:np.load(dest/split/f'{key}.npy',mmap_mode='r') for key in ['eligible','current_app','labels','label_valid','remaining','censored','observed_s','query_time','session','history_apps','history_mask','explicit']}
        n=len(d['query_time']);assert np.all(np.diff(d['query_time'])>=0)
        assert not d['eligible'][:,30:].any();assert not d['eligible'][np.arange(n),d['current_app']].any()
        valid_pairs=candidate_pairs=0
        for start in range(0,n,10000):
            sl=slice(start,start+10000);e=d['eligible'][sl];v=d['label_valid'][sl];c=d['censored'][sl];r=d['remaining'][sl];y=d['labels'][sl];obs=d['observed_s'][sl,None]
            assert not (v & ~e).any()
            observed=e&~c;assert np.isfinite(r[observed]).all() and (r[observed]>0).all();assert np.all(y[observed]==np.searchsorted(bins,r[observed],side='right'))
            assert np.all(r[observed] <= np.broadcast_to(obs,r.shape)[observed]+1e-5)
            cens=e&c;expected=cens&(obs>=bins[-1]);assert np.array_equal(v&cens,expected);assert (y[cens&~v]==-1).all();assert (y[expected]==7).all()
            assert np.isfinite(d['explicit'][sl]).all();valid_pairs+=int(v.sum());candidate_pairs+=int(e.sum())
        rows=np.flatnonzero(d['eligible'].any(1));chosen=rng.choice(rows,min(100,len(rows)),replace=False)
        for qi in sorted(chosen):
            app=int(rng.choice(np.flatnonzero(d['eligible'][qi])));t=float(d['query_time'][qi]);until=t+float(d['observed_s'][qi]);sid=meta['session_ids'][d['session'][qi]]
            nxt=next((when for when,a in src[sid] if a==app and t<when<=until),None)
            cens=nxt is None;remaining=None if cens else nxt-t
            klass=(7 if until-t>=3600 else -1) if cens else sum(remaining>=edge for edge in bins)
            klass=int(klass)
            assert cens==bool(d['censored'][qi,app]);assert klass==int(d['labels'][qi,app])
            if not cens:assert abs(remaining-d['remaining'][qi,app])<1e-5
            examples.append({'split':split,'query_index':int(qi),'session':sid,'app':inv[app],'query':iso(t),'next_entry':iso(nxt) if nxt is not None else 'not observed',
                'remaining':remaining,'observed_seconds':round(until-t,6),'class':klass,'valid':klass>=0,'source_check':'PASS'})
        checks[split]={'queries':n,'candidate_pairs':candidate_pairs,'valid_pairs':valid_pairs,'independent_source_checks':len(chosen),'status':'PASS'}
    write(dest/'independent-validation.json',{'status':'PASS','splits':checks,'samples':examples})
    lines=['# Independent dataset validation','', 'PASS: vector checks for every candidate + 300 independently selected source-event checks.','', '| split/query | app | query time | next entry | remaining / observed s | class | valid |','|---|---|---|---|---|---|---|']
    for r in examples:lines.append(f'| {r["split"]}/{r["query_index"]} | {r["app"]} | {r["query"]} | {r["next_entry"]} | {r["remaining"]} / {r["observed_seconds"]} | {r["class"]} | {r["valid"]} |')
    (dest/'SOURCE-REVIEW.md').write_text('\n'.join(lines)+'\n')
    original=meta['audit'];original['status']='PASS';original['independent_checks']=checks;original['independent_samples']=len(examples);write(dest/'validation.json',original)
    (out/'DATASET-VALIDATION.md').write_text('# DATASET VALIDATION\n\nPhase 3: PASS\n\n'+json.dumps(original,ensure_ascii=False,indent=2)+'\n\n独立核对了全部标签的区间/删失/候选/分区约束，并抽取300条源事件核对。人工可审阅表：dataset/SOURCE-REVIEW.md；自动核对不冒充用户人工验收。\n\n源session可能跨全局时间边界，与M0完全相同；禁止随机切分，跨边界标签截断，不声称session-disjoint。\n\nLSApp opened state != true PC resident process state。历史保留旧最小1秒编码；真实current_duration/时间差不使用最终片段时长。\n')
    print(json.dumps(checks))


if __name__=='__main__':main()
