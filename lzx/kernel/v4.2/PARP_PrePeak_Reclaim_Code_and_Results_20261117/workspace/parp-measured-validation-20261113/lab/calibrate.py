"""Finite real-app calibration. Stops descent at first rejected/unknown level."""
from dataclasses import asdict
import argparse
import gc
import json
from pathlib import Path
import sys
import time

HERE=Path(__file__).resolve().parent;ROOT=HERE.parent
sys.path.insert(0,str(ROOT/'controller'))
from faults import FaultPolicy
from safety_store import SafetyPoint,select
from suite import run,save,load,MIB,WORKLOAD_VERSION
from reference import measured_reference,pgfault,task_ms,sha,compressed

LEVELS={'gimp':(16,32,64,128,256),'libreoffice':(8,16,32,64,96),'firefox':(16,32,64,128)}


def path_of(row):return ROOT/'results/trials'/row['tag']/'result.json'


def existing(stage,scenario):
    rows=[]
    for path in sorted((ROOT/'results/trials').glob('*/result.json'),key=lambda p:p.stat().st_mtime):
        row=load(path)
        if row.get('workload_version')==WORKLOAD_VERSION and row.get('stage')==stage and row.get('scenario')==scenario and row.get('status')=='COMPLETE' and row.get('cleanup',{}).get('complete'):
            rows.append(row)
    return rows


def checked(row):
    if row['status']!='COMPLETE' or not row['cleanup']['complete']:raise RuntimeError('failed native trial retained; no further pressure')
    if row.get('telemetry_error'):raise RuntimeError('incomplete metrics')
    return row


def point(baseline,trial,app,method):
    t=trial['before_peak']['monotonic_ns']/1e9
    rows=[r for r in trial['root_telemetry'] if r['window_end']<=t]
    if not rows or t-rows[-1]['window_end']>1.6:raise ValueError('paired prepeak RSS/cgroup sample missing')
    item=rows[-1]['apps'][app]
    initial=trial['initial']['groups'][app]
    return SafetyPoint(app,method,item['rss_sum_bytes'],item['current'],trial['calibration_intervention']['amount_bytes'],
        max(0,initial['current']-item['current']),pgfault(baseline,app),pgfault(trial,app),task_ms(baseline,app),task_ms(trial,app),
        str(path_of(baseline)),sha(path_of(baseline)),str(path_of(trial)),sha(path_of(trial)),
        'combined-old-and-new-hot-plus-old-document-revisit',True,compressed(trial,app))


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--restart-preparation',action='store_true');parser.add_argument('--resume',action='store_true');args=parser.parse_args()
    output=ROOT/'results/CALIBRATION.json'
    resumed=None
    if output.exists():
        old=load(output)
        if args.resume and old['status']=='STOPPED' and old.get('workload_version')==WORKLOAD_VERSION:
            resumed=old
        elif not args.restart_preparation or old['status']!='STOPPED' or old.get('points'):
            raise RuntimeError('existing calibration: do not silently restart or mix trials')
        archive=ROOT/'results'/('CALIBRATION-preparation-'+str(int(time.time()))+'.json')
        save(archive,old)
    state=dict(status='RUNNING',started_wall_s=time.time(),references={},baselines=[],points={},selected={},
        requested_levels_MiB=LEVELS,relative_fault_limit=.10,absolute_zero_limit=None,
        selection='first failing or undetermined level stops descent; no claimed optimum',
        workload_version=WORKLOAD_VERSION,
        protected_work='combined scene includes old and new accesses plus complete old-document revisit',
        elapsed_time_is_reported=True,elapsed_hard_limit=None)
    if resumed is not None:
        state=resumed
        state.setdefault('resumptions',[]).append(dict(wall_s=time.time(),previous_error=state.get('error')))
        state['status']='RUNNING';state.pop('error',None)
    save(output,state)
    try:
        for scene in ('stable','turnover','revisit'):
            candidates=existing('reference',scene)
            row=candidates[0] if candidates else checked(run(scene,False,stage='reference'))
            measured_reference(path_of(row))
            state['references'][scene]=str(path_of(row));save(output,state)
            print('MEASURED_REFERENCE_READY '+scene,flush=True)
        candidates=existing('baseline','combined')
        for repeat in range(2):
            row=candidates[repeat] if len(candidates)>repeat else checked(run('combined',False,stage='baseline'))
            if str(path_of(row)) not in state['baselines']:state['baselines'].append(str(path_of(row)))
            save(output,state)
            print('CALIBRATION_BASELINE_READY %d/2'%(repeat+1),flush=True)
        baselines=[load(path) for path in state['baselines']]
        policy=FaultPolicy(.10,None)
        for app,levels in LEVELS.items():
            # Lower fault baseline of two repeats is conservative, not chosen
            # based on any enabled result. Both raw controls remain retained.
            baseline=min(baselines,key=lambda row:pgfault(row,app))
            state['points'].setdefault(app,{});state['selected'].setdefault(app,{})
            for method in ('high','reclaim'):
                stored=state['points'][app].setdefault(method,[])
                points=[SafetyPoint(**row['point']) for row in stored]
                if points:
                    selected=select(points,policy)
                    if (stored[-1]['fault_check']['within_trial_limit'] is not True or selected is not points[-1]
                            or len(points)==len(levels)):
                        continue
                for amount in levels[len(points):]:
                    print('CALIBRATING %s %s %dMiB'%(app,method,amount),flush=True)
                    trial=checked(run('combined',False,stage='calibration',
                        calibration=dict(app=app,method=method,amount_bytes=amount*MIB)))
                    candidate=point(baseline,trial,app,method);check=candidate.verify(policy)
                    points.append(candidate)
                    state['points'][app][method].append(dict(point=asdict(candidate),fault_check=check,
                        task_duration_growth=(candidate.current_task_ms/candidate.baseline_task_ms-1) if candidate.baseline_task_ms else None))
                    selected=select(points,policy)
                    state['selected'][app][method]=asdict(selected) if selected is not None else None
                    save(output,state)
                    print('CALIBRATION_LEVEL %s %s %dMiB %s net_release=%.2fMiB fault_growth=%s'%(
                        app,method,amount,check['within_trial_limit'],candidate.observed_net_release_bytes/MIB,
                        check['relative_growth']),flush=True)
                    if check['within_trial_limit'] is not True or selected is None or selected is not candidate:break
                    gc.collect()
        state['status']='COMPLETE'
    except BaseException as exc:
        state.update(status='STOPPED',error=repr(exc));raise
    finally:
        state['ended_wall_s']=time.time();save(output,state)
        print('CALIBRATION_STATUS '+state['status'],flush=True)


if __name__=='__main__':main()
