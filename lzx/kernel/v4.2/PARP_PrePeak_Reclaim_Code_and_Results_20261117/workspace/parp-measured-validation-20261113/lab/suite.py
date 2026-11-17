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
sys.path.insert(0,str(WORK/'oracle-realapps-validation-20261110'))
from prepare import display, identity
from gimp_native import Gimp
sys.path.insert(0,str(ROOT/'controller'))
from adapters import ColdReply
from plan import Candidate, Phase, MIB
from native import NativeCgroup
from executor import Executor,Guard
from plan import AppPlan,Action
from native_apps import Calc, Browser
import telemetry

GROUPS={'gimp':'background','libreoffice':'libreoffice','firefox':'firefox','krita':'foreground'}
PEAK=8.0
DURATION=27.0
BASELINE_SHA='c4fb4234bd99ae4e741ebebdbcfc784d398e2c49197eb2eae0c6e37f1545e2b5'
WORKLOAD_VERSION='measured-v2-cli-open'


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


class Measured(NativeCgroup):
    def __init__(self,path,directory,app):
        super().__init__(path,background=True)
        self.directory=directory;self.app=app;self.writes=[]
    def write(self,name,value):
        before=self.usage();begin=time.monotonic_ns();error=None
        try:super().write(name,value)
        except OSError as exc:error=repr(exc);raise
        finally:
            row=dict(app=self.app,name=name,value=value,start_ns=begin,end_ns=time.monotonic_ns(),
                     before_bytes=before,after_bytes=self.usage(),error=error)
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


