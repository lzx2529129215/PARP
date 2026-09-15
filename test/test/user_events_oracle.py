#!/usr/bin/env python3
"""Continuous workbook replay and paired, isolated next-use/bin experiment."""
import argparse
from collections import Counter
import contextlib
import csv
import ctypes
import fcntl
import hashlib
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import threading
import time
import traceback
from types import SimpleNamespace

import user_events_replay as U
from oracle_next_use import compile_plan, NextUse, OracleBridge, ROOT
from build_user_events_switch_sequence import write_book
from wps_runtime import prepare_runtime
from core.runtime_scope import load_runtime_app_scope

SERVICE='parp-runtime-monitor.service'
MIB=1024*1024
DEBUG=Path('/sys/kernel/debug/parp')
CONTROL_VALUES={str(DEBUG/'mode'):0,str(DEBUG/'effective_tier_mode'):0,
    **{f'/proc/sys/vm/{k}':0 for k in ['tier2_predict_enabled','tier2_wss_predict_enabled',
        'tier2_wss_strengthen_enabled','parp_reclaim_bin_enabled',
        'parp_reclaim_cold_aggressive_enabled','parp_reclaim_workload_enabled']}}


def write_json(path,value):
    path=Path(path);tmp=path.with_suffix(path.suffix+'.tmp')
    tmp.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n');tmp.replace(path)


def log(path,value):
    with Path(path).open('a') as f:f.write(json.dumps(value,ensure_ascii=False)+'\n')


def systemctl(*args,check=True):
    return subprocess.run(['systemctl','--user',*args],capture_output=True,text=True,check=check,timeout=40)


def read_priv(path):
    return subprocess.run(['sudo','-n','cat',str(path)],capture_output=True,text=True,check=True,timeout=5).stdout.strip()


def write_priv(path,value):
    subprocess.run(['sudo','-n',sys.executable,'-c',
        'import os,sys; fd=os.open(sys.argv[1],os.O_WRONLY|os.O_NONBLOCK); os.write(fd,sys.argv[2].encode()); os.close(fd)',str(path),str(value)],
        check=True,capture_output=True,timeout=10)


def kv(path):
    return {k:int(v) for k,v in (line.split() for line in Path(path).read_text().splitlines())}


def kernel_stats():
    result={}
    for line in read_priv(DEBUG/'reclaim_bin_stats').splitlines():
        parts=line.split()
        if len(parts)==2:
            try:result[parts[0]]=int(parts[1])
            except ValueError:pass
    return result


def stop_owned_process(record):
    pid=record['pid']
    try:fd=os.pidfd_open(pid)
    except ProcessLookupError:return
    try:
        identity=U.P.desktop_api.process_identity(pid)
        if identity and identity[1]==record['starttime_ticks']:
            signal.pidfd_send_signal(fd,signal.SIGTERM)
            if not select.select([fd],[],[],2)[0]:signal.pidfd_send_signal(fd,signal.SIGKILL)
    finally:os.close(fd)


def restore(state_path):
    """Idempotent restoration shared by normal exit and an independent guardian."""
    with Path(str(state_path)+'.recovery-lock').open('w') as guard:
        fcntl.flock(guard,fcntl.LOCK_EX)
        state=json.loads(Path(state_path).read_text())
        if state.get('restored'):return
        errors=[]
        def attempt(fn):
            try:fn()
            except Exception as exc:errors.append(repr(exc))
        attempt(lambda:write_priv('/proc/sys/vm/parp_reclaim_bin_enabled',0))
        for root in state.get('roots',[]):
            if Path(root).exists():attempt(lambda p=root:write_priv(Path(p)/'memory.max','max'))
        for unit in reversed(state.get('units',[])):
            attempt(lambda u=unit:systemctl('stop',u,check=False))
        for record in reversed(state.get('processes',[])):
            attempt(lambda r=record:stop_owned_process(r))
        # Expire this publisher's priors before restoring the resident publisher.
        time.sleep(5.1)
        for path,value in state['controls'].items():attempt(lambda p=path,v=value:write_priv(p,v))
        if state['service_active']:attempt(lambda:systemctl('start',SERVICE))
        else:attempt(lambda:systemctl('stop',SERVICE))
        try:
            if (systemctl('is-active',SERVICE,check=False).stdout.strip()=='active')!=state['service_active']:
                errors.append('restore mismatch: resident service activity')
        except Exception as exc:errors.append(repr(exc))
        for path,value in state['controls'].items():
            try:
                if read_priv(path)!=value:errors.append('restore mismatch: '+path)
            except Exception as exc:errors.append(repr(exc))
        state.update(restored=not errors,restore_errors=errors,restored_at=time.time())
        write_json(state_path,state)


