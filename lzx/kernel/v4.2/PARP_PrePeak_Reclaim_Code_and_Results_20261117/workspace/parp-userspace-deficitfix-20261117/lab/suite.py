"""Six small native OUTPUT-driven diagnostics, not a model or production benchmark."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
import uuid

HERE=Path(__file__).resolve().parent
ROOT=HERE.parent
WORK=HERE.parents[1]
sys.path.insert(0,str(ROOT))
sys.path.insert(0,str(WORK/'oracle-realapps-validation-20261110'))
from prepare import display, identity
from gimp_native import Gimp
sys.path.insert(0,str(ROOT/'controller'))
from adapters import ColdReply
from plan import Candidate, Phase, MIB
from native import NativeCgroup
from native_apps import Calc, Browser
import telemetry
from harness_contracts import advance_service
import task_faults

GROUPS={'gimp':'background','libreoffice':'libreoffice','firefox':'firefox','krita':'foreground'}
PEAK=8.0
DURATION=27.0
BASELINE_SHA='c4fb4234bd99ae4e741ebebdbcfc784d398e2c49197eb2eae0c6e37f1545e2b5'
WORKLOAD_VERSION='measured-v2-cli-open:matched-total-task-counters-v1:serial-queue-accounting-v2'
AFTER_PEAK_TOUCHES=(14,18)
REVISIT_AT=22


def save(path,value):
    path.write_text(json.dumps(value,ensure_ascii=False,indent=1)+'\n')


def load(path):return json.loads(Path(path).read_text())


def kv(path):
    return {key:int(value) for key,value in (line.split() for line in path.read_text().splitlines())}


def rss(group):
    total=0
    for value in (group/'cgroup.procs').read_text().split():
        try:
            rows=dict(line.split(':',1) for line in Path('/proc',value,'status').read_text().splitlines() if ':' in line)
            total+=int(rows.get('VmRSS','0 kB').split()[0])*1024
        except FileNotFoundError:pass
    return total


def snapshot(base):
    return dict(monotonic_ns=time.monotonic_ns(),current=int((base/'memory.current').read_text()),
        groups={app:dict(current=int((base/leaf/'memory.current').read_text()),rss_sum_bytes=rss(base/leaf),
            swap=int((base/leaf/'memory.swap.current').read_text()),stats=kv(base/leaf/'memory.stat'))
            for app,leaf in GROUPS.items()}, events=kv(base/'memory.events'))


def until(target):
    while time.monotonic()<target:time.sleep(max(0,min(.03,target-time.monotonic())))


def wait(path,process,timeout):
    end=time.monotonic()+timeout
    while not path.exists():
        if process.poll() is not None:raise RuntimeError(str(path)+' helper exited')
        if time.monotonic()>end:raise TimeoutError(str(path))
        time.sleep(.05)


def host_gate():
    path=HERE/'host-latest.json'
    if not path.exists():raise RuntimeError('fresh host-memory check required')
    row=load(path)
    if abs(time.time()-row['wall_s'])>20 or row['available_bytes']<3*1024**3:
        raise RuntimeError('host memory below 3 GiB or check stale; no workload launched')


def settle_loaded_apps(base):
    """Observe ready applications until startup charges settle; no memory filler."""
    history=[];began=time.monotonic()
    while time.monotonic()-began<35:
        now=time.monotonic()
        row={app:int((base/leaf/'memory.current').read_text()) for app,leaf in GROUPS.items()}
        history.append((now,row));history=[r for r in history if now-r[0]<=2.2]
        if now-history[0][0]>=2 and all(max(r[1][app] for r in history)-min(r[1][app] for r in history)<=8*MIB for app in GROUPS):
            return dict(wait_s=now-began,stable_span_s=now-history[0][0],
                        per_app_range_bytes={app:max(r[1][app] for r in history)-min(r[1][app] for r in history) for app in GROUPS})
        time.sleep(.2)
    raise RuntimeError('application startup occupancy did not settle within 35s')


class Measured(NativeCgroup):
    def __init__(self,path,directory,app):
        super().__init__(path,background=True)
        self.directory=directory;self.app=app;self.writes=[]
    def write(self,name,value):
        before=self.usage();begin=time.monotonic_ns();error=None;error_errno=None
        previous_limit=self.read_high() if name=='memory.high' else None
        try:super().write(name,value)
        except OSError as exc:error=repr(exc);error_errno=exc.errno;raise
        finally:
            row=dict(app=self.app,name=name,value=value,start_ns=begin,end_ns=time.monotonic_ns(),
                     before_bytes=before,after_bytes=self.usage(),error=error,error_errno=error_errno)
            if name=='memory.high':row['previous_limit']=previous_limit
            self.writes.append(row)
            with (self.directory/(self.app+'-writes.jsonl')).open('a') as output:output.write(json.dumps(row)+'\n')


def cleanup(base,contexts):
    sent=[]
    for leaf in GROUPS.values():
        for value in (base/leaf/'cgroup.procs').read_text().split():
            pid=int(value)
            try:
                actual=Path('/proc',value,'cgroup').read_text().strip()
                expected='0::/'+str((base/leaf).relative_to('/sys/fs/cgroup'))
                if actual!=expected:raise RuntimeError('process moved outside owned group during cleanup')
                os.kill(pid,signal.SIGTERM);sent.append(pid)
            except (FileNotFoundError,ProcessLookupError):pass
    end=time.monotonic()+15
    while time.monotonic()<end:
        remaining={leaf:(base/leaf/'cgroup.procs').read_text().split() for leaf in GROUPS.values()}
        if not any(remaining.values()):break
        time.sleep(.1)
    remaining={leaf:(base/leaf/'cgroup.procs').read_text().split() for leaf in GROUPS.values()}
    if any(remaining.values()):return dict(complete=False,remaining=remaining)
    for leaf in GROUPS.values():
        end=time.monotonic()+15
        while True:
            try:(base/leaf).rmdir();break
            except OSError as exc:
                import errno
                if exc.errno!=errno.EBUSY or time.monotonic()>end:raise
                time.sleep(.1)
    base.rmdir()
    for context in contexts:
        for key in ('manager','server'):
            ref=context[key]
            try:
                if identity(ref['pid'])==ref:os.kill(ref['pid'],signal.SIGTERM)
            except (FileNotFoundError,ProcessLookupError):pass
    return dict(complete=True,remaining=remaining,terminated_owned_pids=sent)


def run(scenario,enabled,*,stage='reference',reference=None,calibration=None,quota_bytes=None,peak_mode='concurrent'):
    if calibration is not None:raise ValueError('manual Executor calibration removed; use the same-service calibration stage')
    if stage not in ('reference','baseline','calibration','formal'):raise ValueError('unknown stage')
    if type(quota_bytes)is not int or quota_bytes<=0:raise ValueError('the same explicit memory.max is required for every stage')
    if stage!='reference':
        purpose='formal' if stage=='formal' else 'calibration'
        if not reference or reference.get('aligned') is not True or reference.get('purpose')!=purpose:
            raise ValueError('all controls/calibration/formal trials require the common aligned service')
        if reference.get('collect_regions'):raise ValueError('region collector is outside this aligned protocol')
        if stage=='baseline' and enabled:raise ValueError('baseline must be disabled')
        if stage=='calibration' and not enabled:raise ValueError('calibration must execute the candidate policy')
    if peak_mode not in ('concurrent','ordered'):raise ValueError('unknown diagnostic peak mode')
    host_gate()
    telemetry.require_active()
    original=WORK/'parp-userspace-four-ops/operation_controller.py'
    if hashlib.sha256(original.read_bytes()).hexdigest()!=BASELINE_SHA:raise RuntimeError('original code changed')
    if Path('/sys/module/zswap/parameters/enabled').read_text().strip()!='Y' or not telemetry.active():
        raise RuntimeError('compression guard must be active before any reference/calibration/formal workload')
    master=Path('/proc/sys/vm/parp_operations_enabled')
    if master.exists() and master.read_text().strip()!='0':raise RuntimeError('custom kernel proactive policy active')
    prepared=None
    if stage!='reference':
        if quota_bytes is None:raise ValueError('explicit test-domain limit required')
        from shared_service import prepare_inputs
        prospective=dict(reference)
        prospective['collector_contract']=telemetry.expected_sampling()
        prospective['live_context']=dict(scenario=scenario,kernel=os.uname().release,
            memory_limit_bytes=quota_bytes,peak_mode=peak_mode,workload_version=WORKLOAD_VERSION+':'+peak_mode,
            zswap=Path('/sys/module/zswap/parameters/enabled').read_text().strip(),old_hot_s=[2,4],
            first_growth_s=PEAK,new_hot_s=list(AFTER_PEAK_TOUCHES),old_document_revisit_s=REVISIT_AT)
        prepared=prepare_inputs(prospective)
    run_id=uuid.uuid4().hex[:16]
    tag=stage+'-'+scenario+('-on' if enabled else '-off')+'-'+run_id[:8]
    root=Path.home()/('parp-measured-trial-'+run_id[:8]);root.mkdir(mode=0o700)
    shared=ROOT/'results/trials'/tag;shared.mkdir(parents=True)
    parent=Path('/sys/fs/cgroup')/subprocess.check_output(['systemctl','--user','show','-p','ControlGroup','--value'],text=True).strip().lstrip('/')
    base=parent/('parp-measured-'+uuid.uuid4().hex[:12]);base.mkdir()
    (base/'cgroup.subtree_control').write_text('+memory')
    for leaf in GROUPS.values():(base/leaf).mkdir()
    config=root/'cgroup.json';save(config,dict(base=str(base),parent=str(parent),leaves=list(GROUPS.values())))
    contexts=[];handles=[];gimp=calc=browser=front=workflow=None
    collector_started=False
    sample_stop=threading.Event();sample_thread=None
    result=dict(tag=tag,run_id=run_id,stage=stage,scenario=scenario,enabled=enabled,root=str(root),base=str(base),status='PREPARING',
        workload_version=WORKLOAD_VERSION+':'+peak_mode,peak_mode=peak_mode,
        fault_counter_source='task_stat_self_and_reaped_descendants',
        trained_predictors_called=False,original_controller_modified=False,
        active_control_policy='fixed-sequence-high-fine-reclaim-v1',
        safety_floor='native calibration evidence required for formal execution',
        pressure_scope='one isolated cgroup containing four native applications; not physical-machine exhaustion',
        operation_schedule=dict(old_hot_s=[2,4],peak_s=PEAK,new_hot_s=list(AFTER_PEAK_TOUCHES),old_revisit_s=REVISIT_AT,foreground_end_s=DURATION),
        zswap='Y',kernel=os.uname().release,operations=[],cold_preset={
            'gimp':'old-image layers 3..15 not touched before revisit',
            'libreoffice':'old rows 100..79999 not read before revisit',
            'firefox':'old canvas 1..7 not read before revisit',
            'meaning':'scripted cold objects; eligible bytes bounded by measured safety, not RSS percentages'})
    save(shared/'result.json',result)
    def log(name):
        handle=(root/name).open('wb');handles.append(handle);return handle
    try:
        # Apply the same L even during loading/warming; never calibrate at max
        # and later run the formal task in a restricted test domain.
        (base/'memory.max').write_text(str(quota_bytes))
        if int((base/'memory.max').read_text())!=quota_bytes:raise ValueError('memory.max write did not take effect')
        result['limit_set_before_application_load']=True
        print('PREPARING '+tag,flush=True)
        contexts.append(display(root,'front'));contexts.append(display(root,'background'))
        env=os.environ.copy();env.update(contexts[1]['env'],KRITA_LAB_CGROUP_BASE=str(base),
            KRITA_LAB_CGROUP_CONFIG=str(config),ORACLE_APP_ASSETS=str(Path.home()/'krita-userspace-realapps-20261105/assets'),LANG='C.UTF-8')
        frontenv=env.copy();frontenv.update(contexts[0]['env'])
        frontdir=root/'foreground'
        front=subprocess.Popen([str(Path.home()/'krita-acceptance-venv/bin/python'),'-B',str(HERE/'foreground_probe.py'),
            '--run',str(frontdir),'--asset',str(Path(env['ORACLE_APP_ASSETS'])/'functional_4096_4layers.ora'),
            '--template',str(Path.home()/'krita-userspace-peak-20261103/profile-template'),'--duration',str(DURATION)],
            env=frontenv,stdout=log('foreground.log'),stderr=subprocess.STDOUT)
        gimp=Gimp(root,env);gimp.prepare(16)
        print('GIMP_LOADED '+tag,flush=True)
        calc=Calc(root,config,env)
        print('CALC_LOADED '+tag,flush=True)
        browser=Browser(root,config,env,gimp.assets/'layer-00.png')
        print('FIREFOX_LOADED '+tag,flush=True)
        wait(frontdir/'ready.json',front,240)
        apps={'gimp':gimp,'libreoffice':calc,'firefox':browser}
        def operation(app,kind,func,scheduled_s=None):
            group=base/GROUPS[app]
            before=kv(group/'memory.stat')
            before_current=int((group/'memory.current').read_text());before_swap=int((group/'memory.swap.current').read_text())
            faults_before=task_faults.snapshot(group)
            begin=time.monotonic_ns()
            predecessor_end=max((o['end_ns']/1e9-start for o in result['operations'] if o['app']==app),default=0) if scheduled_s is not None else None
            ready_s=max(scheduled_s,predecessor_end) if scheduled_s is not None else None
            try:
                func();error=None
            except Exception as exc:error=repr(exc);raise
            finally:
                end=time.monotonic_ns()
                faults_after=task_faults.snapshot(group)
                measured_faults=task_faults.difference(faults_before,faults_after)
                after=kv(base/GROUPS[app]/'memory.stat')
                result['operations'].append(dict(app=app,kind=kind,start_ns=begin,end_ns=end,
                    elapsed_ms=(end-begin)/1e6,
                    scheduled_s=scheduled_s,start_lateness_s=(begin/1e9-start-scheduled_s) if scheduled_s is not None else None,
                    queue_delay_s=max(0,predecessor_end-scheduled_s) if scheduled_s is not None else None,
                    dispatch_lateness_s=(begin/1e9-start-ready_s) if ready_s is not None else None,
                    before_current_bytes=before_current,before_swap_bytes=before_swap,
                    after_current_bytes=int((group/'memory.current').read_text()),after_swap_bytes=int((group/'memory.swap.current').read_text()),
                    pgfault=measured_faults['pgfault'],pgmajfault=measured_faults['pgmajfault'],
                    fault_counter_source='task_stat_self_and_reaped_descendants',
                    faults_before=faults_before,faults_after=faults_after,
                    memory_stat_pgfault=after.get('pgfault',0)-before.get('pgfault',0),
                    memory_stat_pgmajfault=after.get('pgmajfault',0)-before.get('pgmajfault',0),error=error))
        for app,obj in apps.items():
            operation(app,'warm_hot',lambda obj=obj,app=app:gimp.touch('old-image',0,3) if app=='gimp' else obj.touch())
        result['startup_settle']=settle_loaded_apps(base)
        collector_ready=telemetry.begin(run_id,base,GROUPS,wss=stage=='reference');collector_started=True
        result['collector_contract']=collector_ready['sampling_contract']
        initial=snapshot(base);result['initial']=initial
        if quota_bytes is not None:(base/'memory.max').write_text(str(quota_bytes))
        result['quota_bytes']=quota_bytes
        # L: available memory for this test domain. Not the amount to reclaim A.
        result['memory_limit_bytes']=quota_bytes
        if stage!='reference':
            from demand_reference import admission
            result['deficit_admission']=admission(prepared['reference'],initial,quota_bytes,
                                                 reference.get('minimum_initial_gap_bytes',0))
            print('DEFICIT_ADMITTED '+tag+' '+json.dumps(result['deficit_admission']),flush=True)
            from shared_service import Service
            bundle=dict(reference)
            bundle['_prepared']=prepared
            bundle['collector_contract']=collector_ready['sampling_contract']
            schedule=result['operation_schedule']
            actual_limit=(base/'memory.max').read_text().strip()
            if actual_limit=='max':raise ValueError('an explicit calibration/formal domain limit is required')
            bundle['live_context']=dict(scenario=scenario,kernel=os.uname().release,
                memory_limit_bytes=int(actual_limit),peak_mode=peak_mode,workload_version=result['workload_version'],
                zswap=Path('/sys/module/zswap/parameters/enabled').read_text().strip(),
                old_hot_s=schedule['old_hot_s'],first_growth_s=schedule['peak_s'],
                new_hot_s=schedule['new_hot_s'],old_document_revisit_s=schedule['old_revisit_s'])
            workflow=Service(bundle,initial,base,shared,tag,quota_bytes,enabled)
        result['status']='RUNNING';save(shared/'result.json',result)
        result['total_faults_initial']={app:task_faults.snapshot(base/leaf)for app,leaf in GROUPS.items()}
        (frontdir/'go').touch();wait(frontdir/'phase-start.json',front,10)
        start=load(frontdir/'phase-start.json')['monotonic_ns']/1e9
        result['start_monotonic_s']=start
        if workflow is not None and hasattr(workflow,'set_start'):
            workflow.set_start(run_id,start)
        def sample():
            with (shared/'occupancy.jsonl').open('a') as output:
                while not sample_stop.is_set():
                    row=dict(time_s=time.monotonic()-start,total=int((base/'memory.current').read_text()),
                             apps={app:int((base/leaf/'memory.current').read_text()) for app,leaf in GROUPS.items()},
                             memory_events=kv(base/'memory.events'),
                             app_memory_events={app:kv(base/leaf/'memory.events')for app,leaf in GROUPS.items()})
                    output.write(json.dumps(row)+'\n');output.flush();sample_stop.wait(.1)
        sample_thread=threading.Thread(target=sample,daemon=True);sample_thread.start()
        grow_gate=threading.Event()
        peak_order=('libreoffice','firefox','gimp')
        turns={app:threading.Event() for app in peak_order};turns[peak_order[0]].set()
        def worker(app,obj):
            for at in (2,4):
                until(start+at)
                operation(app,'old_hot',lambda:gimp.touch('old-image',0,3) if app=='gimp' else obj.touch(),at)
            if not grow_gate.wait(30):raise TimeoutError('peak gate')
            if peak_mode=='ordered' and not turns[app].wait(60):raise TimeoutError('diagnostic peak order')
            def grow():
                if app=='gimp':gimp.controlled_stage('new_pages',1)
                else:obj.grow()
            try:operation(app,'peak_new_data',grow,PEAK if peak_mode=='concurrent' or app==peak_order[0] else None)
            finally:
                index=peak_order.index(app)
                if peak_mode=='ordered' and index+1<len(peak_order):turns[peak_order[index+1]].set()
            for at in AFTER_PEAK_TOUCHES:
                until(start+at)
                def touch():
                    if app=='gimp':gimp.touch('old-image' if scenario=='stable' else 'future-image',0,3 if scenario=='stable' else 8)
                    elif scenario=='stable':obj.touch()
                    else:obj.touch_future()
                    if scenario=='combined':
                        if app=='gimp':gimp.touch('old-image',0,3)
                        else:obj.touch()
                operation(app,'old_hot_after_peak' if scenario=='stable' else 'new_hot_after_peak',touch,at)
            if scenario in ('revisit','combined'):
                until(start+REVISIT_AT)
                operation(app,'old_document_revisit',lambda:gimp.touch('old-image',0,16) if app=='gimp' else obj.revisit(),REVISIT_AT)
        with ThreadPoolExecutor(max_workers=3) as pool:
            tasks=[pool.submit(worker,app,obj) for app,obj in apps.items()]
            deadline_recorded=False
            while time.monotonic()<start+DURATION or not all(task.done() for task in tasks):
                now=time.monotonic()-start
                if now>=PEAK and not deadline_recorded:
                    result['before_peak']=snapshot(base)
                    result['before_peak_s']=now
                    deadline_recorded=True;grow_gate.set()
                    print('PEAK_STARTED '+tag,flush=True)
                    if stage!='reference':
                        growth=result['deficit_admission']['demand_growth_bytes']
                        gap=max(0,growth-(quota_bytes-result['before_peak']['current']))
                        print('PEAK_SPACE_CHECK '+tag+' '+json.dumps(dict(unfilled_bytes=gap,
                              enabled=enabled,prepeak_net_drop_bytes=initial['current']-result['before_peak']['current'])),flush=True)
                if workflow is not None:
                    if hasattr(workflow,'set_activity'):workflow.set_activity(result['operations'],start)
                    # Observation may outlive a plan. End service at its original
                    # expiry; keep measuring application work without extending it.
                    advance_service(workflow,now,frontdir)
                if front.poll() is not None and not (frontdir/'measured.json').exists():
                    raise RuntimeError(('foreground failed: '+(frontdir/'result.json').read_text())
                                       if (frontdir/'result.json').exists() else 'foreground helper failed')
                if now>130:raise TimeoutError('native stage exceeded fixed diagnostic limit')
                time.sleep(.05)
            for task in tasks:task.result()
        wait(frontdir/'measured.json',front,15)
        result['final']=snapshot(base)
        result['total_faults_final']={app:task_faults.snapshot(base/leaf)for app,leaf in GROUPS.items()}
        result['total_faults']={app:task_faults.difference(result['total_faults_initial'][app],result['total_faults_final'][app])for app in GROUPS}
        result['total_fault_counter_source']='task_stat_self_and_reaped_descendants'
        result['total_fault_interval']='before_go_to_after_all_work_completed'
        sample_stop.set()
        if sample_thread is not None:sample_thread.join(timeout=3)
        if collector_started:
            result['root_telemetry']=telemetry.end(run_id,shared);collector_started=False
        if workflow is not None:
            workflow.close(time.monotonic()-start)
            result.update(workflow.report())
        else:
            samples=[json.loads(line) for line in (shared/'occupancy.jsonl').read_text().splitlines()]
            total_peak=max(samples,key=lambda row:row['total'])
            result['growth_bytes']={app:max(0,total_peak['apps'][app]-initial['groups'][app]['current']) for app in GROUPS}
            result['growth_bytes']['cgroup_overhead']=max(0,total_peak['total']-initial['current']-sum(result['growth_bytes'].values()))
            result['profile_peak_time_s']=total_peak['time_s']
            result['profile_peak_total_bytes']=total_peak['total']
        result['foreground_measured']=load(frontdir/'measured.json')
        result['responses']=[json.loads(line) for line in (frontdir/'responses.jsonl').read_text().splitlines()]
        (frontdir/'finish').touch();front.wait(timeout=45)
        result['foreground_exit']=load(frontdir/'result.json')
        gimp.close();gimp=None
        calc.close();calc=None
        browser.close();browser=None
        result['status']='COMPLETE'
        print('COMPLETE '+tag,flush=True)
    except BaseException as exc:
        import traceback
        result['error_traceback']=traceback.format_exc()
        if workflow is not None:
            try:result.update(workflow.report())
            except Exception as report_error:result['failure_report_error']=repr(report_error)
        result.update(status='FAILED',error=repr(exc))
        print('FAILED '+tag+' '+repr(exc),flush=True)
    finally:
        sample_stop.set()
        if sample_thread is not None:sample_thread.join(timeout=3)
        if workflow is not None:
            try:workflow.close(DURATION+4)
            except Exception as exc:result['close_error']=repr(exc)
        if collector_started:
            try:result['root_telemetry']=telemetry.end(run_id,shared)
            except Exception as exc:result['telemetry_error']=repr(exc)
        if front is not None and front.poll() is None:
            front.terminate()
            try:front.wait(timeout=5)
            except subprocess.TimeoutExpired:result['foreground_helper_left_running']=front.pid
        # Save complete measurements BEFORE cleanup, and retain them on EBUSY.
        save(root/'result.json',result);save(shared/'result.json',result)
        try:result['cleanup']=cleanup(base,contexts)
        except Exception as exc:result['cleanup']=dict(complete=False,error=repr(exc))
        for handle in handles:handle.close()
        save(root/'result.json',result);save(shared/'result.json',result)
        save(HERE/'latest-trial.json',dict(tag=tag,status=result['status'],path=str(shared/'result.json')))
    return result


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--scenario',choices=('stable','turnover','revisit','combined'),required=True)
    parser.add_argument('--stage',choices=('reference','baseline','calibration','formal'),required=True)
    parser.add_argument('--limit-mib',type=int,default=3776)
    parser.add_argument('--scope',choices=('per_app','all_apps'),required=True)
    args=parser.parse_args()
    from inputs_bundle import make_inputs
    bundle=make_inputs(args.scenario,'formal' if args.stage=='formal' else 'calibration',args.scope) if args.stage!='reference' else None
    result=run(args.scenario,args.stage in ('calibration','formal'),stage=args.stage,reference=bundle,
               quota_bytes=args.limit_mib*MIB)
    if result['status']!='COMPLETE' or not result['cleanup']['complete']:raise SystemExit(1)


if __name__=='__main__':main()
