"""Separate unconstrained demand measurement from equally constrained test arms."""
import math

MIB=2**20


def require_unclipped(trial):
    for name in ('high','max','oom','oom_kill'):
        delta=trial['final']['events'].get(name,0)-trial['initial']['events'].get(name,0)
        if delta or trial['initial']['events'].get(name,0):
            raise ValueError('demand reference was constrained: '+name+'='+str(delta))


def growth_before_revisit(reference):
    rows=[r for r in reference['occupancy'] if r['time_s']<22.]
    if not rows:raise ValueError('demand occupancy samples missing')
    return max(0,max(r['total']for r in rows)-reference['trial']['initial']['current'])


def reference_conditions(reference,live,mode='same_limit',minimum_gap_bytes=0):
    trial=reference['trial']
    for key in ('scenario','kernel','peak_mode','workload_version','zswap'):
        if trial.get(key)!=live[key]:raise ValueError('reference context differs at '+key)
    reference_limit=trial['memory_limit_bytes'];test_limit=live['memory_limit_bytes']
    if mode=='unclipped_demand':
        if reference_limit<=test_limit:raise ValueError('unclipped demand requires a larger explicit reference limit')
        require_unclipped(trial)
    elif mode!='same_limit' or reference_limit!=test_limit:
        raise ValueError('reference context differs at memory_limit_bytes')
    return dict(mode=mode,reference_limit_bytes=reference_limit,test_limit_bytes=test_limit,
                minimum_initial_gap_bytes=minimum_gap_bytes,
                demand_growth_bytes=growth_before_revisit(reference))


def choose_limit(reference,target_gap_bytes):
    require_unclipped(reference['trial'])
    if target_gap_bytes<=0:raise ValueError('positive target deficit required')
    peak=reference['trial']['initial']['current']+growth_before_revisit(reference)
    limit=math.floor((peak-target_gap_bytes)/MIB)*MIB
    if limit<=reference['trial']['initial']['current']:
        raise ValueError('target would exhaust startup space; no automatic stress escalation')
    return limit


def admission(reference,initial,limit_bytes,minimum_gap_bytes):
    growth=growth_before_revisit(reference)
    free=limit_bytes-initial['current'];gap=max(0,growth-free)
    result=dict(demand_growth_bytes=growth,initial_free_bytes=free,initial_gap_bytes=gap,
                required_minimum_gap_bytes=minimum_gap_bytes,passed=gap>=minimum_gap_bytes)
    if not result['passed']:
        raise ValueError('insufficient initial deficit; workload not admitted: '+str(result))
    return result
