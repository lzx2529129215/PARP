"""A delayed clock check must never issue a negative sleep in the input probe."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'lab'))
from harness_contracts import wait_until_ns,schedule_diagnostics


class ProbeTimingTests(unittest.TestCase):
    def test_deadline_crossed_between_checks_never_requests_negative_sleep(self):
        ticks=iter((99,101));sleeps=[]
        wait_until_ns(100,clock=lambda:next(ticks),sleep=sleeps.append)
        self.assertEqual(sleeps,[1e-9])

    def test_already_late_input_dispatch_does_not_sleep(self):
        sleeps=[]
        wait_until_ns(100,clock=lambda:101,sleep=sleeps.append)
        self.assertFalse(sleeps)

    def test_slow_predecessor_is_reported_not_filtered_as_dispatch_failure(self):
        ops=[dict(app='gimp',kind='new_hot_after_peak',start_lateness_s=4.1,queue_delay_s=4.,dispatch_lateness_s=.1)]
        invalid,queued=schedule_diagnostics(ops)
        self.assertFalse(invalid);self.assertEqual(len(queued),1)
        self.assertEqual(queued[0]['queue_delay_s'],4.)

    def test_independent_late_dispatch_still_rejects_a_trial(self):
        ops=[dict(app='gimp',kind='peak_new_data',start_lateness_s=1.,queue_delay_s=0.,dispatch_lateness_s=1.)]
        invalid,queued=schedule_diagnostics(ops)
        self.assertEqual(len(invalid),1);self.assertEqual(len(queued),1)


if __name__=='__main__':unittest.main()
