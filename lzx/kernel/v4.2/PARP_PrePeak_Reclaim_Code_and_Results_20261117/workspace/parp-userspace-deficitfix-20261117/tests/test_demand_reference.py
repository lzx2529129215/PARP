from copy import deepcopy
import unittest
from demand_reference import MIB,admission,choose_limit,reference_conditions,require_unclipped


def reference():
    return dict(trial=dict(initial=dict(current=3000*MIB,events={'high':0,'max':0}),
                          final=dict(events={'high':0,'max':0}),memory_limit_bytes=6144*MIB,
                          scenario='revisit',kernel='unit',peak_mode='concurrent',workload_version='unit',zswap='Y'),
                occupancy=[dict(time_s=0,total=3000*MIB),dict(time_s=10,total=3800*MIB),
                           dict(time_s=23,total=4000*MIB)])


class DemandReferenceTests(unittest.TestCase):
    def test_peak_clipped_by_limit_cannot_be_demand_reference(self):
        ref=reference();ref['trial']['final']['events']['max']=1
        with self.assertRaisesRegex(ValueError,'constrained'):require_unclipped(ref['trial'])
        ref=reference();ref['trial']['initial']['events']['max']=1;ref['trial']['final']['events']['max']=1
        with self.assertRaisesRegex(ValueError,'constrained'):require_unclipped(ref['trial'])

    def test_same_pressure_limit_cannot_silently_replace_independent_demand_reference(self):
        ref=reference();live={k:v for k,v in ref['trial'].items()if k in ('scenario','kernel','peak_mode','workload_version','zswap')}
        live['memory_limit_bytes']=3608*MIB
        with self.assertRaises(ValueError):reference_conditions(ref,live)
        condition=reference_conditions(ref,live,'unclipped_demand',64*MIB)
        self.assertEqual(condition['demand_growth_bytes'],800*MIB)
        self.assertEqual(condition['test_limit_bytes'],3608*MIB)

    def test_limit_is_chosen_before_test_arms_from_declared_gap(self):
        ref=reference();limit=choose_limit(ref,192*MIB)
        self.assertEqual(limit,3608*MIB)
        passed=admission(ref,dict(current=3000*MIB),limit,64*MIB)
        self.assertTrue(passed['passed']);self.assertEqual(passed['initial_gap_bytes'],192*MIB)

    def test_zero_deficit_refuses_workload_admission(self):
        with self.assertRaisesRegex(ValueError,'not admitted'):
            admission(reference(),dict(current=2800*MIB),3608*MIB,64*MIB)

    def test_target_cannot_force_startup_exhaustion(self):
        with self.assertRaisesRegex(ValueError,'startup'):
            choose_limit(reference(),900*MIB)


if __name__=='__main__':unittest.main()
