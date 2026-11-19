"""Timing regressions: finish before growth, preserve a cutoff, expose overruns."""
from dataclasses import replace
from threading import Event
from types import SimpleNamespace
import unittest

from coordinator import Coordinator
from executor import Executor, Guard
from plan import Candidate, MIB, ReclaimWindow
from prepeak_timing import (GrowthPeriod, PrepeakScheduler, ReleaseProfile,
                           growth_periods, release_profile, remaining_growth)
from replay_forecast import OnlineReplay
from test_measured import Backend
from test_sequence_policy import fixed_plan


def curve(points):
    return [dict(time_s=t,total=int(v*MIB),apps={'a':int(v*MIB)}) for t,v in points]


def source_curve():
    return curve([(0,100),(4,100),(8,100),(9,200),(10,300),(11,300),
                  (12,100),(13,100),(18,100),(19,300),(20,100),(21,100)])


def timed_plan(finish=8,end=10,start=0):
    return replace(fixed_plan(),reclaim_window=ReclaimWindow('first',start,finish,end,1.))


class GrowthDetectionTests(unittest.TestCase):
    def test_cutoff_is_rise_start_instead_of_highest_point(self):
        periods,_=growth_periods(source_curve())
        self.assertEqual([(p.start_s,p.end_s) for p in periods],[(8,13),(18,21)])

    def test_noise_does_not_turn_a_quiet_prefix_into_a_growth_period(self):
        periods,_=growth_periods(curve([(0,100),(1,101),(2,100),(8,100),
                                       (9,300),(10,100),(11,100)]))
        self.assertEqual(periods[0].start_s,8)

    def test_short_dip_does_not_reopen_reclaim_during_one_burst(self):
        periods,_=growth_periods(curve([(0,100),(8,100),(9,300),(9.5,100),
                                       (10,300),(11,100),(12,100)]))
        self.assertEqual(len(periods),1)
        self.assertEqual((periods[0].start_s,periods[0].end_s),(8,12))

    def test_v_shape_uses_the_low_before_the_next_rise(self):
        periods,_=growth_periods(curve([(0,300),(4,100),(8,100),(9,300),(10,100),(11,100)]))
        self.assertEqual(periods[0].start_s,8)

    def test_falling_or_flat_curve_has_no_invented_growth(self):
        for values in ([(0,300),(5,200),(10,100)],[(0,100),(10,100)]):
            self.assertEqual(growth_periods(curve(values))[0],())


class WholeGapTimingTests(unittest.TestCase):
    def setUp(self):
        self.scheduler=PrepeakScheduler(source_curve())
        self.profiles={'a':ReleaseProfile(10*MIB,1.,100*MIB)}

    def test_larger_gap_moves_start_earlier_without_moving_finish(self):
        small=self.scheduler.update(0,20*MIB,self.profiles,{'a':100*MIB},20)
        large=self.scheduler.update(.5,60*MIB,self.profiles,{'a':100*MIB},20)
        self.assertEqual((small.finish_by_s,large.finish_by_s),(8,8))
        self.assertEqual((small.start_s,large.start_s),(4.5,.5))
        self.assertTrue(large.feasible)

    def test_plan_uses_sum_of_donor_durations_instead_of_one_syscall(self):
        profiles={**self.profiles,'b':ReleaseProfile(5*MIB,1.,100*MIB)}
        state=self.scheduler.update(0,60*MIB,profiles,{'a':20*MIB,'b':100*MIB},20)
        self.assertEqual(state.estimated_total_s,11.)
        self.assertEqual(state.start_s,0.)
        self.assertFalse(state.feasible)
        self.assertEqual(state.remaining_bytes,60*MIB)

    def test_forecast_cannot_shift_active_cutoff_to_a_later_peak(self):
        first=self.scheduler.update(0,20*MIB,self.profiles,{'a':100*MIB},20)
        self.scheduler.periods=(GrowthPeriod('changed',12,17),)
        changed=self.scheduler.update(9,30*MIB,self.profiles,{'a':100*MIB},20)
        self.assertEqual(changed.key,first.key)
        self.assertEqual(changed.finish_by_s,8)
        self.assertEqual(changed.reason,'peak_protected')
        self.assertGreaterEqual(changed.protected_until_s,first.protected_until_s)

    def test_later_growth_can_get_a_new_window_after_quiet_recovery(self):
        self.scheduler.update(0,20*MIB,self.profiles,{'a':100*MIB},20)
        next_period=self.scheduler.update(14,20*MIB,self.profiles,{'a':100*MIB},22)
        self.assertEqual(next_period.finish_by_s,18)
        self.assertNotEqual(next_period.key,'0')

    def test_missing_speed_evidence_is_not_assumed_infinite(self):
        state=self.scheduler.update(0,20*MIB,{}, {'a':100*MIB},20)
        self.assertFalse(state.feasible)
        self.assertEqual(state.unallocated_bytes,20*MIB)

    def test_credited_release_and_batch_gaps_bound_the_speed(self):
        point=SimpleNamespace(observed_net_release_bytes=80*MIB,requested_bytes=80*MIB)
        writes=[dict(name='memory.reclaim',value=32*MIB,start_ns=0,end_ns=200_000_000,
                     before_bytes=100*MIB,after_bytes=98*MIB,error=None)]
        profile=release_profile(point,{'calibration_writes':writes},Guard(),1.)
        self.assertEqual(profile.credited_bytes,2*MIB)
        self.assertLess(profile.bytes_per_s,3*MIB)
        writes[0]['after_bytes']=writes[0]['before_bytes']
        self.assertEqual(release_profile(point,{'calibration_writes':writes},Guard(),1.).bytes_per_s,0)

    def test_feedback_does_not_count_cached_growth_twice(self):
        source=OnlineReplay(curve([(0,100),(1,150),(2,200)]),{'a':[]})
        forecast=source.predict(0,100*MIB)
        self.assertEqual(remaining_growth(forecast,0),100*MIB)
        self.assertEqual(remaining_growth(forecast,1),50*MIB)
        self.assertEqual(max(0,remaining_growth(forecast,1)-50*MIB),0)


