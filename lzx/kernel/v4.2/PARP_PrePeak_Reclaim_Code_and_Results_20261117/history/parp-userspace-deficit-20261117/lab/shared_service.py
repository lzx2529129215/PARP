"""Fixed high and fine reclaim: one entry for controls, calibration and formal."""
from dataclasses import replace,asdict
from pathlib import Path
import sys

HERE=Path(__file__).resolve().parent;ROOT=HERE.parent
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'controller'))
from online_service import Service as OriginalService,ForegroundGate,calibrated_guard,GROUPS
from safety_store import SafetyPoint
from signals import Rules
from replay_forecast import OnlineReplay
from coordinator import Coordinator
from reference import load,measured_reference,sha
from execution_contract import build_contract,digest
from total_profiles import load_qualified
from demand_reference import reference_conditions

SOURCE=ROOT.parent/'parp-measured-validation-20261113'


def source_path(value):
    """Resolve recorded guest paths to the preserved source on Windows or Linux."""
    marker='parp-measured-validation-20261113/'
    normalized=str(value).replace('\\','/')
    if marker not in normalized:raise ValueError('proposal evidence outside the preserved source experiment')
    path=(SOURCE/normalized.split(marker,1)[1]).resolve()
    if not path.is_relative_to(SOURCE.resolve()):raise ValueError('proposal evidence escaped its source')
    return path


def prepare_inputs(bundle):
    purpose=bundle['purpose'];scope=bundle['scope']
    if purpose not in ('calibration','formal'):raise ValueError('explicit purpose required')
    ref=measured_reference(bundle['reference_path']);live=bundle['live_context']
    reference_condition=reference_conditions(ref,live,bundle.get('reference_mode','same_limit'),
                                             bundle.get('minimum_initial_gap_bytes',0))
    # Old 13-Nov measurements are proposals only. They are not a new safety
    # certificate and they never exclude an app because of a later 16-MiB table.
    bank_path=SOURCE/'results/APPROVED_SAFETY.json';bank=load(bank_path)
    if bank.get('status')!='COMPLETE':raise ValueError('preserved proposal bank incomplete')
    if sha(SOURCE/'results/CALIBRATION.json')!=bank['calibration_sha256']:
        raise ValueError('preserved proposal bank source changed')
    points={app:{}for app in GROUPS};cal_trials={app:{}for app in GROUPS};baselines={app:{}for app in GROUPS}
    recipes={}
    for app in GROUPS:
        recipes[app]={}
        for method,item in bank['selected'].get(app,{}).items():
            if item is None:continue
            point=SafetyPoint(**item)
            bp=source_path(point.baseline_path);tp=source_path(point.trial_path)
            if sha(bp)!=point.baseline_sha256 or sha(tp)!=point.trial_sha256:raise ValueError('proposal evidence changed')
            row=load(tp)
            actual=max((e.get('used_bytes',0)for e in row.get('calibration_events',[])if e['event']=='terminated'),default=0)
            if not actual:continue
            point=replace(point,baseline_path=str(bp),trial_path=str(tp),requested_bytes=min(point.requested_bytes,actual))
            points[app][method]=point;cal_trials[app][method]=row;baselines[app][method]=load(bp)
            recipes[app][method]={k:v for k,v in asdict(point).items()if k not in ('baseline_path','trial_path')}
    if not any(points.values()):raise ValueError('no preserved candidate plan')
    guard_trial=ref['trial']
    live=bundle['live_context']
    for name in ('scenario','kernel','peak_mode','workload_version','zswap'):
        if guard_trial.get(name)!=live[name]:raise ValueError('reference workload/conditions differ at '+name)
    if guard_trial.get('limit_set_before_application_load')is not True:raise ValueError('reference had different startup limits')
    guards={app:calibrated_guard(guard_trial,app)for app in GROUPS};fg=ForegroundGate(guard_trial)
    proposal_sha=sha(bank_path)
    contract=build_contract(live,bundle['collector_contract'],proposal_sha,guards,fg.limits,ref['trial_sha256'],recipes,scope,
                            reference_condition=reference_condition)
    qualification=None;qualification_sha=None
    if purpose=='formal':
        qualification,qualification_sha=load_qualified(ROOT/'results'/('TOTAL_QUALIFICATION-'+live['scenario']+'.json'),
            contract,scope,root=ROOT)
    durations=[(w['end_ns']-w['start_ns'])/1e9 for methods in cal_trials.values()for row in methods.values()
               for w in row.get('calibration_writes',[])]
    return dict(purpose=purpose,scope=scope,reference=ref,points=points,cal_trials=cal_trials,baselines=baselines,
        guards=guards,foreground=fg,retention_lead_s=max(1.,max(durations,default=0)+.5),
        contract=contract,recipes=recipes,proposal_sha=proposal_sha,reference_condition=reference_condition,
        qualification=qualification,qualification_sha=qualification_sha)