def guardian(state_path,pid):
    fd=os.pidfd_open(pid)
    Path(str(state_path)+'.guardian-ready').write_text(str(os.getpid()))
    try:
        while not select.select([fd],[],[],.5)[0]:
            if json.loads(Path(state_path).read_text()).get('restored'):return
        restore(state_path)
    finally:os.close(fd)


@contextlib.contextmanager
def exclusive_experiment(out):
    lock=Path(f'/tmp/parp-oracle-{os.getuid()}.lock').open('w')
    fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    state_path=out/'recovery-state.json'
    state=dict(controls={p:read_priv(p) for p in CONTROL_VALUES},
               service_active=systemctl('is-active',SERVICE,check=False).stdout.strip()=='active',
               units=[],roots=[],processes=[],restored=False,parent_pid=os.getpid())
    write_json(state_path,state)
    proc=subprocess.Popen([sys.executable,str(Path(__file__).resolve()),'--guardian',str(state_path),str(os.getpid())],
        stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=(out/'guardian.log').open('a'),
        start_new_session=True,pass_fds=(lock.fileno(),))
    try:
        U.P.wait_for(lambda:Path(str(state_path)+'.guardian-ready').exists(),'independent recovery guardian',5)
        if state['service_active']:systemctl('stop',SERVICE)
        if systemctl('is-active',SERVICE,check=False).stdout.strip()=='active':raise RuntimeError('Resident writer still active')
        for path,value in CONTROL_VALUES.items():write_priv(path,value)
        yield state_path
    finally:
        restore(state_path)
        proc.wait(timeout=15)
        fcntl.flock(lock,fcntl.LOCK_UN);lock.close()


def remember(state_path,*,unit=None,root=None):
    state=json.loads(state_path.read_text())
    if unit and unit not in state['units']:state['units'].append(unit)
    if root and str(root) not in state['roots']:state['roots'].append(str(root))
    write_json(state_path,state)


def remember_desktop(state_path,out):
    state=json.loads(state_path.read_text())
    for pid in json.loads((out/'desktop.json').read_text())['owned_pids']:
        identity=U.P.desktop_api.process_identity(pid)
        if not identity:raise RuntimeError('Private desktop process disappeared during startup')
        state.setdefault('processes',[]).append(dict(pid=pid,starttime_ticks=identity[1]))
    write_json(state_path,state)


class ContinuousReplay(U.Replay):
    def __init__(self,out,f,pages,slice_name,state_path):
        super().__init__(out,f,pages);self.service_slice=slice_name
        self.recovery=state_path;self.controller=None
        self.allowed_restarts=set()
        self.wps_launch_mode='components'

    def wps_command(self,path):
        if not hasattr(self,'wps_office_dir'):
            self.wps_office_dir=prepare_runtime(self.out/'wps-runtime')
        return super().wps_command(path)

    def owned(self,app,row):
        main=U.P.gui.unit(self.out,app)
        allowed={main,*[u for u in self.extra_units if u.startswith(main.removesuffix('.service')+'-')]}
        return bool(allowed.intersection(row['cgroup'].split('/')))

    def inventory(self,app):
        live=[]
        for row in super().inventory(app):
            try:state=Path(f'/proc/{row["pid"]}/stat').read_text().rsplit(')',1)[1].split()[0]
            except FileNotFoundError:continue
            if state=='Z':
                log(self.out/'reaped-or-zombie-processes.jsonl',dict(app=app,**row))
            else:live.append(row)
        return live

    def start(self,app,cmd):
        remember(self.recovery,unit=U.P.gui.unit(self.out,app))
        super().start(app,cmd)
        self.allowed_restarts.discard(app)

    def ensure(self,app):
        if app in self.attempted and app not in self.allowed_restarts:
            if not any(self.owned(app,row) and row['is_normal_window'] for row in self.state()['windows']):
                raise RuntimeError('Unexpected application window loss; automatic restart forbidden: '+app)
        return super().ensure(app)

    def launch_command(self,app):
        if app=='SOFTWARE':return ['gnome-software','--mode=installed','--prefer-local']
        return super().launch_command(app)

    def input(self,app,label,*args):
        if self.controller:self.controller.check()
        return super().input(app,label,*args)

    def open_file(self,app,path):
        lifecycle=app in {'WPS','IMAGE_VIEWER'}
        if lifecycle and self.controller:self.controller.disarm('document_relaunch')
        result=super().open_file(app,path)
        if lifecycle and self.controller:self.controller.arm(self.controller.index,app,'document_relaunch')
        return result

    def archive(self,op,event):
        unit=U.P.gui.unit(self.out,'FILE_ROLLER').replace('.service',f'-row{event["excel_row"]}.service')
        remember(self.recovery,unit=unit)
        return super().archive(op,event)