class CompletionFenceTests(unittest.TestCase):
    def test_next_batch_is_not_sent_if_it_cannot_finish(self):
        b=Backend();e=Executor(b);e.update(timed_plan(),0);e.tick(7.1,80*MIB)
        self.assertFalse(any(k=='reclaim' for k,_ in b.calls))
        self.assertEqual(e.used,0)

    def test_no_reclaim_during_growth_while_fixed_high_stays_the_same(self):
        b=Backend();e=Executor(b);p=timed_plan();e.update(p,0);e.tick(0,80*MIB)
        count=len(b.calls);spent=e.used
        for index,now in enumerate((8.,8.5,9.,9.5),1):
            e.update(replace(p,revision=index),now);e.tick(now,80*MIB)
        self.assertEqual(len(b.calls),count)
        self.assertEqual(e.used,spent)
        self.assertEqual(b.high,p.sequence_high.target_bytes)

    def test_deferred_high_is_not_newly_installed_near_or_inside_the_peak(self):
        for now in (7.1,8.,9.):
            b=Backend();e=Executor(b);e.update(timed_plan(),0);e.tick(now,80*MIB)
            with self.subTest(now=now):
                self.assertFalse(any(k=='high'for k,_ in b.calls))
                self.assertEqual(e.plan.sequence_high.target_bytes,220*MIB)

    def test_expired_window_cannot_install_high_after_its_protection_ends(self):
        b=Backend();e=Executor(b);e.update(timed_plan(finish=3,end=5),0);e.tick(5.1,80*MIB)
        self.assertFalse(any(k=='high'for k,_ in b.calls))
        self.assertFalse(any(x['event']=='high_completion_overrun'for x in e.events))

    def test_high_checks_clock_again_after_backend_reads(self):
        clock=[0.];b=Backend();e=Executor(b,clock=lambda:clock[0]);e.update(timed_plan(),0)
        def rss():
            clock[0]=7.2;return b.value
        b.rss=rss;e.tick(0,80*MIB)
        self.assertFalse(any(k=='high'for k,_ in b.calls))

    def test_slow_high_install_is_logged_and_restored(self):
        clock=[0.];b=Backend();e=Executor(b,clock=lambda:clock[0]);e.update(timed_plan(),0)
        original=b.write_high
        def slow(value):
            original(value)
            if value is not None:clock[0]=9.
        b.write_high=slow;e.tick(0,80*MIB)
        self.assertTrue(any(x['event']=='high_completion_overrun'for x in e.events))
        self.assertIsNone(b.high)
        self.assertFalse(any(k=='reclaim'for k,_ in b.calls))

    def test_same_peak_cannot_remove_or_extend_its_fence(self):
        p=timed_plan();e=Executor(Backend());e.update(p,0)
        for window in (None,replace(p.reclaim_window,finish_by_s=9),
                       replace(p.reclaim_window,key='other')):
            with self.subTest(window=window),self.assertRaises(ValueError):
                e.update(replace(p,revision=1,reclaim_window=window),1)

    def test_backend_read_delay_is_checked_at_actual_dispatch(self):
        clock=[0.];b=Backend();e=Executor(b,clock=lambda:clock[0]);e.update(timed_plan(),0)
        def rss():
            clock[0]=7.2
            return b.value
        b.rss=rss;e.tick(0,80*MIB)
        self.assertFalse(any(k=='reclaim' for k,_ in b.calls))

    def test_syscall_overrun_is_a_failure_with_no_budget_refund(self):
        clock=[0.];b=Backend();e=Executor(b,clock=lambda:clock[0]);e.update(timed_plan(),0)
        original=b.reclaim
        def slow(amount):
            original(amount);clock[0]=9.
        b.reclaim=slow;e.tick(0,80*MIB)
        self.assertTrue(any(x['event']=='reclaim_completion_overrun'for x in e.events))
        self.assertEqual(e.state,'paused');self.assertEqual(e.used,8*MIB)
        self.assertIsNone(b.high)

    def test_worker_queue_delay_cannot_use_an_old_timestamp(self):
        clock=[0.];b=Backend();p=timed_plan();c=Candidate('a',1000,200*MIB,(),sequence_high=p.sequence_high)
        co=Coordinator([c],{'a':b},'fixed',10,plans=[p],deadline_s=8,parallel=True,clock=lambda:clock[0])
        entered=Event();release=Event()
        def block():
            entered.set();release.wait(5)
        co.pool.submit(block);self.assertTrue(entered.wait(2))
        try:
            co.tick(0,lambda:80*MIB);clock[0]=7.5
        finally:
            release.set();co.close(8)
        self.assertFalse(any(k=='reclaim'for k,_ in b.calls))


if __name__=='__main__':unittest.main()