def run(scenario,enabled,*,stage='reference',reference=None,calibration=None,quota_bytes=None):
    host_gate()
    original=WORK/'parp-userspace-four-ops/operation_controller.py'
    if hashlib.sha256(original.read_bytes()).hexdigest()!=BASELINE_SHA:raise RuntimeError('original code changed')
    if Path('/sys/module/zswap/parameters/enabled').read_text().strip()!='Y' or not telemetry.active():
        raise RuntimeError('compression guard must be active before any reference/calibration/formal workload')
    master=Path('/proc/sys/vm/parp_operations_enabled')
    if master.exists() and master.read_text().strip()!='0':raise RuntimeError('custom kernel proactive policy active')
    run_id=uuid.uuid4().hex[:16]
    tag=stage+'-'+scenario+('-on' if enabled else '-off')+'-'+run_id[:8]
    root=Path.home()/('parp-measured-trial-'+run_id[:8]);root.mkdir(mode=0o700)
    shared=ROOT/'results/trials'/tag;shared.mkdir(parents=True)
    parent=Path('/sys/fs/cgroup')/subprocess.check_output(['systemctl','--user','show','-p','ControlGroup','--value'],text=True).strip().lstrip('/')
    base=parent/('parp-measured-'+uuid.uuid4().hex[:12]);base.mkdir()
    (base/'cgroup.subtree_control').write_text('+memory')
    for leaf in GROUPS.values():(base/leaf).mkdir()
    config=root/'cgroup.json';save(config,dict(base=str(base),parent=str(parent),leaves=list(GROUPS.values())))
    contexts=[];handles=[];gimp=calc=browser=front=workflow=cal_executor=None
    collector_started=False
    sample_stop=threading.Event();sample_thread=None
    result=dict(tag=tag,run_id=run_id,stage=stage,scenario=scenario,enabled=enabled,root=str(root),base=str(base),status='PREPARING',
        workload_version=WORKLOAD_VERSION,
        trained_predictors_called=False,original_controller_modified=False,
        safety_floor='native calibration evidence required for formal execution',
        pressure_scope='one isolated cgroup containing four native applications; not physical-machine exhaustion',
        zswap='Y',kernel=os.uname().release,operations=[],cold_preset={
            'gimp':'old-image layers 3..15 not touched before revisit',
            'libreoffice':'old rows 100..79999 not read before revisit',
            'firefox':'old canvas 1..7 not read before revisit',
            'meaning':'scripted cold objects; eligible bytes bounded by measured safety, not RSS percentages'})
    save(shared/'result.json',result)
    def log(name):
        handle=(root/name).open('wb');handles.append(handle);return handle
    try:
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
        def operation(app,kind,func):
            before=kv(base/GROUPS[app]/'memory.stat');begin=time.monotonic_ns()
            try:
                func();error=None
            except Exception as exc:error=repr(exc);raise
            finally:
                end=time.monotonic_ns()
                after=kv(base/GROUPS[app]/'memory.stat')
                result['operations'].append(dict(app=app,kind=kind,start_ns=begin,end_ns=end,
                    elapsed_ms=(end-begin)/1e6,
                    pgfault=after.get('pgfault',0)-before.get('pgfault',0),
                    pgmajfault=after.get('pgmajfault',0)-before.get('pgmajfault',0),error=error))
        for app,obj in apps.items():
            operation(app,'warm_hot',lambda obj=obj,app=app:gimp.touch('old-image',0,3) if app=='gimp' else obj.touch())
        time.sleep(.6)
        telemetry.begin(run_id,base,GROUPS,wss=stage=='reference');collector_started=True
        initial=snapshot(base);result['initial']=initial
        if quota_bytes is not None:(base/'memory.max').write_text(str(quota_bytes))
        result['quota_bytes']=quota_bytes
        if stage=='formal':
            from online_service import Service
            workflow=Service(reference,initial,base,shared,tag,quota_bytes,enabled)
        if stage=='calibration':
            app=calibration['app'];amount=calibration['amount_bytes'];method=calibration['method']
            group=initial['groups'][app]
            if amount>=min(group['current'],group['rss_sum_bytes']):raise RuntimeError('calibration step exceeds resident footprint')
            backend=Measured(base/GROUPS[app],shared,app)
            floor_cg=group['current']-amount;floor_rss=group['rss_sum_bytes']-amount
            cal_executor=Executor(backend,Guard())
            cal_executor.update(AppPlan(app,tag,0,0,DURATION+3,amount,
                (Action(0,7.5,method,floor_rss,floor_cg,floor_cg if method=='high' else None,'CALIBRATION_INTERVENTION'),
                 Action(7.5,DURATION+3,'observe',0,0,None,'CALIBRATION_RELEASE_FOR_NEW_DEMAND')),goal_bytes=amount),0)
            result['calibration_intervention']=calibration
        result['status']='RUNNING';save(shared/'result.json',result)
        (frontdir/'go').touch();wait(frontdir/'phase-start.json',front,10)
        start=load(frontdir/'phase-start.json')['monotonic_ns']/1e9
        result['start_monotonic_s']=start
        def sample():
            with (shared/'occupancy.jsonl').open('a') as output:
                while not sample_stop.is_set():
                    row=dict(time_s=time.monotonic()-start,total=int((base/'memory.current').read_text()),
                             apps={app:int((base/leaf/'memory.current').read_text()) for app,leaf in GROUPS.items()})
                    output.write(json.dumps(row)+'\n');output.flush();sample_stop.wait(.1)
        sample_thread=threading.Thread(target=sample,daemon=True);sample_thread.start()
        grow_gate=threading.Event()
        def worker(app,obj):
            for at in (2,4):
                until(start+at)
                operation(app,'old_hot',lambda:gimp.touch('old-image',0,3) if app=='gimp' else obj.touch())
            if not grow_gate.wait(30):raise TimeoutError('peak gate')
            def grow():
                if app=='gimp':gimp.controlled_stage('new_pages',1)
                else:obj.grow()
            operation(app,'peak_new_data',grow)
            for at in (14,18):
                until(start+at)
                def touch():
                    if app=='gimp':gimp.touch('old-image' if scenario=='stable' else 'future-image',0,3 if scenario=='stable' else 8)
                    elif scenario=='stable':obj.touch()
                    else:obj.touch_future()
                    if scenario=='combined':
                        if app=='gimp':gimp.touch('old-image',0,3)
                        else:obj.touch()
                operation(app,'old_hot_after_peak' if scenario=='stable' else 'new_hot_after_peak',touch)
            if scenario in ('revisit','combined'):
                until(start+22)
                operation(app,'old_document_revisit',lambda:gimp.touch('old-image',0,16) if app=='gimp' else obj.revisit())
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
                if workflow is not None:workflow.tick(now,frontdir)
                if cal_executor is not None:
                    app=calibration['app'];observed=max(0,initial['groups'][app]['current']-int((base/GROUPS[app]/'memory.current').read_text()))
                    cal_executor.tick(now,max(0,calibration['amount_bytes']-observed))
                if front.poll() is not None and not (frontdir/'measured.json').exists():
                    raise RuntimeError(('foreground failed: '+(frontdir/'result.json').read_text())
                                       if (frontdir/'result.json').exists() else 'foreground helper failed')
                if now>130:raise TimeoutError('native stage exceeded fixed diagnostic limit')
                time.sleep(.05)
            for task in tasks:task.result()
        wait(frontdir/'measured.json',front,15)
        result['final']=snapshot(base)
        sample_stop.set()
        if sample_thread is not None:sample_thread.join(timeout=3)
        if collector_started:
            result['root_telemetry']=telemetry.end(run_id,shared);collector_started=False
        if cal_executor is not None:
            cal_executor.close(time.monotonic()-start)
            result['calibration_events']=cal_executor.events
            result['calibration_writes']=backend.writes
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
        result.update(status='FAILED',error=repr(exc))
        print('FAILED '+tag+' '+repr(exc),flush=True)
    finally:
        sample_stop.set()
        if sample_thread is not None:sample_thread.join(timeout=3)
        if workflow is not None:
            try:workflow.close(DURATION+4)
            except Exception as exc:result['close_error']=repr(exc)
        if cal_executor is not None:
            try:cal_executor.close(DURATION+4)
            except Exception as exc:result['calibration_close_error']=repr(exc)
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
    parser.add_argument('--stage',choices=('reference','baseline','calibration'),required=True)
    parser.add_argument('--app',choices=('gimp','libreoffice','firefox'))
    parser.add_argument('--method',choices=('high','reclaim'))
    parser.add_argument('--amount-mib',type=int)
    args=parser.parse_args()
    calibration=None
    if args.stage=='calibration':
        if args.app is None or args.method is None or args.amount_mib is None:raise SystemExit('calibration app/method/amount required')
        calibration=dict(app=args.app,method=args.method,amount_bytes=args.amount_mib*MIB)
    result=run(args.scenario,False,stage=args.stage,calibration=calibration)
    if result['status']!='COMPLETE' or not result['cleanup']['complete']:raise SystemExit(1)


if __name__=='__main__':main()
