import importlib.util
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

P=Path(__file__).resolve().parents[1]/'visit_window_real_effects.py'
s=importlib.util.spec_from_file_location('visit_effects_tested',P)
m=importlib.util.module_from_spec(s);sys.modules[s.name]=m;s.loader.exec_module(m)


class EffectTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config=json.loads(m.CONFIG.read_text())

    def test_plans_use_original_GUI_and_heldout_behavior_only(self):
        with patch.object(m,'select_trace',return_value={'source_split':'test','events':[]}):
            for name in ('r1','r2','r3','r4','r5'):
                p=m.make_plan(self.config,name)
                text=json.dumps(p)
                self.assertEqual(p['cold_hot_assignment'],'model_only')
                self.assertEqual(p['trace']['source_split'],'test')
                self.assertTrue(p['pressure']['memory_max_reused_exactly_within_pair'])
                for token in ('MADV_COLD','memory.reclaim','memory-fixture','REDIRTY','TOUCH_HOT','minimum_hot_probability'):
                    self.assertNotIn(token,text)
                self.assertEqual(p['require_file_dirty'],name in ('r4','r5'))
                self.assertEqual(p['require_writepage_promotion'],name=='r5')

    def test_dirty_gate_uses_prediction_and_does_not_substitute_anon(self):
        prediction={'all_probabilities':[{'app_key':'A','thermal_state':'cold'},
                                        {'app_key':'B','thermal_state':'foreground'}]}
        snap={'apps':{'A':{'memory_stat':{'anon':10000*m.MIB,'file':300*m.MIB,'file_dirty':0}},
                      'B':{'memory_stat':{'file':300*m.MIB}}}}
        plan={'require_file_dirty':True}
        self.assertFalse(m.dirty_gate(plan,self.config,prediction,snap)['valid'])
        snap['apps']['A']['memory_stat'].update(file=256*m.MIB,file_dirty=200*m.MIB)
        self.assertTrue(m.dirty_gate(plan,self.config,prediction,snap)['valid'])
        prediction['all_probabilities'][0]['thermal_state']='neutral'
        self.assertFalse(m.dirty_gate(plan,self.config,prediction,snap)['valid'])

    def test_system_swapin_is_not_claimed_as_app_swapin(self):
        before={'apps':{'A':{'memory_current':100,'memory_swap':0,'memory_stat':{'pgmajfault':1},'psi':{}}}}
        after={'apps':{'A':{'memory_current':50,'memory_swap':20,'memory_stat':{'pgmajfault':4},'psi':{'full_total':25}}}}
        r=m.counter_delta(before,after)['A']
        self.assertEqual(r['pgmajfault'],3)
        self.assertEqual(r['memory_drop_bytes'],50)
        self.assertEqual(r['psi_full_us'],25)
        self.assertIsNone(r['pswpin'])

    def test_kernel_controls_restore_after_failure(self):
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as d, patch.object(m,'privileged_read',return_value='0'), \
             patch.object(m,'write_control') as write, patch.object(m.V,'write_json'):
            with self.assertRaisesRegex(RuntimeError,'readback mismatch'):
                with m.controls('parp','r2',Path(d)):pass
            # All explicit controls are restored, including mode and bin.
            writes={str(c.args[0]):c.args[1] for c in write.call_args_list}
            self.assertTrue(writes)
            self.assertTrue(all(v=='0' for v in writes.values()))


if __name__=='__main__':unittest.main()