class Controller:
    def __init__(self,replay,plan,root,bridge,policy,out,reference,pressure):
        self.r=replay;self.lookup=NextUse(plan['segments']);self.root=root;self.bridge=bridge
        self.policy=policy;self.out=out;self.reference=reference;self.pressure=pressure
        self.index=-1;self.expected=None;self.deadline=0.;self.error=None
        self.lock=threading.RLock();self.stop_event=threading.Event();self.thread=None
        self.anchor=None;self.release_at=None;self.released=False;self.pressure_before=None;self.pressure_after=None
        self.pressure_kernel_before=None;self.pressure_kernel_after=None
        self.first_sample=None;self.last_sample=None;self.updates=0;self.last_publish=0.;self.last_signature=None
        self.focus_mismatch_since=None
        self.last_live=set();self.allowed_departures=set()
        self.thread=threading.Thread(target=self.loop,daemon=True);self.thread.start()

    def check(self):
        if self.error:raise RuntimeError(self.error)

    def disarm(self,reason):
        with self.lock:
            if reason in {'explicit_close','document_relaunch'} and self.expected:
                self.allowed_departures.add(self.expected)
                self.r.allowed_restarts.add(self.expected)
            self.expected=None;self.deadline=time.monotonic()+90
            log(self.out/'lifecycle.jsonl',dict(index=self.index,reason=reason,at=time.monotonic()))

    def live(self):
        result={};samples=[]
        for app in set(self.r.attempted):
            main=U.P.gui.unit(self.r.out,app)
            units=[main]+[u for u in self.r.extra_units if u.startswith(main.removesuffix('.service')+'-')]
            app_paths=[];app_samples=[];populated=False
            for unit in units:
                path=self.root/unit
                try:
                    unit_populated=kv(path/'cgroup.events').get('populated',0)==1
                    unit_samples=[]
                    for p in [path]+[f.parent for f in path.rglob('cgroup.procs') if f.parent!=path]:
                        (p/'cgroup.procs').read_text()  # Ensure the owned domain still exists.
                        unit_samples.append(SimpleNamespace(app_id=app,identity=SimpleNamespace(cgroup_path='/'+str(p.relative_to('/sys/fs/cgroup')))))
                    if unit_samples:
                        app_paths.append(path);app_samples.extend(unit_samples);populated|=unit_populated
                except FileNotFoundError:continue  # Unit exited during this inventory; never invent a binding.
            # Include empty owned ancestor/helper domains too: charged pages can
            # remain there while another process of this application is alive.
            if populated:result[app]=app_paths;samples.extend(app_samples)
        return result,samples

    def foreground(self,app):
        state=self.r.state()
        return any(w['window_id']==state['active_window'] and self.r.owned(app,w) for w in state['windows'])

    def publish(self,reason,live,samples):
        for attempt in range(3):
            try:return self._publish_once(reason,live,samples)
            except FileNotFoundError:
                if attempt==2:raise
                # A short-lived archive helper can exit between inventory and stat.
                # Recollect the complete set; never retry ambiguity or ioctl failure.
                live,samples=self.live()
                if not self.foreground(self.expected):raise RuntimeError('Foreground changed during binding refresh')

    def _publish_once(self,reason,live,samples):
        lost=self.last_live-set(live)
        if lost-self.allowed_departures:raise RuntimeError('Unexpected application exit: '+','.join(sorted(lost-self.allowed_departures)))
        service_state=systemctl('is-active',SERVICE,check=False).stdout.strip()
        if service_state in {'active','activating','reloading'}:
            raise RuntimeError('Resident publisher became active during experiment')
        signature=self.binding_signature(samples)
        entries,audit=self.lookup.rank(self.index,self.expected,live)
        audit['reason']=reason;audit['plan_sha256']=self.plan_hash
        self.bridge.publish(entries,audit,samples);self.updates+=1
        self.allowed_departures-=lost;self.last_live=set(live)
        self.last_publish=time.monotonic()
        self.last_signature=signature
        return audit

    @staticmethod
    def binding_signature(samples):
        return tuple(sorted((s.app_id,s.identity.cgroup_path,
            (Path('/sys/fs/cgroup')/s.identity.cgroup_path.lstrip('/')).stat().st_ino) for s in samples))

    def arm(self,index,app,reason='switch'):
        with self.lock:
            self.check()
            if reason=='switch' and index!=self.index+1:raise RuntimeError('Nonsequential segment cursor')
            if reason!='switch' and (reason not in {'document_relaunch','same_segment_reopen'} or index!=self.index):
                raise RuntimeError('Lifecycle update cannot advance segment cursor')
            if not self.foreground(app):raise RuntimeError('Target foreground not verified')
            self.index=index;self.expected=app;self.deadline=time.monotonic()+90
            live,samples=self.live();audit=self.publish(reason,live,samples)
            self.allowed_departures.discard(app)
            self.r.allowed_restarts.discard(app)
            if reason=='switch':
                log(self.out/'switches.jsonl',dict(segment_index=index,foreground=app,at=time.monotonic(),generation=self.bridge.generation))
                self.maybe_pressure(live,audit)

    def snapshot(self,live):
        def one(path):
            psi={}
            for line in (path/'memory.pressure').read_text().splitlines():
                label,*fields=line.split();psi[label]=dict(f.split('=') for f in fields)
            return dict(current=int((path/'memory.current').read_text()),swap=int((path/'memory.swap.current').read_text()),
                        stat=kv(path/'memory.stat'),events=kv(path/'memory.events'),psi=psi)
        apps={}
        for a,ps in live.items():
            apps[a]=[]
            for p in ps:
                try:apps[a].append(dict(path=str(p),**one(p)))
                except FileNotFoundError:continue
        return dict(at=time.monotonic(),segment_index=self.index,root=one(self.root),apps=apps)

    def maybe_pressure(self,live,audit):
        if not self.pressure or self.anchor is not None:return
        usage=int((self.root/'memory.current').read_text())
        if self.policy=='native':
            finite={r['next_segment'] for r in audit['rows'] if r['app_key']!=self.expected and r['next_segment'] is not None}
            if len(live)-1<3 or len(finite)<2 or usage<768*MIB:return
            anchor=dict(segment_index=self.index,usage=usage,memory_max=usage-192*MIB,duration_s=60)
        else:
            if not self.reference:return
            if self.index!=self.reference['segment_index']:return
            anchor=dict(self.reference,oracle_usage=usage)
            write_json(self.out/'pressure-comparability.json',dict(segment_index=self.index,
                native_usage=self.reference['usage'],oracle_usage=usage,
                relative_difference=abs(usage-self.reference['usage'])/self.reference['usage']))
            if abs(usage-self.reference['usage'])/self.reference['usage']>.15:
                raise RuntimeError('Pressure pair working-set difference exceeds 15%')
        self.anchor=anchor;self.pressure_before=self.snapshot(live)
        write_json(self.out/'pressure-anchor.json',anchor)
        self.pressure_kernel_before=kernel_stats()
        write_json(self.out/'pressure-kernel-before.json',self.pressure_kernel_before)
        # Set release deadline before a potentially slow memory.max write.
        self.release_at=time.monotonic()+60
        write_priv(self.root/'memory.max',anchor['memory_max'])

    def release(self):
        if self.release_at is not None and not self.released:
            self.pressure_after=self.snapshot(self.live()[0])
            write_priv(self.root/'memory.max','max');self.released=True
            self.pressure_kernel_after=kernel_stats()
            write_json(self.out/'pressure-kernel-after.json',self.pressure_kernel_after)
            log(self.out/'pressure-events.jsonl',dict(event='released',at=time.monotonic()))

    def loop(self):
        try:
            while not self.stop_event.wait(.25):
                with self.lock:
                    now=time.monotonic()
                    if self.release_at is not None and now>=self.release_at:self.release()
                    live,samples=self.live()
                    snap=self.snapshot(live);self.last_sample=snap
                    if self.first_sample is None:self.first_sample=snap
                    log(self.out/'memory-timeline.jsonl',snap)
                    if snap['root']['events'].get('oom',0) or snap['root']['events'].get('oom_kill',0):raise RuntimeError('Experiment cgroup OOM')
                    if not self.expected:continue
                    if now>self.deadline:raise RuntimeError('Executor action deadline exceeded; oracle lease stopped')
                    if not self.foreground(self.expected):
                        if self.focus_mismatch_since is None:self.focus_mismatch_since=now
                        if now-self.focus_mismatch_since>.75:raise RuntimeError('Unexpected foreground switch; cursor not advanced')
                        continue
                    self.focus_mismatch_since=None
                    try:signature=self.binding_signature(samples)
                    except FileNotFoundError:continue
                    if signature!=self.last_signature or now-self.last_publish>=1:
                        self.publish('bindings_changed' if signature!=self.last_signature else 'lease',live,samples)
        except Exception as exc:
            self.error=repr(exc)
            log(self.out/'controller-errors.jsonl',dict(error=self.error,traceback=traceback.format_exc(),at=time.monotonic()))
            self.release()

    def close(self):
        self.stop_event.set();self.thread.join(timeout=15);self.release()
        if self.thread.is_alive():raise RuntimeError('Controller did not stop')


