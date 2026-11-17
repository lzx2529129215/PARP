"""Only full-workload totals decide acceptance; phase counts are diagnostic."""
import statistics

APPS=('gimp','libreoffice','firefox','krita')
COUNTER='task_stat_self_and_reaped_descendants'


def validate_trial(row):
    if row.get('status')!='COMPLETE' or row.get('cleanup',{}).get('complete')is not True:
        raise ValueError('complete, cleaned trial required')
    if row.get('fault_counter_source')!=COUNTER:raise ValueError('task counters required')
    if row.get('service_errors') or row.get('telemetry_error') or row.get('schedule_late_operations'):
        raise ValueError('invalid execution cannot qualify')
    if set(row.get('total_faults',{}))!=set(APPS):raise ValueError('complete four-app total counters required')
    if row.get('total_fault_interval')!='before_go_to_after_all_work_completed':
        raise ValueError('whole workload, including inter-operation intervals, required')
    for value in row['total_faults'].values():
        for key in ('pgfault','pgmajfault'):
            if type(value[key])is not int or value[key]<0:raise ValueError('invalid total counter')
        if value['pgmajfault']>value['pgfault']:raise ValueError('major faults exceed total faults')
    expected=row['execution_contract']['live_context']
    for key in ('scenario','kernel','peak_mode','workload_version','zswap'):
        if row.get(key)!=expected[key]:raise ValueError('actual '+key+' differs')
    if row.get('memory_limit_bytes')!=expected['memory_limit_bytes']:
        raise ValueError('actual memory.max differs')
    if row.get('limit_set_before_application_load')is not True:raise ValueError('limit was not applied before app startup')
    if row.get('total_fault_counter_source')!='task_stat_self_and_reaped_descendants':raise ValueError('complete-run task counters required')
    if row.get('runtime_guard_configuration')!=row['execution_contract']['guard_profiles']:
        raise ValueError('actual runtime protection differs')
    samples=row.get('root_telemetry',[])
    if not samples:raise ValueError('actual collector evidence missing')
    for sample in samples:
        if sample.get('cleared_references')is not False or sample.get('sampling_contract')!=row['execution_contract']['sampling']:
            raise ValueError('actual sampling conditions differ')
        if set(sample.get('apps',{}))!=set(APPS):raise ValueError('collector application coverage differs')
        if any(not x.get('valid') or x.get('errors') for x in sample['apps'].values()):
            raise ValueError('incomplete app collection')


def statistic(before,after):
    b=sum(before);a=sum(after)
    return dict(off_values=before,on_values=after,off_mean=statistics.mean(before),on_mean=statistics.mean(after),
        mean_absolute_increment=(a-b)/3,relative_growth=(a-b)/b if b else None,
        within_trial_limit=(a*10<=b*11)if b else (True if a==0 else None),
        off_range=[min(before),max(before)],on_range=[min(after),max(after)],
        off_stdev=statistics.stdev(before),on_stdev=statistics.stdev(after))


def compare_totals(controls,treated,scope):
    if scope not in ('per_app','all_apps'):raise ValueError('explicit total-fault scope required')
    if len(controls)!=3 or len(treated)!=3:raise ValueError('exactly three off and three on trials required')
    rows=controls+treated
    if len({r.get('run_id')for r in rows})!=6 or any(not r.get('run_id')for r in rows):
        raise ValueError('six independent native trials required')
    if any(r.get('enabled')is not False for r in controls)or any(r.get('enabled')is not True for r in treated):
        raise ValueError('on/off evidence differs')
    contract=controls[0]['execution_contract']
    if contract['criterion']['scope']!=scope:raise ValueError('configured acceptance scope differs')
    for row in rows:
        validate_trial(row)
        if row['execution_contract']!=contract:raise ValueError('calibration and formal conditions differ')
        from execution_contract import digest
        if row.get('execution_contract_sha256')!=digest(contract):raise ValueError('condition fingerprint differs')
    # Check equal work without turning phase costs into acceptance thresholds.
    def counts(row):
        result={}
        for op in row['operations']:
            key=(op['app'],op['kind']);result[key]=result.get(key,0)+1
        return result
    if any(counts(r)!=counts(rows[0])for r in rows):raise ValueError('workload operation types/counts differ')
    per_app={app:statistic([r['total_faults'][app]['pgfault']for r in controls],
                         [r['total_faults'][app]['pgfault']for r in treated])for app in APPS}
    combined=statistic([sum(r['total_faults'][a]['pgfault']for a in APPS)for r in controls],
                       [sum(r['total_faults'][a]['pgfault']for a in APPS)for r in treated])
    checks=[combined['within_trial_limit']]if scope=='all_apps'else [x['within_trial_limit']for x in per_app.values()]
    verdict=False if False in checks else None if None in checks else True
    return dict(scope=scope,relative_limit=.10,aggregation='fixed-three full-workload arithmetic means',
        within_trial_limit=verdict,per_app=per_app,all_apps=combined,
        phase_metrics='diagnostic only; never used to veto acceptance',
        major_faults={app:dict(off=[r['total_faults'][app]['pgmajfault']for r in controls],
                              on=[r['total_faults'][app]['pgmajfault']for r in treated])for app in APPS},
        zero_baseline='positive increment remains unresolved without an agreed absolute allowance')
