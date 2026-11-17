from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
for p in (ROOT,ROOT/'lab',ROOT/'controller'):sys.path.insert(0,str(p))
import shared_service
from execution_contract import sampling_contract
from reference import load,measured_reference
from run_total import verify_sources
from suite import WORKLOAD_VERSION


class Backend:
    rows={};created=0
    def __init__(self,path,directory,app):self.app=app;self.writes=[];self.high=None;Backend.created+=1
    def usage(self):return self.rows[self.app]['current']
    def rss(self):return self.rows[self.app]['rss_sum_bytes']
    def check(self):pass
    def feedback(self):return 0,0.
    def read_high(self):return self.high
    def write_high(self,value):
        self.writes.append(('high',value));self.high=value
        if value is not None and value<self.usage():
            delta=self.usage()-value;self.rows[self.app]['current']=value
            self.rows[self.app]['rss_sum_bytes']=max(0,self.rss()-delta)
    def read_freeze(self):return 0
    def protections(self):return 0,0
    def maximum(self):return 2**63-1
    def reclaim(self,amount):
        self.writes.append(('reclaim',amount));self.rows[self.app]['current']-=amount
        self.rows[self.app]['rss_sum_bytes']=max(0,self.rss()-amount)


class SameServiceTests(unittest.TestCase):
    def setUp(self):
        source=shared_service.SOURCE
        index=load(source/'results/CALIBRATION.json')
        self.ref=measured_reference(shared_service.source_path(index['references']['revisit']))
        self.ref=deepcopy(self.ref)
        self.initial=deepcopy(self.ref['trial']['initial'])
        self.live=dict(scenario='revisit',kernel='6.17.13',memory_limit_bytes=3776*2**20,
            peak_mode='concurrent',workload_version=WORKLOAD_VERSION+':concurrent',zswap='Y',
            old_hot_s=[2,4],first_growth_s=8.,new_hot_s=[14,18],old_document_revisit_s=22)
        self.ref['trial'].update({k:self.live[k]for k in ('scenario','kernel','memory_limit_bytes','peak_mode','workload_version','zswap')})
        self.ref['trial']['limit_set_before_application_load']=True
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.directory=Path(self.tmp.name)
        self.bundle=dict(aligned=True,purpose='calibration',scope='per_app',reference_path='unit-reference',
            live_context=self.live,collector_contract=sampling_contract(root=ROOT))
        self.reader=patch.object(shared_service,'measured_reference',return_value=self.ref)
        self.reader.start();self.addCleanup(self.reader.stop)

    def service(self,purpose='calibration',bundle=None):
        bundle=deepcopy(bundle or self.bundle);bundle['purpose']=purpose
        Backend.rows=deepcopy(self.initial['groups']);Backend.created=0
        service=shared_service.Service(bundle,deepcopy(self.initial),self.directory,self.directory,'unit',3776*2**20,True,
            backend_factory=Backend)
        self.addCleanup(service.close,31.)
        return service

    def trace(self,service):
        service.coordinator.pool.shutdown(wait=True);service.coordinator.pool=None
        for now in (0.,.2,.4,.6,1.,1.2,1.4,1.6,2.,2.2):
            candidates,plans=service.build(now)
            service.coordinator.update_forecast(candidates,plans,8.,now)
            service.coordinator.tick(now,lambda:300*2**20)
        return deepcopy(dict(writes={a:b.writes for a,b in service.backends.items()},
            budgets={a:e.budget_ceiling for a,e in service.coordinator.executors.items()},
            used={a:e.used for a,e in service.coordinator.executors.items()},
            actions={a:[(x.mode,x.safe_rss_bytes,x.safe_cgroup_bytes)for x in e.plan.actions]
                     for a,e in service.coordinator.executors.items()if e.plan}))

    def test_original_sources_workloads_and_common_entry_are_preserved(self):
        verify_sources()
        self.assertIs(shared_service.Service.build,shared_service.OriginalService.build)
        self.assertIs(shared_service.Service.tick,shared_service.OriginalService.tick)
        self.assertIs(shared_service.Service.cal_for,shared_service.OriginalService.cal_for)

    def test_half_second_rebuilds_preserve_high_and_allocation_revisions(self):
        service=self.service()
        chosen=deepcopy(service.sequence_highs)
        for now in (.5,1.,1.5,2.,8.,14.,20.,22.,24.):
            candidates,plans=service.build(now)
            for candidate,p in zip(candidates,plans):
                self.assertEqual(p.sequence_high,chosen[candidate.app])
                self.assertFalse(any(a.mode=='high'for a in p.actions))
                for a in p.actions:
                    if a.mode=='reclaim':
                        self.assertLessEqual(a.end_s,service.cold_until[p.app]-service.retention_lead_s)
                        self.assertLessEqual(a.end_s,service.forecast.deadline_s-service.retention_lead_s)

    def test_cached_high_does_not_recompute_from_a_shorter_future_tail(self):
        service=self.service();chosen=deepcopy(service.sequence_highs)
        service.reference['signals']['gimp'][-1]['wss_bytes']+=1024*2**20
        _,plans=service.build(.5)
        self.assertEqual(service.sequence_highs,chosen)
        self.assertEqual(plans[0].sequence_high,chosen[plans[0].app])

    def test_service_uses_growth_start_instead_of_later_peak_deadline(self):
        service=self.service()
        first=service.prepeak_scheduler.periods[0]
        service.live_need_bytes=100*2**20
        candidates,plans=service.build(0.)
        self.assertEqual(service.prepeak_state.finish_by_s,first.start_s)
        for p in plans:
            self.assertEqual(p.reclaim_window.finish_by_s,first.start_s)
            for action in p.actions:
                if action.mode=='reclaim':
                    self.assertLessEqual(action.end_s+p.reclaim_window.batch_bound_s,first.start_s)

    def test_half_second_updates_cannot_reopen_a_protected_growth_period(self):
        service=self.service();chosen=deepcopy(service.sequence_highs)
        service.coordinator.pool.shutdown(wait=True);service.coordinator.pool=None
        first=service.prepeak_scheduler.periods[0]
        now=first.start_s
        while now<min(first.end_s,service.expires):
            service.live_need_bytes=100*2**20
            candidates,plans=service.build(now)
            service.coordinator.update_forecast(candidates,plans,service.prepeak_state.finish_by_s,now)
            service.coordinator.tick(now,lambda:100*2**20)
            self.assertEqual(service.prepeak_state.finish_by_s,first.start_s)
            self.assertEqual(service.sequence_highs,chosen)
            now+=.5
        for backend in service.backends.values():
            self.assertFalse(any(k=='reclaim' for k,_ in backend.writes))

    def test_native_entry_keeps_half_second_prediction_cadence_and_reports_timing(self):
        service=self.service()
        service.coordinator.pool.shutdown(wait=True);service.coordinator.pool=None
        (self.directory/'memory.current').write_text(str(service.quota-50*2**20),encoding='ascii')
        initial_calls=len(service.source.calls)
        for now in (0.,.1,.49,.5):
            service.tick(now,self.directory/'front')
        self.assertEqual(len(service.source.calls)-initial_calls,2)
        report=service.report()
        self.assertIn('prepeak_completion_checks',report)
        self.assertIn('prepeak_completion_overruns',report)
        self.assertTrue(report['reclaim_release_profiles'])

    def test_old_thirteen_november_budgets_are_not_replaced_by_sixteen_mib(self):
        service=self.service()
        caps={a:e.budget_ceiling for a,e in service.coordinator.executors.items()}
        self.assertTrue(all(caps[a]>16*2**20 for a in ('gimp','libreoffice','firefox')))

    def test_calibration_and_formal_have_identical_traces_floors_and_budgets(self):
        cal=self.service();contract=cal.contract;expected=self.trace(cal);cal.close(3.)
        with patch.object(shared_service,'load_qualified',return_value=({'eligible':True},'unit-only')):
            formal=self.service('formal');actual=self.trace(formal)
        self.assertEqual(contract,formal.contract);self.assertEqual(expected,actual)
        self.assertTrue(any(actual['writes'].values()))

    def test_missing_fresh_qualification_rejects_before_native_backends(self):
        with self.assertRaisesRegex(ValueError,'no matching total-fault|conditions changed'):self.service('formal')
        self.assertEqual(Backend.created,0)

    def test_changed_quota_or_sampling_rejected_before_backends(self):
        for key in ('quota','sampling'):
            bundle=deepcopy(self.bundle)
            if key=='quota':bundle['live_context']['memory_limit_bytes']+=4096
            else:bundle['collector_contract']['clear_refs']=True
            Backend.created=0
            with self.subTest(key=key),self.assertRaises(ValueError):self.service(bundle=bundle)
            self.assertEqual(Backend.created,0)

    def test_changed_conditions_cannot_rewrite_prepared_contract(self):
        bundle=deepcopy(self.bundle);bundle['_prepared']=shared_service.prepare_inputs(bundle)
        bundle['live_context']['old_document_revisit_s']=40
        with self.assertRaisesRegex(ValueError,'changed after preparation'):self.service(bundle=bundle)

    def test_unlimited_calibration_is_refused_before_workload_launch(self):
        import suite
        with patch.object(suite,'host_gate')as gate:
            with self.assertRaisesRegex(ValueError,'same explicit memory.max'):
                suite.run('revisit',True,stage='calibration',quota_bytes=None)
        gate.assert_not_called()


if __name__=='__main__':unittest.main()