def run_half(plan,policy,out,state_path,reference,pressure,max_segments=None):
    out.mkdir();write_json(out/'plan.json',plan)
    name='oracle'+hashlib.sha256(str(out).encode()).hexdigest()[:10]+'.slice'
    remember(state_path,unit=name)
    systemctl('start',name)
    rel=systemctl('show',name,'-p','ControlGroup','--value').stdout.strip()
    if not rel:raise RuntimeError('Experiment slice missing')
    root=Path('/sys/fs/cgroup')/rel.lstrip('/');remember(state_path,root=root)
    actors={}
    for label,pid in [('controller',os.getpid()),('guardian',int(Path(str(state_path)+'.guardian-ready').read_text()))]:
        membership=Path(f'/proc/{pid}/cgroup').read_text().strip()
        actor_path=Path('/sys/fs/cgroup')/membership.split('::',1)[1].lstrip('/')
        if actor_path.is_relative_to(root):raise RuntimeError('Control process is inside pressure subtree')
        actors[label]=dict(pid=pid,cgroup=membership)
    write_json(out/'isolation.json',dict(pressure_root=str(root),actors=actors))
    write_priv(root/'memory.max','max');write_priv(root/'memory.swap.max',1024*MIB)
    write_priv(root/'memory.tier2_enabled',int(policy=='oracle'))
    write_priv(DEBUG/'mode',2 if policy=='oracle' else 0)
    write_priv('/proc/sys/vm/parp_reclaim_bin_enabled',int(policy=='oracle'))
    controls={p:read_priv(p) for p in CONTROL_VALUES};write_json(out/'controls.json',controls)
    for p,want in CONTROL_VALUES.items():
        expected=2 if p==str(DEBUG/'mode') and policy=='oracle' else 1 if p.endswith('/parp_reclaim_bin_enabled') and policy=='oracle' else want
        if controls[p]!=str(expected):raise RuntimeError('Control mismatch: '+p)
    fixtures={}
    for group in sorted({e['group_index'] for s in plan['segments'] for e in s['events']}):
        g=out/f'source-{group:02d}';g.mkdir();fixtures[group]=U.fixtures(g)
    result=dict(policy=policy,status='RUNNING',segments_completed=0,operations_completed=0,plan_sha256=plan['plan_sha256'])
    before=kernel_stats();write_json(out/'kernel-before.json',before)
    pages=U.LocalPages(out,fixtures[1]);r=None;controller=None;bridge=None
    try:
        with U.P.desktop_api.desktop('isolated',out):
            remember_desktop(state_path,out)
            r=ContinuousReplay(out,fixtures[1],pages,name,state_path)
            write_json(out/'desktop-env.json',{'DISPLAY':os.environ['DISPLAY']})
            scope=load_runtime_app_scope(ROOT/'test/configs/source20_phase1/runtime_app_scope.json')
            bridge=OracleBridge(policy_root=root,mode='apply' if policy=='oracle' else 'dry-run',device='/dev/myfs',
                                runtime_scope=scope,output_dir=out,session_id=out.name)
            controller=Controller(r,plan,root,bridge,policy,out,reference,pressure)
            controller.plan_hash=plan['plan_sha256'];r.controller=controller
            prepared=set();began=time.monotonic()
            try:
                for segment in plan['segments'][:max_segments]:
                    app=segment['app_key'];start=time.monotonic()
                    controller.disarm('planned_switch');r.f=fixtures[segment['events'][0]['group_index']]
                    r.current_row=segment['events'][0]['excel_row']
                    try:
                        r.ensure(app);controller.arm(segment['index'],app)
                    except Exception as exc:
                        log(out/'switch-errors.jsonl',dict(segment_index=segment['index'],app=app,error=repr(exc),screenshot=r.screenshot('switch-failed')))
                        raise
                    log(out/'return-probes.jsonl',dict(segment_index=segment['index'],app=app,focus_ready_s=time.monotonic()-start))
                    for operation_index,event in enumerate(segment['events']):
                        controller.check();r.current_row=event['excel_row'];r.f=fixtures[event['group_index']]
                        controller.deadline=time.monotonic()+90
                        if controller.expected is None:
                            r.ensure(app)
                            controller.arm(segment['index'],app,'same_segment_reopen')
                        marker=(event['group_index'],app)
                        if marker not in prepared:
                            # Reuse processes across source boundaries, but initialize their local document/directory explicitly.
                            if app=='FILES':r.navigate('testingFile/4k')
                            elif app in {'WPS','LIBREOFFICE'} and event['operation']!='document_open':
                                r.open_file(app,r.f/('word.docx' if app=='WPS' else 'lo-word.docx'))
                            prepared.add(marker)
                            log(out/'preparations.jsonl',dict(row=event['excel_row'],group=event['group_index'],app=app,reason='source-local fixtures in persistent session'))
                        if event['operation']=='close' or (event['operation']=='key' and app=='IMAGE_VIEWER' and 'Escape' in event['params']['keys']):
                            controller.disarm('explicit_close')
                        tick=time.monotonic()
                        try:
                            detail=r.action(event);controller.check()
                            log(out/'results.jsonl',dict(excel_row=event['excel_row'],segment_index=segment['index'],app=app,
                                event=event['event1'],detail=detail,elapsed_s=time.monotonic()-tick,status='COMPLETED'))
                            if operation_index==0:
                                log(out/'operation-probes.jsonl',dict(segment_index=segment['index'],app=app,
                                    first_operation_s=time.monotonic()-tick,verification=detail.get('verification'),
                                    focus_and_first_operation_s=time.monotonic()-start))
                        except Exception as exc:
                            log(out/'results.jsonl',dict(excel_row=event['excel_row'],segment_index=segment['index'],app=app,
                                status='FAILED',error=repr(exc),screenshot=r.screenshot('failed')))
                            raise
                        result['operations_completed']+=1
                    result['segments_completed']+=1
                    write_json(out/'progress.json',result)
                    print(f'{policy}: segment {result["segments_completed"]}/{len(plan["segments"])}; operations {result["operations_completed"]}',flush=True)
                if controller.release_at is not None:
                    while not controller.released:controller.check();time.sleep(.25)
                controller.check()
                result.update(status='REPLAY_PASS' if max_segments is None else 'SMOKE_PASS',elapsed_s=time.monotonic()-began)
            finally:
                controller.close();r.controller=None
                result['cleanup']=r.finish()
                if not all(x['passed'] for x in result['cleanup'].values()):result['status']='INVALID'
    except Exception as exc:
        result.update(status='INVALID',error=repr(exc))
    finally:
        if controller:
            controller.close();result.update(pressure_anchor=controller.anchor,kernel_updates=controller.updates,
                                            pressure_released=controller.released)
        if bridge:bridge.close()
        pages.close();write_priv(root/'memory.max','max')
        write_priv(root/'memory.tier2_enabled',0);systemctl('stop',name,check=False)
        after=kernel_stats();write_json(out/'kernel-after.json',after)
        delta={k:after[k]-before.get(k,0) for k in after};write_json(out/'kernel-delta.json',delta)
        result['kernel_delta']=delta
        result['pressure_status']='NOT_TRIGGERED' if not result.get('pressure_anchor') else 'INVALID' if result['status']=='INVALID' else 'COMPLETED'
        if controller and controller.pressure_before and controller.pressure_after:
            a,b=controller.pressure_before,controller.pressure_after
            write_json(out/'pressure-before.json',a);write_json(out/'pressure-after.json',b)
            result['pressure_metrics']=snapshot_delta(a,b)
            reclaimed=result['pressure_metrics']['root']['stat_delta'].get('pgsteal',0)>0
            result['reclaim_observed']=reclaimed
            pressure_delta={k:v-controller.pressure_kernel_before.get(k,0) for k,v in controller.pressure_kernel_after.items()}
            result['pressure_kernel_delta']=pressure_delta
            write_json(out/'pressure-kernel-delta.json',pressure_delta)
            result['bin_context_used']=policy=='oracle' and pressure_delta.get('context_hits',0)>0 and pressure_delta.get('rank_scores',0)>0
            result['reclaim_loop_passed']=result['status']=='REPLAY_PASS' and reclaimed and (policy=='native' or result['bin_context_used'])
        write_json(out/'result.json',result)
    return result


