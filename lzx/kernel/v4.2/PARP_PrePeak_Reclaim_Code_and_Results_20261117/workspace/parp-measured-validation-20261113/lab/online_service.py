"""Evidence-gated online prediction -> numeric policy -> native multi-app execution."""
from dataclasses import asdict,replace
import json
from pathlib import Path
import statistics
import sys
import time

HERE=Path(__file__).resolve().parent;ROOT=HERE.parent
sys.path.insert(0,str(ROOT/'controller'))
from adapters import ColdReply,attach_external_outputs
from coordinator import Coordinator
from executor import Guard
from plan import Candidate,Calibration,Trial,Phase,action_plan
from signals import classify,Rules
from replay_forecast import OnlineReplay
from safety_store import SafetyPoint
from reference import measured_reference,load,sha

GROUPS={'gimp':'background','libreoffice':'libreoffice','firefox':'firefox'}
SCORES={'gimp':1000,'libreoffice':5000,'firefox':10000}


def percentile(values,p=.95):
    values=sorted(values)
    return values[min(len(values)-1,int((len(values)-1)*p))] if values else 0


class ForegroundGate:
    def __init__(self,reference):
        self.limits={}
        rows=reference['responses']
        for kind in {r.get('kind') for r in rows}:
            values=[r['latency_ms'] for r in rows if r.get('kind')==kind and 'latency_ms'in r]
            self.limits[kind]=max(40,percentile(values)*1.10+20)
        self.last_sent=None;self.bad_count=0;self.bad=False;self.events=[]
    def update(self,frontdir,now):
        path=frontdir/'latest-response.json'
        if not path.exists():return
        try:row=load(path)
        except (OSError,ValueError):return
        if row['sent_ns']==self.last_sent:return
        self.last_sent=row['sent_ns']
        bad=row.get('timeout') or (row.get('latency_ms') or 0)>self.limits.get(row['kind'],float('inf'))
        self.bad_count=self.bad_count+1 if bad else 0
        self.bad=self.bad_count>=2
        self.events.append(dict(time_s=now,foreground_bad=self.bad,latency_ms=row.get('latency_ms'),
                                baseline_threshold_ms=self.limits.get(row['kind']),kind=row['kind']))
    def __call__(self,now):return self.bad


def calibrated_guard(trial,app):
    rows=trial['root_telemetry'];refault=[];psi=[];previous=None
    for row in rows:
        item=row['apps'][app];stats=item['stats'];r=sum(stats.get(k,0) for k in ('workingset_refault_anon','workingset_refault_file'))
        some=next(line for line in item['memory_pressure'].splitlines() if line.startswith('some '))
        total=int(dict(v.split('=') for v in some.split()[1:])['total'])/1e6
        if previous is not None:
            t0,r0,p0=previous;dt=max(.001,row['window_end']-t0)
            refault.append(max(0,r-r0)/dt);psi.append(max(0,total-p0)/dt)
        previous=row['window_end'],r,total
    mild=max(128,int(max(refault,default=0)*1.10)+1)
    severe=max(2048,mild*2)
    psi_mild=min(.85,max(.10,percentile(psi)+.05));psi_severe=min(.99,max(.50,max(psi,default=0)+.10,psi_mild+.05))
    return Guard(refault_mild=mild,refault_severe=severe,psi_mild=psi_mild,psi_severe=psi_severe)


