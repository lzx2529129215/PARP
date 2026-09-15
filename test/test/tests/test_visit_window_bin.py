import copy
import datetime as dt
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

P = Path(__file__).resolve().parents[1] / "visit_window_bin.py"
s = importlib.util.spec_from_file_location("visit_window_bin_tested", P)
m = importlib.util.module_from_spec(s); sys.modules[s.name] = m; s.loader.exec_module(m)


class BinTests(unittest.TestCase):
    def setUp(self):
        self.scope = SimpleNamespace(apps=[SimpleNamespace(app_key=k, app_id=i, vocab_name=k,
            prediction_enabled=True) for i,k in enumerate(['A','B','C'],1)])
        self.now = dt.datetime(2026,9,10,12)
        self.bundle = {'prediction_format':'visit_window','status':'success','inference_executed':True,
                       'all_probabilities':[]}
        for name,p30,p180 in [('A',.01,.1),('B',.05,.199),('C',.9,.95)]:
            self.bundle['all_probabilities'].append(dict(app=name,p_visit_30s=p30,p_visit_180s=p180,
                predicted_at=self.now.isoformat(),expires_at=(self.now+dt.timedelta(seconds=30)).isoformat(),
                probability_source='nested_sigmoid_uncalibrated'))

    def project(self, bundle=None, now=None):
        return m.project(bundle or self.bundle,self.scope,'A',['A','B','C'],now or self.now)

    def test_p180_ranking_foreground_and_original_probabilities(self):
        original=copy.deepcopy(self.bundle); entries,audit,ttl=self.project()
        self.assertEqual([x[0] for x in entries],[1,3,2])
        self.assertEqual(entries[0],(1,32767,1,m.ENTRY_FOREGROUND))
        self.assertEqual(ttl,30000)
        self.assertEqual(self.bundle,original)
        self.assertEqual([x['thermal_state'] for x in audit],['foreground','cold','hot'])

    def test_threshold_quantization_never_turns_neutral_cold(self):
        self.bundle['all_probabilities'][1]['p_visit_180s']=.2
        _,audit,_=self.project()
        self.assertFalse(audit[1]['kernel_probability_cold'])
        self.assertEqual(audit[1]['thermal_state'],'neutral')

    def test_missing_duplicate_and_nonmonotone_rejected(self):
        for modify in (lambda b:b['all_probabilities'].pop(),
                       lambda b:b['all_probabilities'].append(b['all_probabilities'][0]),
                       lambda b:b['all_probabilities'][1].update(p_visit_30s=.8),
                       lambda b:b['all_probabilities'][1].update(p_visit_180s=float('nan'))):
            b=copy.deepcopy(self.bundle);modify(b)
            with self.assertRaises(ValueError):self.project(b)

    def test_expiry_future_and_remaining_lifetime(self):
        self.assertEqual(self.project(now=self.now+dt.timedelta(seconds=29))[2],1000)
        for delta in (-1,30,31):
            with self.assertRaises(ValueError):self.project(now=self.now+dt.timedelta(seconds=delta))

    def test_live_bindings_cannot_escape_experiment_boundary(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); (root/'inside/a').mkdir(parents=True); (root/'outside').mkdir()
            bridge=m.VisitWindowBinBridge(policy_root=root/'inside',mode='dry-run',device=root/'none',
                runtime_scope=self.scope,output_dir=root,session_id='test',cgroup_root=root)
            try:
                sample=lambda path:SimpleNamespace(app_id='A',identity=SimpleNamespace(cgroup_path=path))
                self.assertEqual(len(bridge._bindings([sample('/inside/a')],{1})[0]),1)
                with self.assertRaises(ValueError):bridge._bindings([sample('/outside')],{1})
                with self.assertRaises(ValueError):bridge._bindings([],{1})
            finally:bridge.close()

    def test_full_bridge_builds_atomic_state_without_legacy_probabilities(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); policy=root/'policy';policy.mkdir()
            samples=[]
            for key in ('A','B','C'):
                (policy/key).mkdir()
                samples.append(SimpleNamespace(app_id=key,identity=SimpleNamespace(cgroup_path='/policy/'+key)))
            bridge=m.VisitWindowBinBridge(policy_root=policy,mode='dry-run',device=root/'none',
                runtime_scope=self.scope,output_dir=root,session_id='test',cgroup_root=root)
            try:
                now=dt.datetime.now()
                for row in self.bundle['all_probabilities']:
                    row.update(predicted_at=now.isoformat(),expires_at=(now+dt.timedelta(seconds=30)).isoformat())
                with patch.object(bridge,'_make_state_v3',wraps=bridge._make_state_v3) as make:
                    bridge.submit_prediction({'foreground_app':'A','open_apps':'A|B|C'},self.bundle,process_samples=samples)
                    self.assertEqual(make.call_count,1)
                    entries=make.call_args.args[0]
                    self.assertEqual(entries[1][1],m.math.ceil(.95*m.Q15_ONE))
                    self.assertEqual(len(make.call_args.args[1]),3)
                self.assertEqual(bridge._stats['dry_runs'],1)
                self.assertEqual(bridge._stats['ioctl_attempts'],0)
                self.assertTrue(bridge.projection_log.exists())
                cached={**self.bundle,'inference_executed':False}
                bridge.submit_prediction({'foreground_app':'A','open_apps':'A|B|C'},cached,process_samples=samples)
                self.assertEqual(bridge._stats['dry_runs'],1)
            finally:bridge.close()


if __name__=='__main__':unittest.main()