def snapshot_delta(before,after):
    def one(a,b):
        return dict(memory_change_bytes=b['current']-a['current'],swap_change_bytes=b['swap']-a['swap'],
            stat_delta={k:b['stat'].get(k,0)-a['stat'].get(k,0) for k in ['pgscan','pgsteal','pgmajfault','workingset_refault_anon','workingset_refault_file']},
            psi_delta_us={k:int(b['psi'].get(k,{}).get('total',0))-int(a['psi'].get(k,{}).get('total',0)) for k in ['some','full']})
    result=dict(root=one(before['root'],after['root']),apps={})
    for app,paths in before['apps'].items():
        matches={p['path']:p for p in after['apps'].get(app,[])}
        result['apps'][app]=[dict(path=p['path'],**one(p,matches[p['path']])) if p['path'] in matches else
                             dict(path=p['path'],status='exited_or_restarted_not_comparable') for p in paths]
    return result


def audit_publications(plan,updates,policy):
    errors=[];cursor=-1;generation=-1
    for number,u in enumerate(updates):
        try:
            i=u['segment_index'];fg=u['foreground'];live=set(u['live_apps'])
            assert 0<=i<len(plan['segments']) and plan['segments'][i]['app_key']==fg
            assert i==(cursor+1 if u['reason']=='switch' else cursor)
            cursor=i
            assert fg in live and len(live)==len(u['rows'])
            assert {r['app_key'] for r in u['rows']}==live
            ids={s['app_key']:s['app_id'] for s in plan['segments']}
            future={a:next((j for j in range(i+1,len(plan['segments'])) if plan['segments'][j]['app_key']==a),None) for a in live-{fg}}
            early=sorted(future,key=lambda a:(future[a] if future[a] is not None else float('inf'),ids[a]))
            late=sorted(future,key=lambda a:(-(future[a] if future[a] is not None else float('inf')),ids[a]))
            assert u['reclaim_order']==late
            assert [r['app_key'] for r in u['rows']]==[fg]+early
            for rank,row in enumerate(u['rows'],1):
                assert row['rank']==rank and row['app_id']==ids[row['app_key']]
                assert row['score_q15']==(32767 if rank==1 else 0)
                assert row['next_segment']==future.get(row['app_key'])
            assert {b['app_id'] for b in u['bindings']}=={ids[a] for a in live}
            assert len({b['domain_id'] for b in u['bindings']})==len(u['bindings'])
            assert u['ttl_ms']==5000 and u['generation']>generation
            generation=u['generation']
            assert u['mode']==('apply' if policy=='oracle' else 'dry-run')
            assert u['readback_verified']==(policy=='oracle')
        except (AssertionError,KeyError,TypeError,IndexError) as exc:
            errors.append(dict(publication=number,error=repr(exc)))
    return dict(status='PASS' if updates and not errors else 'FAIL',publications=len(updates),
        last_segment_index=cursor,errors=errors,kernel_readback_batches=sum(bool(u.get('readback_verified')) for u in updates),
        scope='Only published segments; this does not certify completion of all 85 segments.')


