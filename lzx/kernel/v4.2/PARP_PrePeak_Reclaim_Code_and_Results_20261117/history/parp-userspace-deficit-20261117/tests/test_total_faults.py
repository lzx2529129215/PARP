from copy import deepcopy
from pathlib import Path
import sys
import unittest
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'controller'));sys.path.insert(0,str(ROOT/'lab'))
from execution_contract import digest
from total_faults import compare_totals,APPS,COUNTER


def cohort(scope='per_app'):
    contract=dict(live_context=dict(scenario='revisit',kernel='6.17.13',memory_limit_bytes=3776*2**20,
        peak_mode='concurrent',workload_version='fixture',zswap='Y'),sampling={'clear_refs':False},
        guard_profiles={'gimp':{'refault_mild':128}},criterion={'scope':scope})
    def row(index,on):
        return dict(status='COMPLETE',cleanup={'complete':True},run_id=str(index),enabled=on,
            stage='calibration'if on else 'baseline',**contract['live_context'],
            limit_set_before_application_load=True,fault_counter_source=COUNTER,total_fault_counter_source=COUNTER,
            total_fault_interval='before_go_to_after_all_work_completed',
            total_faults={app:dict(pgfault=1000,pgmajfault=0)for app in APPS},
            operations=[dict(app=app,kind=kind,pgfault=1)for app in APPS for kind in ('new','revisit')],
            execution_contract=deepcopy(contract),execution_contract_sha256=digest(contract),
            runtime_guard_configuration=deepcopy(contract['guard_profiles']),
            root_telemetry=[dict(cleared_references=False,sampling_contract=deepcopy(contract['sampling']),
                apps={app:dict(valid=True,errors=[])for app in APPS})],
            writes={'gimp':[dict(name='memory.reclaim',value=4096)]}if on else {})
    return [row(i,False)for i in range(3)],[row(i+3,True)for i in range(3)]


class TotalFaultTests(unittest.TestCase):
    def test_phase_growth_does_not_veto_passing_application_total(self):
        off,on=cohort()
        for row in on:
            row['operations'][1]['pgfault']=10000
            row['total_faults']['gimp']['pgfault']=1040
        self.assertTrue(compare_totals(off,on,'per_app')['within_trial_limit'])

    def test_complete_interval_not_sum_of_selected_operation_deltas(self):
        off,on=cohort()
        for row in on:row['total_faults']['gimp']['pgfault']=1101
        self.assertFalse(compare_totals(off,on,'per_app')['within_trial_limit'])

    def test_scope_changes_only_the_agreed_acceptance_gate(self):
        for scope,expected in (('per_app',False),('all_apps',True)):
            off,on=cohort(scope)
            for row in on:row['total_faults']['gimp']['pgfault']=1200
            result=compare_totals(off,on,scope)
            self.assertEqual(result['within_trial_limit'],expected)
            self.assertFalse(result['per_app']['gimp']['within_trial_limit'])

    def test_exact_ten_percent_is_accepted(self):
        off,on=cohort()
        for row in on:row['total_faults']['gimp']['pgfault']=1100
        self.assertTrue(compare_totals(off,on,'per_app')['within_trial_limit'])

    def test_zero_baseline_increment_is_not_silently_approved(self):
        off,on=cohort()
        for row in off:row['total_faults']['gimp']['pgfault']=0
        for row in on:row['total_faults']['gimp']['pgfault']=1
        result=compare_totals(off,on,'per_app')
        self.assertIsNone(result['within_trial_limit'])
        self.assertEqual(result['per_app']['gimp']['mean_absolute_increment'],1)

    def test_uses_fixed_three_means_not_most_favorable_control(self):
        off,on=cohort()
        for row,count in zip(off,(100,200,300)):row['total_faults']['gimp']['pgfault']=count
        for row in on:row['total_faults']['gimp']['pgfault']=220
        self.assertTrue(compare_totals(off,on,'per_app')['within_trial_limit'])
        with self.assertRaises(ValueError):compare_totals(off[:2],on,'per_app')

    def test_quota_and_sampler_mismatch_are_rejected(self):
        for mutation in ('quota','sampling','startup','work','guard','error','counter'):
            off,on=cohort()
            if mutation=='quota':on[0]['memory_limit_bytes']+=4096
            if mutation=='sampling':on[0]['root_telemetry'][0]['cleared_references']=True
            if mutation=='startup':on[0]['limit_set_before_application_load']=False
            if mutation=='work':on[0]['operations'].pop()
            if mutation=='guard':on[0]['runtime_guard_configuration']={}
            if mutation=='error':on[0]['service_errors']=['failed']
            if mutation=='counter':on[0]['total_faults']['gimp']['pgfault']=-1
            with self.subTest(mutation=mutation),self.assertRaises(ValueError):compare_totals(off,on,'per_app')


if __name__=='__main__':unittest.main()