class Service:
    def __init__(self,bundle,initial,base,directory,tag,quota,enabled,backend_factory=None):
        # Delayed import avoids a circular dependency at native helper startup.
        from suite import Measured
        self.bundle=bundle;self.reference=measured_reference(bundle['reference_path'])
        self.safety=load(ROOT/'results/APPROVED_SAFETY.json')
        if self.safety['status']!='COMPLETE':raise ValueError('safety calibration not complete')
        if self.safety['calibration_sha256']!=sha(ROOT/'results/CALIBRATION.json'):
            raise ValueError('safety evidence changed since approval')
        self.enabled=enabled;self.initial=initial;self.base=base;self.directory=directory
        self.tag=tag;self.quota=quota;self.expires=32.0;self.rules=Rules()
        self.source=OnlineReplay(self.reference['occupancy'],self.reference['signals'])
        guard_trial=load(bundle['guard_baseline_path']) if bundle.get('guard_baseline_path') else self.reference['trial']
        self.fg=ForegroundGate(guard_trial);self.last_update=-1;self.decisions=[];self.forecasts=[];self.policy_trace=[]
        self.points={};self.cal_trials={};self.baselines={}
        for app in GROUPS:
            self.points[app]={};self.cal_trials[app]={};self.baselines[app]={}
            for method,item in self.safety['selected'].get(app,{}).items():
                if item is None:continue
                point=SafetyPoint(**item)
                if point.verify()['within_trial_limit'] is not True:raise ValueError('unvalidated point')
                measured_trial=load(point.trial_path)
                actual_attempted=max((e.get('used_bytes',0) for e in measured_trial.get('calibration_events',[]) if e['event']=='terminated'),default=0)
                if not actual_attempted:continue
                # A protection pause may prevent spending the nominal level.
                # Never allow a formal plan to spend an untested remainder.
                point=replace(point,requested_bytes=min(point.requested_bytes,actual_attempted))
                self.points[app][method]=point
                self.cal_trials[app][method]=measured_trial
                self.baselines[app][method]=load(point.baseline_path)
        durations=[(w['end_ns']-w['start_ns'])/1e9 for methods in self.cal_trials.values()
                   for trial in methods.values() for w in trial.get('calibration_writes',[])]
        self.retention_lead_s=max(1.0,max(durations,default=0)+.5)
        backend_factory=backend_factory or Measured
        self.backends={app:backend_factory(base/leaf,directory,app) for app,leaf in GROUPS.items()}
        self.guards={app:calibrated_guard(guard_trial,app) for app in GROUPS}
        self.coordinator=None;self.closed=False
        self.forecast=self.source.predict(0,max(0,quota-initial['current']))
        candidates,plans=self.build(0)
        self.coordinator=Coordinator(candidates,self.backends,tag,self.expires,guard=self.guards,
            plans=plans,deadline_s=self.forecast.deadline_s,parallel=True,foreground_feedback=self.fg)
        self.coordinator._accept(plans,0)

    def selected_row(self,trial,app,at):
        origin=trial['start_monotonic_s']
        rows=[trial['before_peak']['groups'][app]]
        rows.extend(r['apps'][app] for r in trial['root_telemetry']
                    if at-.25<=r['window_end']-origin<=at+self.retention_lead_s+1.0)
        return max(rows,key=lambda r:r['current'])

    def cal_for(self,app,mode,at):
        point=self.points[app].get(mode)
        if point is None:
            row=self.initial['groups'][app]
            return Calibration('no-approved-'+app,Trial(row['rss_sum_bytes'],row['current'],0,0),(),.10,None,None,0,
                               measurement_id='baseline-only-no-reclaim',method='observe')
        baseline=self.selected_row(self.baselines[app][mode],app,at)
        trial=self.selected_row(self.cal_trials[app][mode],app,at)
        before=Trial(baseline['rss_sum_bytes'],baseline['current'],point.baseline_pgfault,point.baseline_task_ms)
        after=Trial(trial['rss_sum_bytes'],trial['current'],point.current_pgfault,point.current_task_ms)
        eligible=after.rss_bytes<before.rss_bytes and after.cgroup_bytes<=before.cgroup_bytes
        return Calibration(app+':'+mode+':'+str(round(at,3)),before,(after,) if eligible else (),.10,None,None,0,
                           measurement_id=point.trial_sha256,method=mode)

    def build(self,now):
        candidates=[]
        for app in GROUPS:
            raw=self.reference['signals'][app]
            phases=[]
            for index,window in enumerate(raw):
                if window['end_s']<=now or window['start_s']>=self.expires:continue
                start=max(now,window['start_s']);end=min(self.expires,window['end_s'])
                future=[p for p in raw[index:] if max(now,p['start_s'])<start+self.rules.lookahead_s]
                decision=classify([p['wss_bytes'] for p in future],[p['pgfault'] for p in future],rules=self.rules)
                cal=self.cal_for(app,decision['mode'],start)
                if window['start_s']<=now<window['end_s']:
                    selected=cal.select()
                    self.policy_trace.append(dict(time_s=now,app=app,wss_bytes=[p['wss_bytes'] for p in future],
                        pgfault=[p['pgfault'] for p in future],numeric_decision=decision,
                        measured_calibration_id=cal.measurement_id,approved_method=cal.method,
                        retained_rss_bytes=selected.rss_bytes,retained_cgroup_bytes=selected.cgroup_bytes))
                phases.append(Phase(start,end,cal,'unknown',window['wss_bytes'],window['pgfault']))
            # No made-up WSS tail: unknown/unavailable reference ends the plan's actions.
            if not phases:continue
            points=list(self.points[app].values())
            cold=max((min(p.requested_bytes,p.observed_net_release_bytes) for p in points),default=0)
            budget=max((p.requested_bytes for p in points),default=0)
            # Close the preset cold interval before old-document revisit.
            valid_until=22.0 if self.reference['trial']['scenario']=='revisit' else self.expires
            candidates.append(Candidate(app,SCORES[app],self.initial['groups'][app]['current'],tuple(phases),
                cold_cap_bytes=cold,cold_valid_until_s=valid_until,attempt_cap_bytes=budget,policy_rules=self.rules,
                method_attempt_caps=tuple((m,self.points[app][m].requested_bytes if m in self.points[app] else 0)
                                          for m in ('high','reclaim'))))
        if len(candidates)!=3:raise ValueError('measured reference exhausted')
        entries=((1,1000,4,0),(2,5000,3,0),(3,10000,2,0),(4,32767,1,1))
        replies=tuple(ColdReply(c.app,c.cold_cap_bytes,0,c.cold_valid_until_s,'script preset cold objects + measured safe allowance') for c in candidates)
        candidates,excluded=attach_external_outputs(candidates,entries,{1:'gimp',2:'libreoffice',3:'firefox',4:'krita'},replies,now)
        plans=[]
        for candidate in candidates:
            old=self.coordinator.executors[candidate.app].plan if self.coordinator is not None else None
            budget=old.budget_bytes if old is not None else candidate.attempt_cap_bytes
            goal=old.goal_bytes if old is not None else 0
            plans.append(action_plan(candidate,goal or 0,self.tag+':'+candidate.app,self.expires,
                                     attempt_budget=budget or 0,revision=old.revision+1 if old else 0))
        return candidates,plans

    def tick(self,now,frontdir):
        if self.closed:return
        self.fg.update(frontdir,now)
        if now-self.last_update>=.5:
            try:
                self.forecast=self.source.predict(now,max(0,self.quota-int((self.base/'memory.current').read_text())))
                candidates,plans=self.build(now)
            except ValueError as exc:
                if 'exhausted' in str(exc):self.close(now);return
                raise
            self.coordinator.update_forecast(candidates,plans,self.forecast.deadline_s,now)
            self.last_update=now
            active={app:next((a for a in p.actions if a.start_s<=now<a.end_s),None)
                    for app,p in ((p.app,p) for p in plans)}
            self.decisions.append(dict(time_s=now,required_bytes=self.forecast.required_bytes,
                deadline_s=self.forecast.deadline_s,actions={app:asdict(a) if a else None for app,a in active.items()}))
        if self.enabled:
            future_growth=max(sum(next(v for t,v in reversed(curve) if t<=at) for curve in self.forecast.curves.values())
                              for at in {t for curve in self.forecast.curves.values() for t,_ in curve})
            self.coordinator.tick(now,lambda:max(0,future_growth-(self.quota-int((self.base/'memory.current').read_text()))))

    def close(self,now):
        if not self.closed:
            self.coordinator.close(now);self.closed=True

    def report(self):
        return dict(online_forecasts=self.source.calls,numeric_decisions=self.decisions,
            service={app:dict(events=e.events,attempt_budget=e.budget_ceiling,attempted_bytes=e.used,
                used_by_method=e.used_by_method,observed_drop_bytes=e.observed_release)
                for app,e in self.coordinator.executors.items()},
            coordinator_events=self.coordinator.events,service_errors=self.coordinator.errors,
            writes={app:backend.writes for app,backend in self.backends.items()},
            runtime_guard_configuration={app:asdict(guard) for app,guard in self.guards.items()},
            foreground_guard=self.fg.events,real_wss_source_sha256=self.reference['trial_sha256'],
            numeric_policy_inputs=self.policy_trace,high_retention_lead_s=self.retention_lead_s)
