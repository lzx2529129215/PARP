#!/usr/bin/env python3
"""Frozen-boundary, causal 30-second query data; no writes outside experiment."""
import argparse,bisect,csv,datetime as dt,json,sys,time
from collections import defaultdict,deque,Counter
from pathlib import Path
import numpy as np
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from models.app_lstm_visit_explicit import explicit_features,FEATURE_NAMES


def stamp(s):return dt.datetime.fromisoformat(s).replace(tzinfo=dt.timezone.utc).timestamp()
def iso(t):return dt.datetime.fromtimestamp(t,dt.timezone.utc).replace(tzinfo=None).isoformat(sep=' ')
def write(p,x):
 tmp=p.with_suffix(p.suffix+'.tmp');tmp.write_text(json.dumps(x,ensure_ascii=False,indent=2)+'\n');tmp.replace(p)


def label(query,next_entry,observed,bins):
    if next_entry is not None and query < next_entry <= observed:
        remaining=next_entry-query
        return remaining,int(np.searchsorted(bins,remaining,side='right')),False,True
    # Observation includes its endpoint. >= largest edge uniquely identifies last class.
    return np.nan,len(bins) if observed-query>=bins[-1] else -1,True,observed-query>=bins[-1]


def main():
    ap=argparse.ArgumentParser();ap.add_argument('--config',type=Path,required=True);a=ap.parse_args()
    cfg=json.loads(a.config.read_text());out=Path(cfg['output']);base=out/'baseline/dataset';dest=out/'dataset';dest.mkdir(exist_ok=False)
    meta=json.loads((base/'dataset_meta.json').read_text());vocab=meta['app_vocab'];inv={v:k for k,v in vocab.items()};bins=cfg['reentry_bins']
    sessions=defaultdict(list)
    with (base/'segments.csv').open() as f:
        for r in csv.DictReader(f):
            sessions[r['session_id']].append((stamp(r['start_time']),vocab[r['app']],float(r['dwell_s']),
                [vocab[x] for x in r['opened_apps_start'].replace(';','|').split('|') if x and not x.startswith('<')],stamp(r['observed_until']),int(r['user_id'])))
    for items in sessions.values():items.sort(key=lambda x:x[0])
    sid_names=list(sessions);sid_id={s:i for i,s in enumerate(sid_names)}
    limits=[stamp(meta['splits']['val']['start']),stamp(meta['splits']['test']['start'])]
    first=stamp(meta['splits']['train']['start']);last=stamp(meta['splits']['test']['end'])
    originals={};counts={};orig_sessions={}
    for split in ('train','val','test'):
        original=defaultdict(deque);seen=set()
        with (base/f'{split}.csv').open() as f:
            for index,r in enumerate(csv.DictReader(f)):
                key=(r['session_id'],stamp(r['timestamp']),tuple(vocab[x] for x,m in zip(r['history_apps'].split('|'),r['history_mask'].split('|')) if int(m)))
                original[key].append((index,r['opened_apps'],r['history_durations_s'],r['observed_until']))
                seen.add(r['session_id'])
        originals[split]=original;orig_sessions[split]=seen;counts[split]=index+1
    queries={s:[] for s in originals}
    for sid,items in sessions.items():
        for idx,item in enumerate(items):
            start,app,dwell,opened,obs,user=item
            elapsed=0
            while elapsed<max(1.,dwell):
                query=start+elapsed
                if first<=query<=last and query<=obs:
                    split=('train','val','test')[bisect.bisect_right(limits,query)]
                    queries[split].append((query,sid_id[sid],idx,elapsed))
                elapsed+=cfg['period_s']
    for rows in queries.values():rows.sort(key=lambda x:x[0])
    audit={'bins':bins,'fixed_split_boundaries':[iso(x) for x in limits],'original_split_counts':counts,
        'original_session_overlap_train_val':len(orig_sessions['train']&orig_sessions['val']),
        'original_session_overlap_val_test':len(orig_sessions['val']&orig_sessions['test']),
        'split_contract':'chronological partitions, no random splitting; labels censored at next partition start minus 1 microsecond; prior history allowed',
        'opened_definition':'frozen segment-start opened approximation; not PC process residency','splits':{}}
    rng=np.random.default_rng(42);samples=[]
    for split,rows in queries.items():
        write(out/'progress.json',{'phase':'dataset','split':split,'rows':len(rows)})
        path=dest/split;path.mkdir();n=len(rows);A=len(vocab);H=cfg['history_len']
        specifications={'history_apps':((n,H),'int64'),'history_durations':((n,H),'float32'),'history_mask':((n,H),'float32'),
            'opened_apps':((n,A),'float32'),'current_app':((n,),'int64'),'time_feature':((n,3),'float32'),'user_group':((n,),'int64'),
            'explicit':((n,A,len(FEATURE_NAMES)),'float32'),'eligible':((n,A),'bool'),'labels':((n,A),'int64'),
            'label_valid':((n,A),'bool'),'remaining':((n,A),'float64'),'censored':((n,A),'bool'),
            'observed_s':((n,),'float64'),'query_time':((n,),'float64'),'session':((n,),'int32'),'user':((n,),'int32'),
            'original_index':((n,),'int64'),'recency':((n,A),'float64'),'recency_known':((n,A),'bool'),'trigger':((n,),'int8')}
        arr={key:np.lib.format.open_memmap(path/f'{key}.npy',mode='w+',dtype=dtype,shape=shape) for key,(shape,dtype) in specifications.items()}
        for key,value in [('history_apps',vocab['<PAD>']),('labels',-1),('remaining',np.nan),('original_index',-1),('recency',0)]:arr[key][:]=value
        fields=['query_index','query_time','session_id','user_id','candidate_app','foreground_app','opened_apps','history_apps','history_durations','history_mask','current_duration','last_enter_gap','last_leave_gap','frequency_features','time_features','next_entry_time','remaining_reentry_sec','observed_until','reentry_class','is_censored','label_valid','original_index']
        f=(path/'candidate_rows.csv').open('w');writer=csv.DictWriter(f,fieldnames=fields);writer.writeheader()
        distribution=Counter();candidate_count=Counter();censor_count=valid_count=matched=0;max_feature_error=0.;previous={};violations=0
        sample_indices=set(rng.choice(n,min(50,n),replace=False).tolist())
        entries={};previous_sid=None
        for qi,(query,snum,idx,elapsed) in enumerate(rows):
            sid=sid_names[snum];items=sessions[sid];start,current,dwell,opened,obs,user=items[idx]
            partition_end=limits[('train','val').index(split)]-1e-6 if split!='test' else float('inf');until=min(obs,partition_end)
            history=items[max(0,idx-H+1):idx+1];apps=[h[1] for h in history];times=[h[0] for h in history];length=len(history)
            durations=[h[2] for h in history[:-1]]+[max(1.,query-start)]
            assert all(t<=query for t in times) and all(t+max(0,d-1)<=query for t,d in zip(times[:-1],durations[:-1]))
            assert all(t<=query for t in times[1:])
            arr['history_apps'][qi,-length:]=apps;arr['history_durations'][qi,-length:]=durations;arr['history_mask'][qi,-length:]=1
            arr['opened_apps'][qi,opened]=1;arr['current_app'][qi]=current
            now=dt.datetime.fromtimestamp(query,dt.timezone.utc);tf=[now.hour/23.,now.weekday()/6.,float(now.weekday()>=5)];arr['time_feature'][qi]=tf
            arr['explicit'][qi]=explicit_features(apps,times,query,A)
            candidates=sorted(set(opened)-{current});arr['eligible'][qi,candidates]=True;candidate_count[len(candidates)]+=1
            arr['observed_s'][qi]=until-query;arr['query_time'][qi]=query;arr['session'][qi]=snum;arr['user'][qi]=user;arr['trigger'][qi]=int(elapsed!=0)
            key=(sid,query,tuple(apps[-5:]));original=originals[split].get(key)
            if original:
                oi,oo,od,ou=original.popleft();arr['original_index'][qi]=oi;matched+=1
                assert set(oo.split('|'))-{''}=={inv[x] for x in opened}
                expected=np.array([float(x) for x in od.split('|')]);assert np.array_equal(arr['history_durations'][qi,-5:],expected)
                assert abs(stamp(ou)-until)<1e-5
            if sid not in entries:
                byapp=defaultdict(list)
                for item in items:byapp[item[1]].append(item[0])
                entries[sid]=byapp
            for app in candidates:
                ts=entries[sid][app];pos=bisect.bisect_right(ts,query);nxt=ts[pos] if pos<len(ts) else None
                remaining,cls,cens,valid=label(query,nxt,until,bins)
                arr['remaining'][qi,app]=remaining;arr['labels'][qi,app]=cls;arr['label_valid'][qi,app]=valid;arr['censored'][qi,app]=cens
                # Recency = elapsed since last known foreground end within the same visible 20 segments.
                locations=[i for i,x in enumerate(apps[:-1]) if x==app]
                last_enter=query-times[locations[-1]] if locations else None;last_leave=query-times[locations[-1]+1] if locations else None
                if last_leave is not None:arr['recency'][qi,app]=last_leave;arr['recency_known'][qi,app]=True
                distribution[str(cls)]+=1;censor_count+=int(cens);valid_count+=int(valid)
                if not cens:
                    old=previous.get((sid,app))
                    if old and nxt==old[1] and query>old[0]:
                        assert remaining<old[2] and abs((old[2]-remaining)-(query-old[0]))<1e-5
                    previous[(sid,app)]=(query,nxt,remaining)
                record={'query_index':qi,'query_time':iso(query),'session_id':sid,'user_id':user,'candidate_app':inv[app],'foreground_app':inv[current],
                    'opened_apps':'|'.join(inv[x] for x in opened),'history_apps':'|'.join(inv[x] for x in apps),'history_durations':'|'.join(map(str,durations)),
                    'history_mask':'|'.join(['1']*length),'current_duration':query-start,'last_enter_gap':last_enter,'last_leave_gap':last_leave,
                    'frequency_features':json.dumps(arr['explicit'][qi,app,4:8].tolist()),'time_features':json.dumps(tf),
                    'next_entry_time':iso(nxt) if not cens else '', 'remaining_reentry_sec':remaining if not cens else '',
                    'observed_until':iso(until),'reentry_class':cls,'is_censored':int(cens),'label_valid':int(valid),'original_index':int(arr['original_index'][qi])}
                writer.writerow(record)
                if qi in sample_indices:
                    # Independently scan source suffix to verify nearest strict-future event.
                    future=next((x[0] for x in items[idx+1:] if x[1]==app and query<x[0]<=until),None)
                    check=label(query,future,until,bins)
                    assert check[1:]==(cls,cens,valid)
                    if not cens:assert abs(check[0]-remaining)<1e-5
                    samples.append({'split':split,**record,'review':'independent source-suffix scan PASS'})
            if qi%10000==0:write(out/'progress.json',{'phase':'dataset','split':split,'completed_queries':qi,'queries':n})
        f.close()
        for x in arr.values():x.flush()
        assert matched==counts[split],(split,matched,counts[split])
        audit['splits'][split]={'queries':n,'original_queries_matched':matched,'candidate_histogram':dict(candidate_count),'class_distribution_including_unknown':dict(distribution),
            'censored_candidates':censor_count,'classification_valid':valid_count,'candidate_pairs':sum(distribution.values()),'status':'PASS'}
        write(dest/'meta.json',{'source_meta':meta,'config':cfg,'session_ids':sid_names,'audit':audit});print(split,audit['splits'][split],flush=True)
    # Mandatory >=100 candidate checks are performed by validate.py, which
    # samples only nonempty queries. Uniform query sampling can undershoot.
    with (dest/'manual-review-samples.json').open('w') as f:json.dump(samples,f,ensure_ascii=False,indent=2)
    audit['source_scanned_sample_count']=len(samples);audit['status']='PENDING_INDEPENDENT_VALIDATION';write(dest/'validation.json',audit)
    (out/'DATASET-VALIDATION.md').write_text('# Dataset validation\n\nPhase 3: PENDING independent validator\n\n'+json.dumps(audit,ensure_ascii=False,indent=2)+'\n\n随机查询样本已独立扫描源事件后缀核对；必须继续运行validate.py完成至少100条候选审阅；完整人工可审阅记录见 dataset/manual-review-samples.json。本报告不把自动核对称为用户人工验收。\n\n未知重入不能填成C7；仅当观测至少3600秒且未返回时可唯一判C7。原分区交叉session保留但切断未来标签，不声称session-disjoint。当前时长编码保持旧最小1秒，审阅字段current_duration使用真实0秒。\n')
    write(out/'progress.json',{'phase':'dataset','status':'PENDING_VALIDATION','next':'validate.py then tests then training'})


if __name__=='__main__':main()
