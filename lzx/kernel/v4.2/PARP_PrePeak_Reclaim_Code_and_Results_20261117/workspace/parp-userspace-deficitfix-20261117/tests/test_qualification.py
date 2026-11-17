from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from test_total_faults import cohort
from total_profiles import publish,load_qualified
from execution_contract import SCHEMA


class QualificationTests(unittest.TestCase):
    def make(self,path,off,on):
        controls=[];trials=[]
        for i,row in enumerate(off+on):
            p=path/(str(i)+'.json');p.write_text(json.dumps(row),encoding='utf-8')
            (controls if i<3 else trials).append(p)
        return publish(path/'table.json',controls,trials,off[0]['execution_contract'],'per_app',root=path)

    def test_recomputes_totals_and_rejects_tampered_evidence(self):
        off,on=cohort()
        with tempfile.TemporaryDirectory()as name:
            root=Path(name);doc=self.make(root,off,on)
            self.assertTrue(doc['assessment']['eligible'])
            self.assertTrue(load_qualified(root/'table.json',off[0]['execution_contract'],'per_app',root=root)[0]['eligible'])
            (root/'3.json').write_text('{}',encoding='utf-8')
            with self.assertRaisesRegex(ValueError,'raw qualification evidence changed'):
                load_qualified(root/'table.json',off[0]['execution_contract'],'per_app',root=root)

    def test_changed_test_limit_cannot_reuse_calibration(self):
        off,on=cohort()
        with tempfile.TemporaryDirectory()as name:
            root=Path(name);self.make(root,off,on)
            changed=deepcopy(off[0]['execution_contract']);changed['live_context']['memory_limit_bytes']+=4096
            with self.assertRaisesRegex(ValueError,'conditions changed'):
                load_qualified(root/'table.json',changed,'per_app',root=root)

    def test_no_action_never_certifies_an_available_reclaim_amount(self):
        off,on=cohort()
        for row in on:row['writes']={}
        with tempfile.TemporaryDirectory()as name:
            root=Path(name);doc=self.make(root,off,on)
            self.assertFalse(doc['assessment']['eligible'])
            with self.assertRaisesRegex(ValueError,'did not pass'):
                load_qualified(root/'table.json',off[0]['execution_contract'],'per_app',root=root)

    def test_fixed_high_above_usage_is_not_reported_as_actual_reclamation(self):
        off,on=cohort()
        for row in on:
            row['writes']={'gimp':[dict(name='memory.high',value=200*2**20,previous_limit=None,before_bytes=100*2**20)]}
        with tempfile.TemporaryDirectory()as name:
            doc=self.make(Path(name),off,on)
            self.assertEqual(doc['assessment']['reason'],'no_actual_intervention')

    def test_later_high_pressure_counts_as_intervention_but_not_space_or_latency_success(self):
        off,on=cohort()
        for row in on:
            row['writes']={};row['initial']={'events':{'high':0}};row['final']={'events':{'high':1}}
        with tempfile.TemporaryDirectory()as name:
            result=self.make(Path(name),off,on)['assessment']
            self.assertTrue(result['eligible'])
            self.assertFalse(result['space_target_qualified'])
            self.assertFalse(result['latency_qualified'])

    def test_previous_policy_schema_cannot_qualify_fixed_high(self):
        off,on=cohort()
        with tempfile.TemporaryDirectory()as name:
            root=Path(name);doc=self.make(root,off,on)
            doc['schema']='parp-total-fault-same-service-v1'
            (root/'table.json').write_text(json.dumps(doc),encoding='utf-8')
            with self.assertRaisesRegex(ValueError,'conditions changed'):
                load_qualified(root/'table.json',off[0]['execution_contract'],'per_app',root=root)


if __name__=='__main__':unittest.main()
