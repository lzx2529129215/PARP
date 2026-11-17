"""One fixed scene/limit/scope; fresh controls and calibration, then formal."""
import argparse
import json
import os
from pathlib import Path
import shutil
import signal
import sys
import time

ROOT=Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'lab'));sys.path.insert(0,str(ROOT/'controller'))
from execution_contract import sha


def verify_sources():
    manifest=json.loads((ROOT/'SOURCE_BASELINE.json').read_text(encoding='utf-8'))
    changed_policy={'adapters.py','coordinator.py','executor.py','plan.py'}
    for name,value in manifest['controller_files'].items():
        if sha(ROOT.parent/manifest['base']/'controller'/name)!=value:
            raise RuntimeError('preserved 13-Nov source changed: '+name)
        if name not in changed_policy and sha(ROOT/'controller'/name)!=value:
            raise RuntimeError('unrelated controller source changed: '+name)
    for name,value in manifest['native_workload_sources'].items():
        if sha(ROOT.parent/manifest['base']/'lab'/name)!=value:
            raise RuntimeError('preserved 13-Nov application source changed: '+name)
        expected=manifest.get('measurement_patches',{}).get(name,{}).get('patched_sha256',value)
        if sha(ROOT/'lab'/name)!=expected:raise RuntimeError('application/probe source changed: '+name)
    return manifest


