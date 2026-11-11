"""RankProp 4.2: past-window resident reuse estimates and service fusion.

The temporal model is unchanged. Its eviction score is transformed to a
positive, monotonically decreasing protection weight; it is not a probability.
"""
from __future__ import annotations
import json, math, os, subprocess, sys, time, threading
from pathlib import Path
from core.collection_profile import profiled
from concurrent.futures import ThreadPoolExecutor
from collections import defaultdict
from core.reclaim_types import enabled as lru_types_enabled, resident_view, compatible_reuse_state, saved_reuse_state

Q15=32767
MODEL_VERSION=402

def temporal_weight(eviction_score):
    return 1./(1.+math.exp(max(-60.,min(60.,4.*(float(eviction_score)-.5)))))

def conservative_reuse(ratio,confidence):
    return 1.-max(0.,min(.95,confidence))*(1.-max(0.,min(1.,ratio)))

def fused_score(temporal, reuse, active_fraction):
    return max(0.,min(1.,max(temporal*reuse,active_fraction)))

class ResidentReuseController:
    def __init__(self,output_dir,runtime_scope,policy_path=None):
        self.output_dir=Path(output_dir);self.scope=runtime_scope;self.policy_path=policy_path
        self.audit=(self.output_dir/'resident_reuse.jsonl').open('a',buffering=1)
        self.helper_errors=(self.output_dir/'resident_reuse_worker.stderr.log').open('a',buffering=1)
        self.state_path=Path(os.environ.get('PARP42_STATE_PATH',str(Path.home()/'.local/state/parp/resident_reuse42.json')))
        try:self.learned=compatible_reuse_state(json.loads(self.state_path.read_text()))
        except (OSError,ValueError):self.learned={}
        self.focus_state_path=self.output_dir/'control_foreground.json'
        self.proactive_context={};self.activities={};self.foreground='';self.last=0.;self.pending=None;self.closed=False
        self.foreground_started=0.;self.observation_sequence=0
        self.control_lock=threading.RLock();self.intent_profiles={};self.cancelled_visits=[]
        profile_path=os.environ.get('PARP_SERVICE_CANCEL_PROFILES','')
        if profile_path:
            import re
            profiles=json.loads(Path(profile_path).read_text())
            if not isinstance(profiles,dict):raise ValueError('cancel profiles must map app names to title regex lists')
            for key,patterns in profiles.items():
                if not isinstance(patterns,list) or not all(isinstance(p,str) for p in patterns):raise ValueError('invalid cancel profile list')
                for pattern in patterns:re.compile(pattern)
            self.intent_profiles=profiles
        self.window_intent=None
        if os.environ.get('PARP_SERVICE_WINDOW_INTENT','0')=='1':
            if os.environ.get('PARP_SERVICE_PROACTIVE_BACKEND')!='coordinated-intent':raise ValueError('window intent requires coordinated-intent backend')
            from core.window_intent import WindowIntent
            self.window_intent=WindowIntent(self.intent_profiles)
        self.executor=ThreadPoolExecutor(max_workers=1,thread_name_prefix='parp42-page-probe')
        self.helper=None;self.error='';self.low_targets={}
        from core.workload_reserve import WorkloadReserve
        self.schedule_budget=WorkloadReserve()
        self.scheduler_state_path=self.output_dir/'control_scheduler.json';self.scheduler_published_ns=0;self.scheduler_previous_need=0
        self.force_reuse_one=os.environ.get('PARP42_REUSE_ONE')=='1'
        self.active_low=os.environ.get('PARP42_ACTIVE_LOW','1')=='1'
        self.buffered_reads = None
        if os.environ.get('PARP_SERVICE_PROACTIVE_BACKEND','').startswith('coordinated-') or os.environ.get('PARP_SERVICE_PROACTIVE_BACKEND') in ('typed-shared','typed-ahead','typed-ahead-observe','typed-ahead-guard','typed-operation','typed-deadline','joint-native','joint-high','joint-reclaim','joint-both','coordinated-native','coordinated-high','coordinated-reclaim','coordinated-both'):
            from core.buffered_demand import BufferedReads
            self.buffered_reads=BufferedReads()
        self.io_accounting=None
        if self.buffered_reads is not None:
            from core.io_accounting import IOAccounting
            self.io_accounting=IOAccounting()
        self.audit.write(json.dumps(dict(kind='resident_capabilities',timestamp_ns=time.monotonic_ns(),backend=os.environ.get('PARP_SERVICE_PROACTIVE_BACKEND','legacy'),buffered_reads_registered=self.buffered_reads is not None,file_event_source=os.environ.get('PARP_SERVICE_FILE_EVENT_SOURCE','ebpf')))+'\n')
        self._start_helper()

    def _start_helper(self):
        try:
            script=Path(__file__).with_name('resident_page_probe.py')
            self.helper=subprocess.Popen(['sudo','-n','env','PARP_SERVICE_SMAPS_PARSER='+os.environ.get('PARP_SERVICE_SMAPS_PARSER','legacy'),'PARP_COLLECTION_PROFILE_DIR='+os.environ.get('PARP_COLLECTION_PROFILE_DIR',''),sys.executable,'-u',str(script),'--proactive-backend',os.environ.get('PARP_SERVICE_PROACTIVE_BACKEND','legacy'),'--proactive-observe',os.environ.get('PARP_SERVICE_PROACTIVE_OBSERVE','0'),'--page-cohort',os.environ.get('PARP_SERVICE_PAGE_COHORT','0'),'--shared-smaps',os.environ.get('PARP_SERVICE_SHARED_SMAPS','0'),'--precontrol-origin',os.environ.get('PARP_SERVICE_PRECONTROL_ORIGIN','0'),'--reuse-phase',os.environ.get('PARP_SERVICE_REUSE_PHASE','0'),'--resident-sampler',os.environ.get('PARP_SERVICE_RESIDENT_SAMPLER','legacy'),'--page-seed',os.environ.get('PARP_SERVICE_PAGE_SEED','402'),'--typed-lru-basis',os.environ.get('PARP_SERVICE_TYPED_LRU_BASIS','0'),'--foreground-quiet-ms',os.environ.get('PARP_SERVICE_FOREGROUND_QUIET_MS','0')],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=self.helper_errors,text=True,bufsize=1)
            # All blocking helper I/O stays on the single observation thread.
            self.pending=self.executor.submit(self._hello)
        except OSError as exc:self.error=str(exc)

    def _hello(self):
        line=self.helper.stdout.readline()
        result=json.loads(line)
        if not result.get('ready'):raise RuntimeError(result)
        if lru_types_enabled() and result.get('typed_lru_basis')!='1':raise RuntimeError('helper type-basis capability mismatch')
        if result.get('foreground_quiet_ms')!=int(os.environ.get('PARP_SERVICE_FOREGROUND_QUIET_MS','0')):raise RuntimeError('helper foreground quiet capability mismatch')
        return {'hello':result}

    def _request(self,apps,foreground,targets):
        context={**self.proactive_context,'foreground':foreground,'focus_state_path':str(self.focus_state_path)}
        # Context and cancellation remain observable before the first model
        # publication. Actuation additionally needs published live bindings.
        arm_path=os.environ.get('PARP_SERVICE_CONTROL_ARM_PATH','')
        context['control_enabled']=bool(context.get('apps')) and (not arm_path or Path(arm_path).exists())
        if os.environ.get('PARP_SERVICE_UNIFIED_SCHEDULER_RESERVE','0')=='1':context['scheduler_state_path']=str(self.scheduler_state_path)
        if self.window_intent is not None:
            with self.control_lock:
                state=json.loads(self.focus_state_path.read_text())
                context['operation_epoch']=state.get('operation_epoch',0)

        io_health=dict(required=False,ready=None,validated_live_scopes=0)
        if self.io_accounting is not None:
            context,io_health=self.io_accounting.context(context)
            io_health['required']=True
        if self.buffered_reads is not None:
            context['buffered_reads']=self.buffered_reads.snapshot()
        self.helper.stdin.write(json.dumps({'apps':apps,'foreground':foreground,'low_targets':targets,'proactive_context':context})+'\n');self.helper.stdin.flush()
        line=self.helper.stdout.readline()
        if not line:raise RuntimeError('resident probe exited')
        result=json.loads(line)
        result['io_accounting']=io_health
        return result

    def _drain(self):
        if self.pending is None or not self.pending.done():return
        try:
            result=self.pending.result()
            self.error=result.get('error','')
            self.schedule_budget.update(result)
            if 'activities' in result:
                self.activities=result['activities'];self.observation_sequence=int(result.get('sequence',0))
            if os.environ.get('PARP42_FREEZE_LEARNING')!='1':
                for label in result.get('labels',[]):self.learn(label)
            self.audit.write(json.dumps({'kind':'observation','timestamp_ns':time.monotonic_ns(),**result})+'\n')
        except Exception as exc:self.error=str(exc)
        self.pending=None

    def learn(self,label):
        if label.get('demand_censored'):return
        app=label['app'];state=self.learned.setdefault(app,{})
        for kind,item in label['types'].items():
            count=sum(1 for p in [item] if p.get('valid_bytes',0)>0)
            coverage=float(item.get('coverage',0))
            # Missing/swapped/migrated frames are censored; low coverage cannot
            # become evidence that those old pages will not be needed.
            if not count or coverage<.85 or item.get('samples',0)<32:continue
            previous=state.setdefault(kind,{'ratio':1.,'episodes':0,'coverage':coverage})
            ratio=float(item['ratio'])
            previous['ratio']=ratio if previous['episodes']==0 else .6*previous['ratio']+.4*ratio
            previous['coverage']=.6*previous['coverage']+.4*coverage
            previous['episodes']+=1
        self.state_path.parent.mkdir(parents=True,exist_ok=True)
        temporary=self.state_path.with_suffix('.tmp');temporary.write_text(json.dumps(saved_reuse_state(self.learned),indent=2));temporary.replace(self.state_path)

    def publish_scheduler_demand(self,result):
        if os.environ.get('PARP_SERVICE_UNIFIED_SCHEDULER_RESERVE','0')!='1' or result.get('error'):return
        now=time.monotonic_ns();need=int(result.get('admission_need_bytes',0))
        if now-self.scheduler_published_ns<100_000_000 and not (self.scheduler_previous_need>0 and need==0):return
        keys=['observed_ns','foreground','parent','parent_identity','admission_need_bytes','budget_bytes','margin_bytes','waiting_ids','waiting_reservation','running_reservations']
        if not all(k in result for k in keys):return
        state={k:result[k] for k in keys}
        with self.control_lock:
            tmp=self.scheduler_state_path.with_suffix('.scheduler.tmp');tmp.write_text(json.dumps(state));os.replace(tmp,self.scheduler_state_path)
            self.scheduler_published_ns=now;self.scheduler_previous_need=need

    def scheduling_reserve(self,parent,identity,foreground):
        return self.schedule_budget.get(parent,identity,foreground)

    def observe(self,samples):
        samples=list(samples)
        if not samples:
            self.low_targets={}
        self._drain()
        now=time.monotonic()
        if self.closed or self.pending is not None or now-self.last<2. or self.helper is None:return
        apps=defaultdict(list)
        for sample in samples:
            key=str(getattr(sample,'app_id',''))
            pid=int(getattr(getattr(sample,'identity',None),'pid',0))
            if key and pid>0:apps[key].append(pid)
        self.pending=self.executor.submit(self._request,dict(apps),self.foreground,dict(self.low_targets))
        self.last=now

    def observe_focus_event(self,app):
        self.event_driven_focus=True
        self.focus(app)

    def observe_window_context(self,context):
        if self.window_intent is None:return
        from core.window_intent import publish_transition
        with self.control_lock:
            transition=self.window_intent.update(context)
            if transition is not None and context.get('app')==self.foreground:
                publish_transition(self,transition)

    def observe_input_event(self,event):
        pressed=event.get('state')=='pressed' and event.get('event_type') in ('KEY_PRESS','MOUSE_CLICK')
        native=event.get('source')=='libinput' and (pressed or event.get('event_type')=='MOUSE_SCROLL' or event.get('dragging'))
        if native and int(os.environ.get('PARP_SERVICE_FOREGROUND_QUIET_MS','0')) and event.get('foreground_context',{}).get('app')==self.foreground:
            now=time.monotonic_ns()
            if now-getattr(self,'input_published_ns',0)>=100_000_000:
                with self.control_lock:
                    try:
                        state=json.loads(self.focus_state_path.read_text())
                        if state.get('foreground')==self.foreground:
                            state['last_input_ns']=now
                            tmp=self.focus_state_path.with_suffix('.input.tmp');tmp.write_text(json.dumps(state));os.replace(tmp,self.focus_state_path)
                            self.input_published_ns=now
                    except (OSError,ValueError):pass

        if event.get('source')=='libinput' and event.get('event_type')=='KEY_PRESS' and event.get('key_code')==1 and event.get('state')=='pressed':
            self.audit.write(json.dumps(dict(kind='intent_cancel_candidate',timestamp_ns=time.monotonic_ns(),source_event_ns=event.get('monotonic_ns'),source_seq=event.get('source_seq'),foreground=event.get('foreground_context',{}).get('app'),window_id=event.get('foreground_context',{}).get('window_id')))+'\n')
        if not self.intent_profiles:return False
        from core.intent_fence import publish_cancel
        return publish_cancel(self,event)

    def focus(self,app):
        with self.control_lock:self._focus_locked(app)

    def _focus_locked(self,app):
        if self.foreground!=app:
            self.foreground=app;self.foreground_started=time.monotonic();self.last=0.
            tmp=self.focus_state_path.with_suffix('.tmp')
            stamp=time.monotonic()
            for record in self.cancelled_visits:
                if not record['end_ns']:record['end_ns']=int(stamp*1e9)
            tmp.write_text(json.dumps(dict(foreground=app,stamp=stamp,cancelled_visits=self.cancelled_visits)))
            os.replace(tmp,self.focus_state_path)

    def estimate(self,app,kind):
        row=self.learned.get(app,{}).get(kind,{})
        confidence=min(.95,float(row.get('episodes',0))/3.*.95)*float(row.get('coverage',0))
        return conservative_reuse(float(row.get('ratio',1.)),confidence),confidence

    @profiled('fusion_and_publication')
    def fuse(self,entries,rows,binding_paths,current_app):
        self._drain()
        if not getattr(self,'event_driven_focus',False):self.focus(current_app)
        by_id={int(a.app_id):a for a in self.scope.apps}
        by_vocab={str(a.vocab_name):int(a.app_id) for a in self.scope.apps}
        model_rows={by_vocab.get(str(r.get('app'))):r for r in rows if isinstance(r,dict)}
        stats={};paths=defaultdict(list)
        for _,(appid,key,path) in binding_paths.items():
            try:stat=resident_view({line.split()[0]:int(line.split()[1]) for line in (Path(path)/'memory.stat').read_text().splitlines()})
            except (OSError,ValueError,IndexError):continue
            paths[appid].append((Path(path),stat))
            total=stats.setdefault(appid,{'file':0,'anon':0})
            for kind in ('file','anon'):total[kind]+=stat.get(kind,0)
        result=[];details=[];targets=defaultdict(int)
        for appid,old_score,rank,flags in entries:
            app=by_id.get(appid)
            if app is None:continue
            key=str(app.app_key);resident=stats.get(appid,{'file':0,'anon':0});rss=sum(resident.values())
            fg=bool(flags&1)
            temporal=1. if fg else temporal_weight(model_rows[appid]['eviction_score']) if appid in model_rows else 1.
            activity=self.activities.get(key,{})
            # Recent sampled accesses provide a separate active-demand floor,
            # including continuous background work with no foreground return.
            active=sum(min(resident[k],float(activity.get('types',{}).get(k,{}).get('active_bytes',0))*1.2) for k in ('file','anon'))
            active_fraction=min(1.,active/max(1,rss))
            type_estimates={};useful=0.
            for kind in ('file','anon'):
                effective,confidence=self.estimate(key,kind)
                # Unobserved resident bytes retain the legacy protection.
                observed=float(activity.get('types',{}).get(kind,{}).get('sample_bytes',0))
                coverage=min(1.,observed/max(1,resident[kind]))
                effective=1.-coverage*(1.-effective)
                if self.force_reuse_one:effective=1.
                useful+=resident[kind]*effective
                type_estimates[kind]={'reuse_effective':effective,'confidence':confidence,'resident_bytes':resident[kind],'sample_coverage':coverage}
            reuse=useful/max(1,rss) if rss else 1.
            # Foreground has no rank floor. Once an interval is observed its
            # protection follows measured active demand, not whole resident RSS.
            first_window=fg and time.monotonic()-getattr(self,'foreground_started',0.)<4.
            if fg and not first_window and activity.get('samples',0)>=32:
                reuse=max(active_fraction,.02)
            protection=fused_score(temporal,reuse,active_fraction)
            result.append((appid,round(protection*Q15),rank,flags))
            predicted_floor=useful if first_window and any(v['confidence']>0 for v in type_estimates.values()) else 0.
            guard=int(min(rss,max(active,predicted_floor)))
            if self.active_low:
                for path,stat in paths[appid]:
                    share=sum(stat.get(k,0) for k in ('file','anon'))/max(1,rss)
                    amount=int(guard*share)
                    node=path
                    while node!=Path('/sys/fs/cgroup') and str(node).startswith('/sys/fs/cgroup/'):
                        targets[str(node)]+=amount;node=node.parent
            details.append({'app':key,'app_id':appid,'foreground':fg,'temporal_weight':temporal,'reuse_effective':reuse,'active_bytes':int(active),'memory_low_bytes':guard,'protection':protection,'score_q15':round(protection*Q15),'bin':min(7,round(protection*Q15)*8>>15),'types':type_estimates})
        context_apps={}
        for item in details:
            key=item['app'];appid=item['app_id']
            context_apps[key]={'paths':[str(p) for p,stat in paths[appid]],'types':item['types'],'temporal_weight':item['temporal_weight']}
        arm_path=os.environ.get('PARP_SERVICE_CONTROL_ARM_PATH','')
        self.proactive_context={'control_enabled':not arm_path or Path(arm_path).exists(),'apps':context_apps,'foreground':self.foreground,'stamp':time.monotonic(),'focus_state_path':str(self.focus_state_path)}
        self.low_targets=dict(targets)
        # Ranks remain a unique metadata permutation; kernel model 402 consumes
        # the explicit scores directly, so legacy floors cannot undo fusion.
        result.sort(key=lambda r:(-r[1],r[0]));result=[(a,s,i+1,f) for i,(a,s,_,f) in enumerate(result)]
        self.audit.write(json.dumps({'kind':'fusion','timestamp_ns':time.monotonic_ns(),'model_version':402,'foreground':current_app,'reuse_one':self.force_reuse_one,'probe_error':self.error,'apps':details})+'\n')
        return result

    def close(self):
        if self.closed:return
        self.closed=True
        if self.pending is not None:
            try:self.pending.result(timeout=10)
            except Exception:pass
        if self.helper is not None and self.helper.poll() is None:
            try:self.helper.stdin.write('{"command":"quit"}\n');self.helper.stdin.flush();self.helper.wait(timeout=10)
            except Exception:self.helper.terminate()
        self.executor.shutdown(wait=False,cancel_futures=True);self.audit.close();self.helper_errors.close()
