"""Analyze only the new compressed formal cohort; keep calibration separate."""
import json
from pathlib import Path
import statistics
import sys

HERE=Path(__file__).resolve().parent;ROOT=HERE.parent
sys.path.insert(0,str(ROOT/'controller'))
from faults import FaultPolicy
from reference import measured_reference,pgfault,task_ms,compressed


def load(path):return json.loads(Path(path).read_text())


def pause_spans(events):
    start=None;spans=[]
    for event in events:
        if event['event']=='paused':start=event['time_s']
        elif event['event'] in ('resumed','terminated') and start is not None:
            spans.append((start,max(start,event['time_s'])));start=None
    return spans


def row_summary(row,reference):
    counts={kind:sum(e['event']==kind for app in row['service'].values() for e in app['events']) for kind in ('high','reclaim','paused','resumed')}
    net=(row['initial']['current']-row['before_peak']['current'])/2**20
    growth=max(q['total'] for q in reference['occupancy'] if q['time_s']<22)-reference['occupancy'][0]['total']
    required=max(0,growth-(row['quota_bytes']-row['initial']['current']))
    gap=max(0,growth-(row['quota_bytes']-row['before_peak']['current']))
    values=[r['latency_ms'] for r in row['responses'] if 8<=r.get('phase_seconds',-1)<22 and 'latency_ms'in r]
    apps=row['initial']['groups']
    kernel=sum(row['before_peak']['groups'][app]['stats'].get('pgsteal',0)-row['initial']['groups'][app]['stats'].get('pgsteal',0)
               for app in ('gimp','libreoffice','firefox'))*4096/2**20
    return dict(tag=row['tag'],enabled=row['enabled'],calls=counts,
        first_growth_required_MiB=required/2**20,pre_growth_net_release_MiB=net,pre_growth_gap_MiB=gap/2**20,
        pre_growth_kernel_pgsteal_MiB=kernel,
        peak_response_median_ms=statistics.median(values),peak_response_max_ms=max(values),
        pgfault={app:pgfault(row,app) for app in apps},
        pgmajfault={app:row['final']['groups'][app]['stats']['pgmajfault']-row['initial']['groups'][app]['stats']['pgmajfault'] for app in apps},
        task_ms={app:task_ms(row,app) for app in apps if app!='krita'},
        old_revisit_ms={op['app']:op['elapsed_ms'] for op in row['operations'] if op['kind']=='old_document_revisit'},
        protection_pause_seconds={app:sum(b-a for a,b in pause_spans(state['events'])) for app,state in row['service'].items()},
        kernel_write_seconds={app:sum((w['end_ns']-w['start_ns'])/1e9 for w in writes) for app,writes in row['writes'].items()},
        online_prediction_calls=len(row['online_forecasts']),
        prediction_requirement_range_MiB=[min(f['required_bytes'] for f in row['online_forecasts'])/2**20,max(f['required_bytes'] for f in row['online_forecasts'])/2**20],
        compressed_pages_observed={app:compressed(row,app) for app in apps},
        numeric_inputs_logged=len(row['numeric_policy_inputs']),service_errors=row['service_errors'],cleanup=row['cleanup'])


def main():
    formal=load(ROOT/'results/FORMAL.json');cal=load(ROOT/'results/CALIBRATION.json');policy=FaultPolicy(.10,None)
    refs={scene:measured_reference(path) for scene,path in cal['references'].items()};scenes={}
    for row in formal['trials']:
        if row['status']=='COMPLETE':scenes.setdefault(row['scenario'],{})['on' if row['enabled'] else 'off']=row_summary(row,refs[row['scenario']])
    for scene,pair in scenes.items():
        if set(pair)!={'off','on'}:continue
        off,on=pair['off'],pair['on']
        pair['fault_check']={app:policy.compare(off['pgfault'][app],on['pgfault'][app]) for app in off['pgfault']}
        pair['fault_check']['all_apps']=policy.compare(sum(off['pgfault'].values()),sum(on['pgfault'].values()))
        pair['all_apps_within_10_percent']=all(c['within_trial_limit'] is True for c in pair['fault_check'].values())
        pair['first_growth_space_ready']=on['pre_growth_gap_MiB']==0
    summary=dict(status=formal['status'],formal_trials=len(formal['trials']),sources_unchanged=formal.get('sources_unchanged'),
        scenes=scenes,safety=load(ROOT/'results/APPROVED_SAFETY.json'),
        limitations=['four-app fixed-capacity test domain; not physical 16GiB exhaustion',
            'measured future replay, not a trained model','one pair per scenario; not a statistical optimum',
            'Referenced WSS uses the original shared-file lower-bound convention'])
    (ROOT/'results/SUMMARY.json').write_text(json.dumps(summary,ensure_ascii=False,indent=1))
    print(json.dumps(summary,ensure_ascii=False,indent=1))


if __name__=='__main__':main()