def sources():
    return {p.relative_to(ROOT).as_posix():sha(p)for d in (ROOT,ROOT/'lab',ROOT/'controller')for p in d.glob('*.py')}


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--scenario',choices=('stable','turnover','revisit'),default='revisit')
    parser.add_argument('--scope',choices=('per_app','all_apps'),required=True)
    parser.add_argument('--limit-mib',type=int,default=None)
    parser.add_argument('--reference-limit-mib',type=int,default=6144)
    parser.add_argument('--target-gap-mib',type=int,default=192)
    parser.add_argument('--minimum-gap-mib',type=int,default=64)
    parser.add_argument('--check',action='store_true',help='source check only; no native workload')
    args=parser.parse_args()
    verify_sources()
    if args.check:
        print('SOURCE_CHECK_PASS: fixed-high/fine-reclaim policy; historical source, workloads and unrelated controller files preserved; no native run.');return
    if sys.platform!='linux' or os.getuid()!=1000:raise RuntimeError('ordinary Ubuntu desktop user required')
    if args.limit_mib is not None and args.limit_mib<=0:raise ValueError('positive test-domain limit required')
    if min(args.reference_limit_mib,args.target_gap_mib,args.minimum_gap_mib)<=0:
        raise ValueError('positive reference limit, target and minimum deficit required')
    from suite import run,save,load,host_gate,MIB
    from inputs_bundle import make_inputs
    from total_profiles import publish
    from total_faults import compare_totals
    import telemetry
    from harness_contracts import schedule_diagnostics
    for name in ('gimp','krita','libreoffice','Xephyr','openbox','xdotool','wmctrl'):
        if shutil.which(name)is None:raise FileNotFoundError(name)
    target=ROOT/'results'/('RUN-'+args.scenario+'.json')
    reference_index=ROOT/'results'/('REFERENCE-'+args.scenario+'.json')
    qualification_path=ROOT/'results'/('TOTAL_QUALIFICATION-'+args.scenario+'.json')
    if any(p.exists()for p in (target,reference_index,qualification_path)):
        raise RuntimeError('preserve previous cohort; no automatic repeat or favorable replacement')
    telemetry.require_active();host_gate()
    frozen=sources();limit=None if args.limit_mib is None else args.limit_mib*MIB
    doc=dict(status='RUNNING',scene=args.scenario,scope=args.scope,memory_limit_bytes=limit,
        source_sha256=frozen,started_wall_s=time.time(),trials=[],
        order=['baseline','calibration','calibration','baseline','baseline','calibration'],
        formal_order=['formal']*3,
        criterion='mean full-workload total faults +10%; operations diagnostic only',
        candidate='preserved 13-Nov multi-app proposal; no later 16-MiB table',
        reference_preparation='separate clear_refs WSS run; excluded from on/off fault scoring')
    doc.update(reference_limit_bytes=args.reference_limit_mib*MIB,target_gap_bytes=args.target_gap_mib*MIB,
               minimum_initial_gap_bytes=args.minimum_gap_mib*MIB,
               reference_preparation='unclipped demand reference; high/max/OOM counters must remain zero; excluded from scoring')
    save(target,doc)
    def stop(*_):raise KeyboardInterrupt('requested stop')
    signal.signal(signal.SIGTERM,stop)
    def check(row):
        if row['status']!='COMPLETE' or not row.get('cleanup',{}).get('complete')or row.get('service_errors'):
            raise RuntimeError('failed native trial retained; no further pressure')
        late,queued=schedule_diagnostics(row['operations'])
        row['schedule_late_operations']=late
        row['schedule_queue_delays']=queued
        save(ROOT/'results/trials'/row['tag']/'result.json',row)
        if late:raise RuntimeError('workload schedule missed; result retained')
        return row
    try:
        print('TOTAL_REFERENCE_START',args.scenario,flush=True)
        ref=run(args.scenario,False,stage='reference',quota_bytes=args.reference_limit_mib*MIB)
        doc['reference']=ref;save(target,doc);check(ref)
        from reference import measured_reference
        from demand_reference import require_unclipped,choose_limit,growth_before_revisit
        require_unclipped(ref)
        demand=measured_reference(ROOT/'results/trials'/ref['tag']/'result.json')
        if limit is None:limit=choose_limit(demand,args.target_gap_mib*MIB)
        if limit>=args.reference_limit_mib*MIB:raise ValueError('test limit must be below unconstrained demand reference limit')
        doc.update(memory_limit_bytes=limit,reference_demand_growth_bytes=growth_before_revisit(demand))
        save(target,doc)
        print('DEFICIT_SETUP '+str(dict(test_limit_MiB=limit/MIB,demand_growth_MiB=growth_before_revisit(demand)/MIB,
                                      target_gap_MiB=args.target_gap_mib,minimum_gap_MiB=args.minimum_gap_mib)),flush=True)
        save(reference_index,dict(path='results/trials/'+ref['tag']+'/result.json',sha256=sha(ROOT/'results/trials'/ref['tag']/'result.json'),
                                 reference_mode='unclipped_demand',minimum_initial_gap_bytes=args.minimum_gap_mib*MIB))
        def execute(stage):
            verify_sources()
            if sources()!=frozen:raise RuntimeError('frozen experimental source changed')
            host_gate()
            print('TOTAL_START',len(doc['trials'])+1,stage,flush=True)
            row=run(args.scenario,stage!='baseline',stage=stage,
                reference=make_inputs(args.scenario,'formal'if stage=='formal'else 'calibration',args.scope),quota_bytes=limit)
            doc['trials'].append(row);save(target,doc);check(row)
            save(target,doc)
            print('TOTAL_END',len(doc['trials']),stage,flush=True)
        for stage in doc['order']:execute(stage)
        off=[r for r in doc['trials']if r['stage']=='baseline'];on=[r for r in doc['trials']if r['stage']=='calibration']
        contract=off[0]['execution_contract']
        qual=publish(qualification_path,[ROOT/'results/trials'/r['tag']/'result.json'for r in off],
            [ROOT/'results/trials'/r['tag']/'result.json'for r in on],contract,args.scope,root=ROOT)
        doc['calibration']=qual['assessment'];save(target,doc)
        if qual['assessment']['eligible']:
            for stage in doc['formal_order']:execute(stage)
            formal=[r for r in doc['trials']if r['stage']=='formal']
            doc['formal_total_faults']=compare_totals(off,formal,args.scope)
            if doc['formal_total_faults']['within_trial_limit']is not True:
                retained=qualification_path.with_name('CALIBRATION_ONLY-'+args.scenario+'.json')
                qualification_path.rename(retained)
                doc['formal_qualification_published']=False
        else:doc['formal_skipped']='same-service whole-plan total-fault calibration did not pass'
        doc['status']='COMPLETE'
    except BaseException as exc:
        doc.update(status='STOPPED',error=repr(exc));raise
    finally:
        (telemetry.BRIDGE/'STOP').touch()
        # A calibration-only certificate is never left at the default formal
        # entry after an interrupted or failed formal verification.
        verified=doc.get('formal_total_faults',{}).get('within_trial_limit')is True and doc.get('status')=='COMPLETE'
        if qualification_path.exists()and not verified:
            label='CALIBRATION_ONLY'if doc.get('calibration',{}).get('eligible')else 'UNQUALIFIED'
            retained=qualification_path.with_name(label+'-'+args.scenario+'.json')
            if retained.exists():retained=retained.with_name(retained.stem+'-'+str(time.time_ns())+retained.suffix)
            qualification_path.rename(retained)
            doc['retained_qualification']=retained.name
        doc['formal_qualification_published']=verified
        doc.update(ended_wall_s=time.time(),sources_unchanged=sources()==frozen);save(target,doc)
        for _ in range(50):
            if not telemetry.active():break
            time.sleep(.1)
        save(ROOT/'results'/('RESTORE-'+args.scenario+'.json'),dict(guard=load(telemetry.BRIDGE/'guard_status.json'),
            zswap=Path('/sys/module/zswap/parameters/enabled').read_text().strip()))


if __name__=='__main__':main()
