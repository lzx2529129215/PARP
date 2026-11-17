"""Actual conditions shared by controls, calibration and formal trials."""
from dataclasses import asdict
import hashlib
import json
from pathlib import Path

ROOT=Path(__file__).resolve().parent
SCHEMA='parp-total-fault-unclipped-demand-v4'


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()


def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def sampling_contract(*,reference=False,root=ROOT):
    return dict(schema=1,source_sha256=sha(Path(root)/'lab/root_guard.py'),
        mode='reference_clear_refs' if reference else 'passive_smaps',clear_refs=reference,interval_s=1.0)


def verify_sampling(actual,*,reference=False,root=ROOT):
    expected=sampling_contract(reference=reference,root=root)
    if actual!=expected:raise ValueError('actual collector/clear_refs/interval mismatch')
    return expected


def build_contract(live_context,collector,proposal_sha,guards,foreground_limits,reference_sha,recipes,scope,*,root=ROOT,reference_condition=None):
    if scope not in ('per_app','all_apps'):raise ValueError('explicit total-fault scope required')
    required={'scenario','kernel','memory_limit_bytes','peak_mode','workload_version','zswap',
        'old_hot_s','first_growth_s','new_hot_s','old_document_revisit_s'}
    if set(live_context)!=required:raise ValueError('complete live conditions required')
    limit=live_context['memory_limit_bytes']
    if type(limit)is not int or limit<=0:raise ValueError('finite positive shared memory.max required')
    if live_context['zswap']!='Y':raise ValueError('compression experiment requires zswap=Y')
    root=Path(root);verify_sampling(collector,root=root)
    files=[p for p in (root/'controller').glob('*.py') if not p.name.startswith('test_')]
    files.extend(root/p for p in ('execution_contract.py','demand_reference.py','total_faults.py','total_profiles.py','inputs_bundle.py',
        'lab/shared_service.py','lab/suite.py','lab/online_service.py','lab/root_guard.py','lab/telemetry.py',
        'lab/reference.py','lab/task_faults.py','lab/harness_contracts.py','lab/native_apps.py',
        'lab/foreground_probe.py','lab/firefox-install.json'))
    from signals import Rules
    from prepeak_timing import PROMINENCE_FRACTION,QUIET_S,UPDATE_S
    value=dict(schema=SCHEMA,live_context=live_context,sampling=collector,
        source_sha256={p.relative_to(root).as_posix():sha(p) for p in files},
        proposal_bank_sha256=proposal_sha,forecast_reference_sha256=reference_sha,recipes=recipes,
        reference_conditions=reference_condition,
        monitored_apps=['gimp','libreoffice','firefox','krita'],rules=asdict(Rules()),
        guard_profiles={app:asdict(g) for app,g in guards.items()},foreground_limits=foreground_limits,
        execution=dict(entrypoint='shared_service.Service',forecast_update_s=.5,plan_expiry_s=32.,parallel=True,
            controller='fixed sequence high plus feedback/gap/retention fine reclaim',
            high='full-sequence WSS peak with calibrated occupancy and tested high allowance floors; immutable per plan',
            reclaim='finish before curve-derived growth; whole-gap measured duration; immutable per-peak cutoff and live feedback',
            timing=dict(growth_prominence_fraction=PROMINENCE_FRACTION,quiet_s=QUIET_S,update_s=UPDATE_S,
                duration='slowest credited calibration batch and full request span, with feedback/batch gaps',
                cutoff='can move earlier only within the same protected growth period',
                completion='admission uses live clock and measured batch bound; overruns are explicit failures'),
            budget='original cumulative ceilings; no refill on resume/update',
            donors='same complete proposal set in calibration and formal; no post-calibration donor filtering'),
        criterion=dict(scope=scope,relative_fault_limit=.10,runs_per_group=3,
            aggregation='arithmetic mean of complete-workload total pgfault; operations diagnostic only',
            counter='task_stat_self_and_reaped_descendants',zero_baseline='absolute increment; no invented allowance'))
    return json.loads(json.dumps(value,sort_keys=True,allow_nan=False))