class Service(OriginalService):
    def __init__(self,bundle,initial,base,directory,tag,quota,enabled,backend_factory=None):
        from suite import Measured
        prepared=bundle.get('_prepared')or prepare_inputs(bundle)
        if prepared['purpose']!=bundle['purpose']or prepared['scope']!=bundle['scope']:raise ValueError('prepared purpose/scope changed')
        self.reference=prepared['reference'];self.points=prepared['points'];self.cal_trials=prepared['cal_trials']
        self.baselines=prepared['baselines'];self.guards=prepared['guards'];self.fg=prepared['foreground']
        self.retention_lead_s=prepared['retention_lead_s']
        self.contract=build_contract(bundle['live_context'],bundle['collector_contract'],prepared['proposal_sha'],
            self.guards,self.fg.limits,self.reference['trial_sha256'],prepared['recipes'],bundle['scope'],
            reference_condition=reference_conditions(self.reference,bundle['live_context'],
                bundle.get('reference_mode','same_limit'),bundle.get('minimum_initial_gap_bytes',0)))
        if self.contract!=prepared['contract']:raise ValueError('actual conditions changed after preparation')
        if quota!=self.contract['live_context']['memory_limit_bytes']:raise ValueError('actual quota differs')
        self.purpose=bundle['purpose'];self.scope=bundle['scope'];self.qualification=prepared['qualification']
        self.qualification_sha=prepared['qualification_sha']
        if self.purpose=='formal'and self.qualification_sha is None:raise ValueError('formal qualification missing')
        self.bundle=bundle;self.enabled=enabled;self.initial=initial;self.base=base;self.directory=directory
        self.use_live_clock=backend_factory is None;self.clock_origin=None
        self.tag=tag;self.quota=quota;self.expires=32.;self.rules=Rules()
        self.source=OnlineReplay(self.reference['occupancy'],self.reference['signals'])
        self.last_update=-1;self.decisions=[];self.forecasts=[];self.policy_trace=[]
        self.backends={app:(backend_factory or Measured)(base/leaf,directory,app)for app,leaf in GROUPS.items()}
        self.coordinator=None;self.closed=False
        self.forecast=self.source.predict(0,max(0,quota-initial['current']))
        # All modes use the same fixed-high/fine-reclaim build and executor.
        candidates,plans=self.build(0)
        self.coordinator=Coordinator(candidates,self.backends,tag,self.expires,guard=self.guards,
            plans=plans,deadline_s=self.prepeak_state.finish_by_s,parallel=True,foreground_feedback=self.fg,
            clock=self.execution_time if self.use_live_clock else None)
        self.coordinator._accept(plans,0)

    def report(self):
        return dict(**super().report(),execution_contract=self.contract,execution_contract_sha256=digest(self.contract),
            service_purpose=self.purpose,acceptance_scope=self.scope,qualification=self.qualification,
            qualification_sha256=self.qualification_sha,
            proposal_state='13-Nov allowance proposals; fixed-high/fine-reclaim needs fresh same-service qualification',
            trained_predictors_called=False)
