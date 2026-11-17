"""Run the original three off/on comparisons only after explicit feature gates."""
from dataclasses import asdict
import argparse
import hashlib
import json
from pathlib import Path
import sys
import time
import tempfile
import subprocess

HERE=Path(__file__).resolve().parent;ROOT=HERE.parent
sys.path.insert(0,str(ROOT/'controller'))
from suite import run,load,save,WORKLOAD_VERSION,MIB
from reference import measured_reference,sha,pgfault
from safety_store import SafetyPoint,select
from online_service import Service


def sources():
    return {str(path.relative_to(ROOT)):sha(path) for directory in (ROOT/'controller',ROOT/'lab') for path in directory.glob('*.py')}


def gate():
    cal=load(ROOT/'results/CALIBRATION.json')
    if cal['status']!='COMPLETE' or cal['workload_version']!=WORKLOAD_VERSION:
        raise RuntimeError('complete matching safety calibration required')
    refs={scene:measured_reference(path) for scene,path in cal['references'].items()}
    if set(refs)!={'stable','turnover','revisit'}:raise RuntimeError('three real WSS/pgfault references required')
    if any(r['trial'].get('workload_version')!=WORKLOAD_VERSION for r in refs.values()):raise RuntimeError('mixed startup/workload variants')
    accepted={};compressed=False
    approved_points={};scope_checks={}
    for app,methods in cal['points'].items():
        accepted[app]={}
        approved_points[app]={};scope_checks[app]={}
        # Combined calibration has additional old-document work. Do not let
        # that larger baseline hide excessive cost for the shorter scenarios.
        allowed_extra=min(pgfault(ref['trial'],app) for ref in refs.values())*.10
        for method,rows in methods.items():
            prefix=[];checks=[]
            for item in rows:
                candidate=SafetyPoint(**item['point']);check=candidate.verify()
                scope_ok=check['within_trial_limit'] is True and candidate.current_pgfault-candidate.baseline_pgfault<=allowed_extra
                checks.append(dict(trial_sha256=candidate.trial_sha256,allowed_absolute_extra=allowed_extra,
                    paired_extra=candidate.current_pgfault-candidate.baseline_pgfault,scope_approved=scope_ok))
                if not scope_ok:break
                prefix.append(candidate)
            scope_checks[app][method]=checks
            point=select(prefix)
            approved_points[app][method]=asdict(point) if point is not None else None
            if point is None:continue
            if point.verify()['within_trial_limit'] is not True:raise RuntimeError('uncertified safety point')
            trial=load(point.trial_path)
            actual=max((e.get('used_bytes',0) for e in trial['calibration_events'] if e['event']=='terminated'),default=0)
            if not actual:continue
            accepted[app][method]=dict(actual_attempt_cap_bytes=min(actual,point.requested_bytes),
                maximum_net_allowance_bytes=min(actual,point.requested_bytes,point.observed_net_release_bytes),
                retained_rss_bytes=point.retained_rss_bytes,retained_cgroup_bytes=point.retained_cgroup_bytes,
                baseline_pgfault=point.baseline_pgfault,current_pgfault=point.current_pgfault)
            compressed=compressed or point.compressed_pages_observed
    if not any(accepted.values()):raise RuntimeError('no app/method has a measured safe reclaim level')
    if not compressed:raise RuntimeError('no actual compressed-page evidence; do not label this a compression test')
    save(ROOT/'results/APPROVED_SAFETY.json',dict(status='COMPLETE',calibration_sha256=sha(ROOT/'results/CALIBRATION.json'),
        selected=approved_points,scope_checks=scope_checks,
        context='same combined task paired against its controls; additionally cap extra faults by smallest reference-task allowance'))
    # Keep one absolute capacity across all six runs. It is chosen from the
    # reference BEFORE any enabled formal result, not tuned to an on outcome.
    initial=max(r['trial']['initial']['current'] for r in refs.values())
    quota=((initial+620*MIB+64*MIB-1)//(64*MIB))*(64*MIB)
    return cal,refs,accepted,quota


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--check',action='store_true');args=parser.parse_args()
    if args.check:
        cal,refs,accepted,quota=gate()
        test=subprocess.run(['python3','-B','-m','unittest','discover','-s',str(ROOT/'controller'),'-p','test_measured.py'],
                            capture_output=True,text=True,check=True)
        class ReadOnlyBackend:
            def __init__(self,path,directory,app):self.app=app;self.writes=[]
            def usage(self):return initial['groups'][self.app]['current']
            def feedback(self):return 0,0
            def read_high(self):return None
            def write_high(self,_):raise AssertionError('preflight may not write a kernel control')
            def read_freeze(self):return 0
        previews={}
        for scene,ref in refs.items():
            initial=ref['trial']['initial']
            with tempfile.TemporaryDirectory(prefix='parp-plan-preflight-') as name:
                base=Path(name);(base/'memory.current').write_text(str(initial['current']))
                service=Service(dict(reference_path=cal['references'][scene]),initial,base,base,'preflight',quota,False,
                                backend_factory=ReadOnlyBackend)
                for now in (0,.5,1,3,5,7,7.8,8.1,12,20,22,26):service.tick(now,base)
                service.close(30)
                report=service.report()
                if not report['numeric_policy_inputs'] or len(report['online_forecasts'])<4:raise RuntimeError('incomplete rolling numeric pipeline')
                previews[scene]=dict(real_numeric_inputs=len(report['numeric_policy_inputs']),
                    online_forecasts=len(report['online_forecasts']),kernel_writes=0)
        result=dict(status='PASS',unit_test_output=test.stderr,approved_points=accepted,
                    previews=previews,native_memory_policy_executed=False)
        save(ROOT/'results/PREFLIGHT.json',result);print(json.dumps(result,ensure_ascii=False));return
    target=ROOT/'results/FORMAL.json'
    if target.exists():raise RuntimeError('do not overwrite or silently repeat formal trials')
    cal,refs,accepted,quota=gate()
    protocol=dict(source_hashes=sources(),safety_source_sha256=sha(ROOT/'results/CALIBRATION.json'),
        quota_bytes=quota,capacity_rule='one fixed absolute memory.max: max reference initial + 620MiB, round up 64MiB',
        prediction='rolling measured reference, refreshed every 0.5s with actual current headroom; not a learned model',
        numeric_policy='real WSS bytes and pagefault counts only; scenario label cannot select a path',
        safety=accepted,compression='zswap Y; actual compressed pages must be observed',
        runtime_protection='per-app reference-adjusted refault/PSI plus foreground response; immutable budgets',
        cold='scripted old images/rows/canvases, allowable quantity bounded by measured safe levels',
        order=[(scene,flag) for scene in ('stable','turnover','revisit') for flag in (False,True)],
        caveat='one off/on pair per scenario, four-app test domain, no statistical optimum claim')
    doc=dict(status='RUNNING',protocol=protocol,trials=[],started_wall_s=time.time())
    save(target,doc)
    try:
        for number,(scene,enabled) in enumerate(protocol['order'],1):
            if sources()!=protocol['source_hashes']:raise RuntimeError('frozen code changed')
            baseline=next((row for row in doc['trials'] if row['scenario']==scene and not row['enabled']),None)
            bundle=dict(reference_path=cal['references'][scene],
                        guard_baseline_path=str(ROOT/'results/trials'/baseline['tag']/'result.json') if baseline else None)
            print('FORMAL_START %d/6 %s %s'%(number,scene,'on' if enabled else 'off'),flush=True)
            row=run(scene,enabled,stage='formal',reference=bundle,quota_bytes=quota)
            doc['trials'].append(row);save(target,doc)
            if row['status']!='COMPLETE' or not row['cleanup']['complete']:
                raise RuntimeError('failed formal trial retained; no further pressure')
            print('FORMAL_FINISHED %d/6'%number,flush=True)
        doc['status']='COMPLETE'
    except BaseException as exc:
        doc.update(status='STOPPED',error=repr(exc));raise
    finally:
        doc.update(ended_wall_s=time.time(),sources_unchanged=sources()==protocol['source_hashes'])
        save(target,doc)


if __name__=='__main__':main()
