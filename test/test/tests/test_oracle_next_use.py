import ctypes
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from oracle_next_use import NextUse, bin_for_rank, same_state, PredictStateV3, compile_plan, ROOT, OracleBridge
from user_events_oracle import Controller, restore


def sequence(names):
    ids={name:i+1 for i,name in enumerate(dict.fromkeys(names))}
    return [dict(app_key=n,app_id=ids[n]) for n in names]


class NextUseTests(unittest.TestCase):
    def test_next_occurrence_not_last_occurrence_or_past(self):
        lookup=NextUse(sequence(['A','B','C','A','D','B']))
        entries,audit=lookup.rank(2,'C',{'A','B','C','D'})
        self.assertEqual(audit['reclaim_order'],['B','D','A'])
        self.assertEqual([r['next_segment'] for r in audit['rows']],[None,3,4,5])
        self.assertEqual(entries[0][1:],(32767,1,1))
        self.assertTrue(all(e[1]==0 and e[3]==0 for e in entries[1:]))

    def test_never_again_is_first_reclaimed_and_ties_use_id(self):
        lookup=NextUse(sequence(['A','B','C','D','C']))
        _,audit=lookup.rank(3,'D',{'A','B','C','D'})
        self.assertEqual(audit['reclaim_order'],['A','B','C'])
        self.assertEqual([r['app_key'] for r in audit['rows']],['D','C','A','B'])

    def test_not_opened_or_exited_apps_are_not_submitted(self):
        lookup=NextUse(sequence(['A','B','C','A']))
        _,audit=lookup.rank(0,'A',{'A','C'})
        self.assertNotIn('B',audit['live_apps'])
        _,audit=lookup.rank(3,'A',{'A'})
        self.assertEqual(audit['reclaim_order'],[])

    def test_foreground_mismatch_and_unknown_live_app_rejected(self):
        lookup=NextUse(sequence(['A','B','A']))
        for index,fg,opened in [(1,'A',{'A','B'}),(0,'A',{'B'}),(0,'A',{'A','X'})]:
            with self.assertRaises(ValueError):lookup.rank(index,fg,opened)

    def test_repeated_app_uses_explicit_cursor(self):
        lookup=NextUse(sequence(['A','B','A','C','B']))
        self.assertEqual(lookup.rank(0,'A',{'A','B','C'})[1]['reclaim_order'],['C','B'])
        self.assertEqual(lookup.rank(2,'A',{'A','B','C'})[1]['reclaim_order'],['B','C'])

    def test_eight_bin_rank_floors(self):
        self.assertEqual([bin_for_rank(i) for i in range(1,15)],[7,6,5,4,3,3,2,2,2,1,1,1,0,0])

    def test_readback_rejects_concurrent_writer_or_wrong_binding(self):
        a=PredictStateV3();a.generation=4;a.nr_predictions=1;a.nr_bindings=1
        a.predictions[0].app_id=1;a.bindings[0].domain_id=42
        b=PredictStateV3.from_buffer_copy(bytes(a));self.assertTrue(same_state(a,b))
        b.generation=5;self.assertFalse(same_state(a,b))
        b.generation=4;b.bindings[0].domain_id=43;self.assertFalse(same_state(a,b))

    def test_complete_comparison_compiles_without_group_boundaries(self):
        p=compile_plan(ROOT/'test_reports/user_events原始与当前复现对比.xlsx',Path('/home/lzx/Desktop/user_events合并.xlsx'))
        self.assertEqual(len(p['segments']),85)
        self.assertEqual(sum(len(s['events']) for s in p['segments']),2230)
        self.assertTrue(all(a['app_key']!=b['app_key'] for a,b in zip(p['segments'],p['segments'][1:])))

    def test_dialog_does_not_advance_cursor_and_nonsequential_switch_rejected(self):
        c=object.__new__(Controller)
        import threading
        c.lock=threading.RLock();c.error=None;c.index=3;c.foreground=Mock(return_value=True)
        with self.assertRaisesRegex(RuntimeError,'Nonsequential'):c.arm(3,'A')
        with self.assertRaisesRegex(RuntimeError,'Nonsequential'):c.arm(5,'A')
        self.assertEqual(c.index,3)
        with self.assertRaisesRegex(RuntimeError,'cannot advance'):c.arm(5,'A','document_relaunch')
        c.live=Mock(return_value=({'A':[]},[]));c.publish=Mock();c.allowed_departures=set();c.r=Mock();c.r.allowed_restarts=set()
        c.arm(3,'A','document_relaunch');self.assertEqual(c.index,3)
        c.foreground.return_value=False
        with self.assertRaisesRegex(RuntimeError,'foreground not verified'):c.arm(4,'B')
        self.assertEqual(c.index,3)

    def test_incomplete_ambiguous_or_escaped_bindings_are_rejected(self):
        import tempfile
        from types import SimpleNamespace as S
        with tempfile.TemporaryDirectory() as temp:
            base=Path(temp);root=base/'experiment';root.mkdir()
            for name in ['a','b']:(root/name).mkdir()
            (base/'outside').mkdir()
            bridge=object.__new__(OracleBridge)
            bridge.policy_root=root;bridge.cgroup_root=base
            bridge.runtime_scope=S(apps=[S(app_key='A',app_id=1),S(app_key='B',app_id=2)])
            sample=lambda a,p:S(app_id=a,identity=S(cgroup_path=p))
            entries=[(1,32767,1,1),(2,0,2,0)]
            for samples in [[sample('A','experiment/a')],
                [sample('A','experiment/a'),sample('B','experiment/a')],
                [sample('A','experiment/a'),sample('B','outside')]]:
                with self.assertRaises(RuntimeError):bridge.publish(entries,{},samples)

    def test_readback_requires_lease_timestamp_and_ttl(self):
        a=PredictStateV3();a.timestamp_ns=100;a.ttl_ns=5000000000
        b=PredictStateV3.from_buffer_copy(bytes(a));b.ttl_ns-=1
        self.assertFalse(same_state(a,b))
        b.ttl_ns=a.ttl_ns;b.timestamp_ns+=1
        self.assertFalse(same_state(a,b))

    def test_restoration_is_idempotent_and_resumes_only_originally_active_service(self):
        import json,tempfile
        with tempfile.TemporaryDirectory() as temp:
            p=Path(temp)/'state.json'
            p.write_text(json.dumps(dict(controls={'/example/control':'1'},service_active=True,units=['owned.service'],roots=[])))
            with patch('user_events_oracle.write_priv') as write,patch('user_events_oracle.read_priv',return_value='1'),patch('user_events_oracle.systemctl',return_value=Mock(stdout='active')) as ctl,patch('user_events_oracle.time.sleep'):
                restore(p);calls=write.call_count;restore(p)
                self.assertEqual(write.call_count,calls)
                ctl.assert_any_call('start','parp-runtime-monitor.service')
                self.assertTrue(json.loads(p.read_text())['restored'])

    def test_unexpected_background_exit_is_not_silently_removed(self):
        c=object.__new__(Controller);c.last_live={'A','B'};c.allowed_departures=set()
        with self.assertRaisesRegex(RuntimeError,'Unexpected application exit'):c.publish('lease',{'A':[]},[])

    def test_resident_publisher_restart_stops_renewal(self):
        c=object.__new__(Controller);c.last_live={'A'};c.allowed_departures=set()
        with patch('user_events_oracle.systemctl',return_value=Mock(stdout='active')):
            with self.assertRaisesRegex(RuntimeError,'Resident publisher'):c.publish('lease',{'A':[]},[])

    def test_pressure_gate_cap_and_pair_variance(self):
        import tempfile
        from user_events_oracle import MIB
        with tempfile.TemporaryDirectory() as temp:
            c=object.__new__(Controller);c.root=c.out=Path(temp)
            c.pressure=True;c.anchor=None;c.policy='native';c.index=3;c.expected='D'
            c.snapshot=Mock(return_value={})
            live={a:[] for a in 'ABCD'}
            audit=NextUse(sequence(['A','B','C','D','A','B'])).rank(3,'D',live)[1]
            (c.root/'memory.current').write_text(str(767*MIB))
            with patch('user_events_oracle.write_priv') as write,patch('user_events_oracle.kernel_stats',return_value={}):
                c.maybe_pressure(live,audit);write.assert_not_called()
                (c.root/'memory.current').write_text(str(800*MIB))
                c.maybe_pressure({a:[] for a in 'ABD'},audit);write.assert_not_called()
                c.maybe_pressure(live,audit)
                self.assertEqual(c.anchor['memory_max'],608*MIB)
                write.assert_called_once_with(c.root/'memory.max',608*MIB)
                c.reference=c.anchor;c.anchor=None;c.policy='oracle'
                (c.root/'memory.current').write_text(str(1000*MIB))
                with self.assertRaisesRegex(RuntimeError,'exceeds 15%'):c.maybe_pressure(live,audit)
                self.assertIsNone(c.anchor)
                self.assertEqual(write.call_count,1)

    def test_overwritten_kernel_state_is_rejected_before_set(self):
        import tempfile
        from types import SimpleNamespace as S
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);app=root/'a';app.mkdir()
            bridge=object.__new__(OracleBridge)
            bridge.policy_root=bridge.cgroup_root=root
            bridge.runtime_scope=S(apps=[S(app_key='A',app_id=1)])
            bridge.mode='apply';bridge.generation=8
            bridge._fill_state=Mock()
            before=PredictStateV3();before.generation=8
            after=PredictStateV3();after.generation=9
            bridge._oracle_previous_state=before;bridge._get_state=Mock(return_value=after)
            with patch('oracle_next_use.fcntl.ioctl') as ioctl:
                with self.assertRaisesRegex(RuntimeError,'overwritten'):
                    bridge.publish([(1,32767,1,1)],{},[S(app_id='A',identity=S(cgroup_path='a'))])
                ioctl.assert_not_called()

    def test_vanished_helper_recollects_without_skipping_cursor(self):
        c=object.__new__(Controller);c.expected='A';c.index=3
        c._publish_once=Mock(side_effect=[FileNotFoundError(),{'ok':True}])
        c.live=Mock(return_value=({'A':['new']},['new-sample']))
        c.foreground=Mock(return_value=True)
        self.assertEqual(c.publish('bindings_changed',{'A':['old']},['old-sample']),{'ok':True})
        self.assertEqual(c.index,3)
        c._publish_once.assert_called_with('bindings_changed',{'A':['new']},['new-sample'])

    def test_unexpected_window_loss_cannot_be_hidden_by_ensure_restart(self):
        from user_events_oracle import ContinuousReplay
        r=object.__new__(ContinuousReplay);r.attempted={'A'};r.allowed_restarts=set()
        r.state=Mock(return_value={'windows':[]})
        with patch('user_events_oracle.U.Replay.ensure') as ensure:
            with self.assertRaisesRegex(RuntimeError,'automatic restart forbidden'):r.ensure('A')
            ensure.assert_not_called()
            r.allowed_restarts.add('A');r.ensure('A');ensure.assert_called_once_with('A')

    def test_live_app_binds_empty_owned_ancestors_and_helper_domains(self):
        c=object.__new__(Controller);c.root=Path('/sys/fs/cgroup/experiment')
        c.r=Mock();c.r.attempted={'A'};c.r.extra_units={'app-a-row1.service'}
        active=True
        def read(path,*args,**kwargs):
            if path.name=='cgroup.events':return 'populated '+str(int(active and path.parent.name=='app-a.service'))+'\n'
            return '99\n' if path.parent.name=='child' else ''
        def nested(path,*args,**kwargs):
            return [path/'child/cgroup.procs'] if path.name=='app-a.service' else []
        with patch('user_events_oracle.U.P.gui.unit',return_value='app-a.service'),patch.object(Path,'read_text',read),patch.object(Path,'rglob',nested):
            live,samples=c.live()
            self.assertEqual(set(live),{'A'})
            self.assertEqual({s.identity.cgroup_path for s in samples},{
                '/experiment/app-a.service','/experiment/app-a.service/child','/experiment/app-a-row1.service'})
            active=False
            self.assertEqual(c.live(),({},[]))


if __name__=='__main__':unittest.main()
