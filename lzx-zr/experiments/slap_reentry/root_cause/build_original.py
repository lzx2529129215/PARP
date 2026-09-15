from common import *
from collections import defaultdict
from models.app_lstm_visit_explicit import explicit_features,FEATURE_NAMES

def main():
    rawmeta=json.loads(Path(CFG['raw_meta']).read_text());vocab=rawmeta['app_vocab'];A=len(vocab)
    raw=read_segments(CFG['raw_segments'],vocab);mapped=read_segments(BASE/'baseline/dataset/segments.csv',META['app_vocab']);sids=json.loads((BASE/'dataset/meta.json').read_text())['session_ids']
    spec=json.loads(Path(META['identity_mapping']).read_text());mapping={a:r['mapped_app'] for r in spec['mapping_rules'] for a in r['lsapp_apps']};inv={i:a for a,i in vocab.items()};mids={i:META['app_vocab'][mapping.get(a,'<UNKNOWN>')] for i,a in inv.items()}
    times={};mtimes={};groups={};entries={};merged=0
    for sid,rows in raw.items():
        rt=[r['t'] for r in rows];times[sid]=rt;mtimes[sid]=[r['t'] for r in mapped[sid]];g=[]
        for i,r in enumerate(rows):
            if not i or mids[r['a']]!=mids[rows[i-1]['a']]:g.append(i)
        assert [(rows[i]['t'],mids[rows[i]['a']]) for i in g]==[(r['t'],r['a']) for r in mapped[sid]],sid
        groups[sid]=g+[len(rows)];merged+=len(rows)-len(g);by=defaultdict(list)
        for i,r in enumerate(rows):by[r['a']].append((r['t'],i))
        entries[sid]={a:([t for t,i in v],[i for t,i in v]) for a,v in by.items()}
    dest=OUT/'dataset/original';dest.mkdir(exist_ok=True);validation={'status':'BUILDING','source_segments':sum(map(len,raw.values())),'mapped_segments':sum(map(len,mapped.values())),'collapsed_segments':merged,'splits':{},'query_policy':CFG['mapping_comparison'],'omitted_queries':'only zero-raw-candidate anchors omitted; retained old_query_index links to all original timestamps; all valid-supervision anchors retained'}
    for split in ['train','val','test']:
        old=load_data(BASE/'dataset'/split);anchors=[];tie_resolved=0;used_ties=defaultdict(set)
        for qi,q in enumerate(old['query_time']):
            sid=sids[int(old['session'][qi])];mt=mtimes[sid];idx=bisect.bisect_right(mt,q)-1
            if old['trigger'][qi]==0:
                lo=bisect.bisect_left(mt,q);hi=bisect.bisect_right(mt,q)
                if hi-lo>1:
                    h=old['history_apps'][qi][old['history_mask'][qi]>0].tolist()
                    possible=[j for j in range(lo,hi) if [r['a'] for r in mapped[sid][max(0,j-19):j+1]]==h]
                    possible=[j for j in possible if j not in used_ties[(sid,q)]]
                    assert possible,(sid,q,possible);idx=possible[0];used_ties[(sid,q)].add(idx);tie_resolved+=1
            assert mapped[sid][idx]['a']==old['current_app'][qi]
            start,end=groups[sid][idx:idx+2]
            ri=start if old['trigger'][qi]==0 else min(end-1,bisect.bisect_right(times[sid],q)-1)
            r=raw[sid][ri];assert mids[r['a']]==old['current_app'][qi]
            if set(r['opened'])-{r['a']}:anchors.append((qi,ri))
        n=len(anchors);path=dest/split;path.mkdir(exist_ok=True);arrays={}
        for k in ['history_apps','history_durations','history_mask','opened_apps','current_app','time_feature','user_group','explicit','eligible','labels','label_valid','remaining','censored','observed_s','query_time','session','user','trigger']:
            shape=(n,)+old[k].shape[1:]
            if k in ['opened_apps','eligible','labels','label_valid','remaining','censored']:shape=(n,A)
            if k=='explicit':shape=(n,A,len(FEATURE_NAMES))
            arrays[k]=np.lib.format.open_memmap(path/f'{k}.npy',mode='w+',dtype=old[k].dtype,shape=shape)
        arrays['old_query_index']=np.lib.format.open_memmap(path/'old_query_index.npy',mode='w+',dtype='int64',shape=(n,))
        arrays['history_apps'][:]=vocab['<PAD>'];arrays['labels'][:]=-1;arrays['remaining'][:]=np.nan
        count=valid=observed=0;checks=[];rng=np.random.default_rng(42);review=set(rng.choice(n,min(100,n),replace=False).tolist())
        for row,(qi,ri) in enumerate(anchors):
            sid=sids[int(old['session'][qi])];q=old['query_time'][qi];r=raw[sid][ri];h=raw[sid][max(0,ri-19):ri+1];apps=[x['a'] for x in h];ts=[x['t'] for x in h];L=len(h)
            for k in ['query_time','session','user','trigger','observed_s','time_feature','user_group']:arrays[k][row]=old[k][qi]
            arrays['old_query_index'][row]=qi;arrays['current_app'][row]=r['a'];arrays['history_apps'][row,-L:]=apps;arrays['history_durations'][row,-L:]=[x['d'] for x in h[:-1]]+[max(1,q-r['t'])];arrays['history_mask'][row,-L:]=1
            arrays['opened_apps'][row,r['opened']]=1;arrays['explicit'][row]=explicit_features(apps,ts,q,A);cands=sorted(set(r['opened'])-{r['a']});arrays['eligible'][row,cands]=True
            until=q+old['observed_s'][qi];assert abs(r['obs']-mapped[sid][0]['obs'])<1e-5
            assert all(t<=q for t in ts)
            for a in cands:
                tlist,eventids=entries[sid][a];pos=bisect.bisect_right(tlist,q);nxt=tlist[pos] if pos<len(tlist) else None
                rem,cl,cens,lv=label(q,nxt,until,CFG['reentry_bins']);arrays['remaining'][row,a]=rem;arrays['labels'][row,a]=cl;arrays['censored'][row,a]=cens;arrays['label_valid'][row,a]=lv
                count+=1;valid+=lv;observed+=not cens
                if row in review:
                    scan=next((x['t'] for x in raw[sid][ri+1:] if x['a']==a and q<x['t']<=until),None);check=label(q,scan,until,CFG['reentry_bins']);assert check[1:]==(cl,cens,lv)
                    checks.append({'old_query_index':int(qi),'raw_row':row,'app':inv[a],'query':float(q),'next_entry':scan,'class':int(cl),'censored':bool(cens)})
            if row%20000==0:write(OUT/'metrics/original-build-progress.json',{'split':split,'row':row,'rows':n})
        for x in arrays.values():x.flush()
        assert not np.any(arrays['eligible'][np.arange(n),arrays['current_app']]);assert np.all(arrays['label_valid']<=arrays['eligible']);assert len(checks)>=100
        write(path/'source-review.json',checks);validation['splits'][split]={'queries':n,'all_shared_anchors':len(old['query_time']),'candidate_rows':count,'valid_rows':int(valid),'observed_rows':int(observed),'source_checks':len(checks),'same_timestamp_anchors_resolved':tie_resolved}
        write(dest/'validation.json',validation);print(split,validation['splits'][split],flush=True)
    validation['status']='PASS';write(dest/'validation.json',validation)
if __name__=='__main__':main()