def report(out,plan,results):
    audits={}
    lines=['# 已知未来序列驱动 bin：闭环验证','',
        f'计划：{len(plan["segments"])} 个应用段，{sum(len(s["events"]) for s in plan["segments"])} 条应用操作。',
        '后台排序依据严格未来的下一次出现位置；无后续访问视为无限远。分数不是模型概率。','',
        '| 轮次 | 回放状态 | 完成段数 | 完成操作数 | 压力 | 实际回收 | bin 上下文证据 |',
        '|---|---|---:|---:|---|---|---|']
    for r in results:
        lines.append(f'| {r["policy"]} | {r["status"]} | {r["segments_completed"]} | {r["operations_completed"]} | {r["pressure_status"]} | {r.get("reclaim_observed",False)} | {r.get("bin_context_used",False)} |')
        if r.get('error'):lines+=['',r['policy']+' 错误：'+r['error'],'']
        path=out/r['policy']/'parp/oracle_updates.jsonl'
        if path.exists():
            updates=[json.loads(l) for l in path.read_text().splitlines()]
            audits[r['policy']]=audit_publications(plan,updates,r['policy'])
            with (out/r['policy']/'switch-rankings.csv').open('w',encoding='utf-8-sig',newline='') as f:
                writer=csv.writer(f);writer.writerow(['段序号','更新原因','前台','回收优先顺序','应用','下次段序号','内核rank','预计bin','generation','内核读回校验'])
                for u in updates:
                    if u['reason']=='lease':continue
                    for row in u['rows']:
                        writer.writerow([u['segment_index'],u['reason'],u['foreground'],' → '.join(u['reclaim_order']),
                            row['app_key'],row['next_segment'],row['rank'],row['expected_bin'],u['generation'],u['readback_verified']])
            with (out/r['policy']/'switch-rankings.csv').open(encoding='utf-8-sig',newline='') as f:
                write_book(out/r['policy']/'switch-rankings.xlsx',[
                    ('逐次排序',list(csv.reader(f)),[12,26,22,70,22,16,14,14,18,20]),
                    ('说明',[['项目','说明'],['段序号','从 0 开始，与 plan.json、内核审计一致'],
                        ['空白下次段','前台行不适用；后台行表示后续不再出现'],
                        ['rank','越小越早访问；回收顺序从后续不再出现到较早访问'],
                        ['内核读回','native 为模拟提交，oracle 为真实 ioctl 后 GET_STATE 校验'],
                        ['续租','表格省略 lease；完整日志保留全部 1 秒续租']], [22,110])])
    paired=len(results)==2 and all(r.get('reclaim_loop_passed') for r in results)
    def records(policy,name):
        path=out/policy/name
        return {r['segment_index']:r for r in (json.loads(l) for l in path.read_text().splitlines())} if path.exists() else {}
    native=records('native','operation-probes.jsonl');oracle=records('oracle','operation-probes.jsonl')
    comparisons=[]
    for index in sorted(native.keys() & oracle.keys()):
        a,b=native[index],oracle[index]
        if a['app']!=b['app']:raise RuntimeError('Response comparison app mismatch')
        comparisons.append(dict(segment_index=index,app=a['app'],
            native_s=a['focus_and_first_operation_s'],oracle_s=b['focus_and_first_operation_s'],
            delta_s=b['focus_and_first_operation_s']-a['focus_and_first_operation_s'],
            native_verification=a['verification'],oracle_verification=b['verification']))
    performance=dict(valid_pair=paired,matched_segments=len(comparisons),entries=comparisons,
        note='Focus plus first operation, including first launches and preparation; not a claim of fully verified business completion or statistically established improvement.')
    if paired and comparisons:
        performance.update(native_mean_s=sum(r['native_s'] for r in comparisons)/len(comparisons),
                           oracle_mean_s=sum(r['oracle_s'] for r in comparisons)/len(comparisons))
    write_json(out/'performance-comparison.json',performance)
    write_json(out/'publication-audit.json',audits)
    lines+=['','排序及提交审计（仅覆盖已发布的应用段）：']
    for policy,audit in audits.items():
        lines.append(f'- {policy}: {audit["status"]}，{audit["publications"]} 批排序，{audit["kernel_readback_batches"]} 批真实内核读回。')
    lines+=['',f'受控回收闭环验收：{"通过" if paired else "未通过/条件未满足"}。',
        '仅出现 ioctl 成功或无压力回放完成，不算回收闭环通过；不承诺未来信息轮性能更优。',
        '逐应用内存、swap、refault、major fault、扫描/回收和 PSI 见 pressure_metrics 与 memory-timeline.jsonl。',
        '焦点到达耗时见 return-probes.jsonl，首次操作反馈及耗时见 operation-probes.jsonl；二者不混同业务结果已验证。',
        '完整排序、绑定及 generation 见 parp/oracle_updates.jsonl，便于查看的排序表见 switch-rankings.csv。',
        '原始操作采用已确认的本地替代素材；未声称真实网站或原始大文件工作集已还原。']
    (out/'REPORT.md').write_text('\n'.join(lines)+'\n')
    write_json(out/'acceptance.json',dict(reclaim_loop_passed=paired,performance_improvement_claimed=False,
        published_sorting_and_submission=audits,
        all_segments_replayed=len(results)==2 and all(r['segments_completed']==len(plan['segments']) and r['status']=='REPLAY_PASS' for r in results)))


