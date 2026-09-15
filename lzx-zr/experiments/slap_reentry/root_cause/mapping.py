from common import *
from collections import defaultdict,Counter

def intervals(sessions,excluded=frozenset()):
    intervals=[];entries=0;switches=0
    for sid,rows in sessions.items():
        last={};switches+=max(0,len(rows)-1)
        for i,r in enumerate(rows):
            a=r['a'];t=r['t'];entries+=1
            if a in last and a not in excluded:intervals.append(t-last[a])
            if i+1<len(rows):last[a]=rows[i+1]['t']
    a=np.asarray(intervals);return {'segments':entries,'switches':switches,'completed_reentries':len(a),'background_interval_sec':stats(a),'fraction_lt30':float((a<30).mean()),'fraction_lt180':float((a<180).mean()),'fraction_gt600':float((a>600).mean())}

def main():
    rm=json.loads(Path(CFG['raw_meta']).read_text());raw=read_segments(CFG['raw_segments'],rm['app_vocab']);mapped=read_segments(BASE/'baseline/dataset/segments.csv',META['app_vocab']);spec=json.loads(Path(META['identity_mapping']).read_text());rules={r['mapped_app']:r['lsapp_apps'] for r in spec['mapping_rules']};lookup={a:t for t,aa in rules.items() for a in aa};inv={i:a for a,i in rm['app_vocab'].items()}
    source=set(inv.values())-{'<PAD>','<UNKNOWN>'};retained=lost=aba=lostaba=0;per=Counter()
    for sid,rows in raw.items():
        for left,right in zip(rows,rows[1:]):
            if lookup.get(inv[left['a']],'<UNKNOWN>')==lookup.get(inv[right['a']],'<UNKNOWN>'):lost+=1;per[lookup.get(inv[left['a']],'<UNKNOWN>')]+=1
            else:retained+=1
        for a,b,c in zip(rows,rows[1:],rows[2:]):
            if a['a']==c['a'] and a['a']!=b['a']:
                aba+=1;lostaba+=lookup.get(inv[a['a']],'<UNKNOWN>')==lookup.get(inv[b['a']],'<UNKNOWN>')
    r=intervals(raw);m=intervals(mapped);assert retained==m['switches']
    result={'source_apps':len(source),'mapped_real_apps':30,'mapped_sources':len(source&lookup.keys()),'unmapped_source_apps':sorted(source-lookup.keys()),'many_to_one':{k:{'source_count':len(set(v)&source),'sources':v,'collapsed_switches':per[k]} for k,v in rules.items()},'raw':r,'mapped':m,'mapped_real_apps_only':intervals(mapped,{META['app_vocab']['<UNKNOWN>'],META['app_vocab']['<PAD>']}),'switch_retention':retained/(retained+lost),'collapsed_switches':lost,'raw_ABA_triples':aba,'collapsed_ABA_triples':lostaba,'definitions':'same derived sessions, foreground identity transitions; reentry interval=last foreground exit to next entry; not query remaining time; includes only completed within-session returns'}
    write(OUT/'metrics/mapping_distortion.json',result)
    lines=['# Mapping distortion','','Same user/session/time inventory. Source foreground segments are built using the existing merge rule; raw Closed is not interpreted as OS process death. Completed return intervals use foreground exit → next entry, distinct from query remaining time.','',f'Source apps={len(source)}, target apps=30, source-to-target average={len(source&lookup.keys())/30:.3f}; unmapped={len(source-lookup.keys())}.',f'Switch retention={result["switch_retention"]:.3%}; {lost:,} source switches disappear. A→B→A collapsed triples={lostaba:,}/{aba:,}.','', '| Metric | Raw | Mapped |','|---|---:|---:|']
    for k in ['segments','switches','completed_reentries','fraction_lt30','fraction_lt180','fraction_gt600']:lines.append(f'| {k} | {r[k]} | {m[k]} |')
    for k in ['p25','median','p75']:lines.append(f'| interval {k} (s) | {r["background_interval_sec"][k]} | {m["background_interval_sec"][k]} |')
    lines+=['', 'The main transition distribution includes UNKNOWN as an explicit boundary identity; an additional mapped_real_apps_only interval distribution excludes special identities without joining across their intervening segments. Raw 87 versus mapped 30 also changes coverage: 15 source identities become UNKNOWN. Full source groups and per-target collapse counts: metrics/mapping_distortion.json. A change in the target process is established when identities collapse; whether it harms predictability additionally requires the matched-time model comparison. Distribution change alone is not proof that mapping is the main model bottleneck.']
    (OUT/'MAPPING-DISTORTION.md').write_text('\n'.join(lines)+'\n');print(json.dumps({k:v for k,v in result.items() if k!='many_to_one'}),flush=True)
    # Metadata-only historical reservation. No labels or metrics of the reserved
    # slice are computed under this new split. Prior exposure remains disclosed.
    rows=sorted([(v[0]['t'],v[0]['obs'],sid,v[0]['user']) for sid,v in mapped.items()]);cuts=[rows[int(len(rows)*x)][0] for x in [.6,.75,.9]];parts={k:[] for k in ['train','validation','development','retrospective_reserved']};quarantine=[]
    for begin,end,sid,u in rows:
        k=bisect.bisect_right(cuts,begin);name=list(parts)[k]
        if k<3 and end>=cuts[k]:quarantine.append(sid)
        else:parts[name].append(sid)
    manifest={'status':'RETROSPECTIVE_SPLIT_FROZEN; GENUINELY_UNSEEN_FINAL_HOLDOUT_PENDING','old_test_status':'development permanently; all old train/val/test has been accessed during this or prior experiments','partition_basis':'chronological 60/75/90% derived-session start quantiles; whole crossing sessions quarantined; equal start timestamps kept together','boundaries_epoch_s':cuts,'partitions':{k:{'session_count':len(v),'session_ids':v} for k,v in parts.items()},'quarantined_cross_boundary_sessions':quarantine,'retrospective_reserved_warning':'The last 10% session region was previously exposed. Locking it now does not make it clean; never claim independent final generalization from it. It has not been used to select any root-pack model under this new partition.','final_holdout':{'status':'PENDING_NEW_UNSEEN_DATA','source':'future collected real PC trace or independently obtained unexamined trace','existing_clean_sessions':0,'access_policy':'no model tuning, boundary selection, diagnosis or metrics until model and final evaluation protocol are frozen'},'root_pack_split':'unchanged frozen historical M0 split for all Phase 1–6 diagnostics','source_sha256':META['source_sha256']}
    write(OUT/'HOLDOUT-MANIFEST.json',manifest)
if __name__=='__main__':main()
