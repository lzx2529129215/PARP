"""Read-only analysis of the fixed-high cohort: prepeak space and peak-only costs."""
import argparse
import json
import math
from pathlib import Path
import statistics
import time

MIB=1024**2
APPS=('gimp','libreoffice','firefox')


def load(path):
    for attempt in range(6):
        try:return json.loads(Path(path).read_text(encoding='utf-8'))
        except json.JSONDecodeError:
            if attempt==5:raise
            time.sleep(.05)


def pct(values,q):
    return sorted(values)[max(0,math.ceil(len(values)*q)-1)] if values else None


def overlap(a,b,start,end):return max(0.,min(b,end)-max(a,start))


def pause_time(events,start,end,finished):
    began=None;total=0.
    for e in events:
        if e['event']=='paused':began=e['time_s']
        elif e['event'] in ('resumed','terminated') and began is not None:
            total+=overlap(began,e['time_s'],start,end);began=None
    if began is not None:total+=overlap(began,finished,start,end)
    return total


def summary(row,ref_growth,start=8.,end=22.):
    origin=row['start_monotonic_s'];before=row.get('before_peak');initial=row['initial'];quota=row['quota_bytes']
    responses=[r for r in row.get('responses',[]) if start<=r.get('phase_seconds',-1)<end]
    latency=[r['latency_ms'] for r in responses if r.get('latency_ms')is not None]
    scheduled=[r['scheduled_response_ms'] for r in responses if r.get('scheduled_response_ms')is not None]
    finished=row.get('final',{}).get('monotonic_ns',int((origin+end)*1e9))/1e9-origin
    result=dict(tag=row['tag'],stage=row['stage'],enabled=row['enabled'],status=row['status'],
        valid=not row.get('schedule_late_operations') and not row.get('service_errors') and row.get('cleanup',{}).get('complete')is True,
        source_contract=row.get('execution_contract_sha256'),window_s=[start,end],
        input_samples=len(latency),response_p50_ms=statistics.median(latency)if latency else None,
        response_p95_ms=pct(latency,.95),response_max_ms=max(latency)if latency else None,
        scheduled_response_p95_ms=pct(scheduled,.95),scheduled_response_max_ms=max(scheduled)if scheduled else None,
        timeouts=sum(bool(r.get('timeout'))for r in responses),missed_input_slots=sum(r.get('kind')=='missed_slot'for r in responses),
        full_workload_faults=row.get('total_faults'),peak_operations={},peak_active_overlap_s={},
        peak_protection_pause_s={},peak_kernel_write_s={},prepeak_reclaim_requested_MiB={},
        fixed_high_MiB={a:v['target_bytes']/MIB for a,v in row.get('fixed_sequence_high',{}).items()},
        queue_delays=row.get('schedule_queue_delays',[]),cleanup=row.get('cleanup'))
    if before is not None:
        initial_free=quota-initial['current'];pre_free=quota-before['current']
        result.update(prepeak_net_drop_MiB=(initial['current']-before['current'])/MIB,
            prepeak_donor_net_drop_MiB=sum(initial['groups'][a]['current']-before['groups'][a]['current']for a in APPS)/MIB,
            prepeak_headroom_MiB=pre_free/MIB,prepeak_sample_s=before['monotonic_ns']/1e9-origin,
            reference_growth_MiB=ref_growth/MIB if ref_growth is not None else None,
            required_from_start_MiB=max(0,ref_growth-initial_free)/MIB if ref_growth is not None else None,
            remaining_gap_MiB=max(0,ref_growth-pre_free)/MIB if ref_growth is not None else None,
            space_ready=(pre_free>=ref_growth)if ref_growth is not None else None)
    for app in APPS:
        ops=[o for o in row.get('operations',[]) if o['app']==app]
        result['peak_operations'][app]=[dict(kind=o['kind'],elapsed_ms=o['elapsed_ms'],
            start_s=o['start_ns']/1e9-origin,end_s=o['end_ns']/1e9-origin,
            crosses_peak_end=o['end_ns']/1e9-origin>end,queue_delay_s=o.get('queue_delay_s'),
            dispatch_lateness_s=o.get('dispatch_lateness_s'))for o in ops if o['kind']=='peak_new_data']
        result['peak_active_overlap_s'][app]=sum(overlap(o['start_ns']/1e9-origin,o['end_ns']/1e9-origin,start,end)for o in ops)
        events=row.get('service',{}).get(app,{}).get('events',[])
        result['peak_protection_pause_s'][app]=pause_time(events,start,end,finished)
        writes=row.get('writes',{}).get(app,[])
        result['peak_kernel_write_s'][app]=sum(overlap(w['start_ns']/1e9-origin,w['end_ns']/1e9-origin,start,end)for w in writes)
        result['prepeak_reclaim_requested_MiB'][app]=sum(int(w['value'])for w in writes if w['name']=='memory.reclaim'and w['start_ns']/1e9-origin<start)/MIB
    return result


def mean(rows,key):
    v=[r[key]for r in rows if r.get(key)is not None]
    return statistics.mean(v)if v else None


def main():
    p=argparse.ArgumentParser();p.add_argument('--root',type=Path,required=True);p.add_argument('--scene',default='revisit');args=p.parse_args()
    root=args.root;run=load(root/'results'/('RUN-'+args.scene+'.json'));ref=run.get('reference');growth=None
    if ref and ref.get('status')=='COMPLETE':
        occupancy=root/'results/trials'/ref['tag']/'occupancy.jsonl'
        values=[json.loads(l)['total']for l in occupancy.read_text(encoding='utf-8').splitlines()if json.loads(l)['time_s']<22]
        if values:growth=max(0,max(values)-ref['initial']['current'])
    rows=[summary(r,growth)for r in run.get('trials',[]) if r.get('status')=='COMPLETE']
    reference=summary(ref,growth)if ref and ref.get('status')=='COMPLETE'else None
    baseline=[r for r in rows if r['stage']=='baseline'and r['valid']]
    groups={}
    for stage in ('baseline','calibration','formal'):
        selected=[r for r in rows if r['stage']==stage and r['valid']]
        groups[stage]=dict(count=len(selected),prepeak_net_drop_MiB=mean(selected,'prepeak_net_drop_MiB'),
            remaining_gap_MiB=mean(selected,'remaining_gap_MiB'),response_p95_ms=mean(selected,'response_p95_ms'),
            response_max_ms=mean(selected,'response_max_ms'),ready_count=sum(r.get('space_ready')is True for r in selected))
    doc=dict(status=run['status'],scene=args.scene,source_run=str(root/'results'/('RUN-'+args.scene+'.json')),
        definitions=dict(peak_interval_s=[8,22],responses='input initiated in [8,22); timeout/missed slots retained separately',
            operation_time='peak_new_data full completion time; crossings explicitly marked; no whole-task elapsed comparison',
            active_time='operation overlap with [8,22), per app; not summed as foreground stall',
            net_drop='observed occupancy change, not exact old-page reclamation',
            space='reference growth before 22s versus headroom before first growth gate',
            percentile='nearest-rank; aggregate is mean of per-run P95/max; calibration separated from formal'),
        reference=reference,rounds=rows,groups=groups,total_fault_calibration=run.get('calibration'),
        formal_total_faults=run.get('formal_total_faults'),error=run.get('error'))
    target=root/'results'/('PEAK_SUMMARY-'+args.scene+'.json')
    target.write_text(json.dumps(doc,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(dict(status=doc['status'],reference=reference,rounds=rows,groups=groups,error=doc['error']),ensure_ascii=False))


if __name__=='__main__':main()