def main(argv=None):
    p=argparse.ArgumentParser();p.add_argument('command',choices=['plan','preflight','run'])
    p.add_argument('--comparison',type=Path,default=ROOT/'test_reports/user_events原始与当前复现对比.xlsx')
    p.add_argument('--xlsx',type=Path,default=Path('/home/lzx/Desktop/user_events合并.xlsx'))
    p.add_argument('--output-dir',type=Path,required=True)
    p.add_argument('--no-pressure',action='store_true');p.add_argument('--max-segments',type=int)
    args=p.parse_args(argv);out=args.output_dir.resolve();out.mkdir(parents=True,exist_ok=False)
    write_json(out/'code-manifest.json',{str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in
        [Path(__file__).resolve(),Path(U.__file__).resolve(),Path(__file__).with_name('oracle_next_use.py'),
         Path(__file__).with_name('wps_runtime.py'),Path(U.P.__file__).resolve()]})
    plan=compile_plan(args.comparison,args.xlsx);write_json(out/'plan.json',plan)
    if args.command=='plan':print(f'{len(plan["segments"])} segments');return 0
    preflight=dict(kernel=os.uname().release,device=Path('/dev/myfs').exists(),
        controls={p:read_priv(p) for p in CONTROL_VALUES},service=systemctl('is-active',SERVICE,check=False).stdout.strip())
    write_json(out/'preflight.json',preflight)
    if args.command=='preflight':print(json.dumps(preflight));return 0
    if args.max_segments is not None and not args.no_pressure:p.error('Limited smoke runs must use --no-pressure')
    if args.max_segments is not None and not 1<=args.max_segments<=len(plan['segments']):p.error('Invalid smoke segment count')
    for sig in [signal.SIGTERM,signal.SIGINT]:signal.signal(sig,lambda *_:(_ for _ in ()).throw(KeyboardInterrupt()))
    results=[]
    with exclusive_experiment(out) as state:
        reference=None
        for policy in ['native','oracle']:
            result=run_half(plan,policy,out/policy,state,reference,not args.no_pressure,args.max_segments)
            results.append(result);write_json(out/'summary.json',results);report(out,plan,results)
            # An invalid baseline remains invalid. A separate oracle attempt can
            # still diagnose kernel reception/reclaim; it cannot make the pair pass.
            reference=result.get('pressure_anchor')
    return int(len(results)!=2 or any(r['status']=='INVALID' for r in results))


if __name__=='__main__':
    if len(sys.argv)>1 and sys.argv[1]=='--guardian':guardian(Path(sys.argv[2]),int(sys.argv[3]))
    else:raise SystemExit(main())
