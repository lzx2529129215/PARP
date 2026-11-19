"""Behavioral checks for the fixed envelope and the independent fine controller."""
from dataclasses import replace
import unittest

from adapters import ColdReply, attach_external_outputs
from coordinator import Coordinator
from executor import Executor
from plan import Action, AppPlan, Calibration, Candidate, MIB, Phase, SequenceHigh, Trial, action_plan, revise_allocation
from sequence_policy import reclaim_decision, sequence_high
from test_measured import Backend


def windows(values):
    return [dict(start_s=i,end_s=i+1,wss_bytes=v*MIB,pgfault=1,valid=True) for i,v in enumerate(values)]


def fixed_plan(mode='reclaim',target=220):
    return AppPlan('a','fixed',0,0,10,100*MIB,
        (Action(0,10,mode,100*MIB,100*MIB,None,'measured'),),goal_bytes=100*MIB,
        method_budgets=(('high',40*MIB),('reclaim',100*MIB)),
        sequence_high=SequenceHigh(target*MIB,100*MIB,100*MIB,'whole-sequence'))


class SequencePlanningTests(unittest.TestCase):
    def test_whole_curve_peak_is_frozen_for_valley_peak_and_oscillation(self):
        for curve in ([800,200,200,800],[200,800,800,200],[800,200,800,200]):
            with self.subTest(curve=curve):
                fixed=sequence_high(windows(curve),[Trial(200*MIB,300*MIB,100,1)],1000*MIB,400*MIB,'evidence')
                self.assertEqual(fixed.target_bytes,800*MIB)

    def test_occupancy_and_tested_high_allowance_bound_the_fixed_limit(self):
        w=windows([200]*4)
        self.assertEqual(sequence_high(w,[Trial(300*MIB,850*MIB,0,0)],1000*MIB,400*MIB,'x').target_bytes,850*MIB)
        self.assertEqual(sequence_high(w,[Trial(200*MIB,300*MIB,0,0)],1000*MIB,100*MIB,'x').target_bytes,900*MIB)

    def test_fine_reclaim_has_no_curve_shape_or_fault_burst_gate(self):
        for curve in ([0]*4,[300]*4,[200,300,500,800],[800,500,300,200],[800,200,800,200]):
            for fault in (0,100000):
                w=windows(curve)
                for row in w:row['pgfault']=fault
                with self.subTest(curve=curve,fault=fault):
                    self.assertEqual(reclaim_decision(w,0,80*MIB,10,1)['mode'],'reclaim')

    def test_no_cold_or_revisit_lead_stops_fine_reclaim(self):
        w=windows([300]*10)
        self.assertEqual(reclaim_decision(w,0,0,10,1)['mode'],'observe')
        self.assertEqual(reclaim_decision(w,9,80*MIB,10,1)['mode'],'observe')
        self.assertEqual(reclaim_decision(w,8,80*MIB,10,1)['mode'],'reclaim')

    def test_near_term_hot_memory_is_retained_before_a_rise(self):
        d=reclaim_decision(windows([200,800,200,200]),.5,80*MIB,10,1)
        self.assertEqual(d['keep_rss_bytes'],800*MIB)

    def test_missing_sequence_intervals_are_not_invented(self):
        w=windows([200,800]);w[1]['start_s']=1.5
        with self.assertRaisesRegex(ValueError,'gap or overlap'):
            sequence_high(w,[Trial(1,1,0,0)],1000,100,'x')

    def test_allocations_change_reclaim_goal_without_changing_high(self):
        cal=Calibration('c',Trial(200*MIB,200*MIB,100,1),(Trial(100*MIB,100*MIB,105,1),),.1,None,None,0,'evidence','reclaim')
        c=Candidate('a',1000,200*MIB,(Phase(0,10,cal,'unknown',150*MIB,100000,'reclaim',150*MIB),),
                    sequence_high=fixed_plan().sequence_high)
        p=action_plan(c,80*MIB,'fixed',10,attempt_budget=100*MIB)
        q=revise_allocation(p,c,20*MIB)
        self.assertEqual(q.sequence_high,p.sequence_high)
        self.assertEqual(q.goal_bytes,20*MIB)
        self.assertEqual(q.actions[0].safe_rss_bytes,150*MIB)
        self.assertEqual(q.actions[0].mode,'reclaim')

    def test_expired_cold_certificate_stops_reclaim_but_keeps_high_eligible(self):
        c=Candidate('a',1000,200*MIB,(),sequence_high=fixed_plan().sequence_high)
        result,excluded=attach_external_outputs([c],[(1,1000,1,0)],{1:'a'},[ColdReply('a',80*MIB,0,2,'measured')],3)
        self.assertEqual(excluded['a'],'stale_cold_reply')
        self.assertTrue(result[0].background)
        self.assertEqual(result[0].cold_cap_bytes,0)
        result,_=attach_external_outputs([c],[(1,1000,1,1)],{1:'a'},[ColdReply('a',80*MIB,0,4,'measured')],3)
        self.assertFalse(result[0].background)


class FixedHighExecutionTests(unittest.TestCase):
    def test_reclaim_and_observe_do_not_remove_or_ramp_the_fixed_high(self):
        b=Backend();e=Executor(b);p=fixed_plan();e.update(p,0)
        e.tick(0,100*MIB);e.tick(.2,100*MIB)
        e.update(replace(p,revision=1,actions=(replace(p.actions[0],mode='observe'),)),.5)
        e.tick(.5,100*MIB)
        self.assertEqual([v for kind,v in b.calls if kind=='high'],[220*MIB])
        self.assertTrue(any(kind=='reclaim'for kind,_ in b.calls))
        e.close(1);self.assertIsNone(b.high)

    def test_no_deficit_installs_high_but_does_not_reclaim(self):
        b=Backend();e=Executor(b);e.update(fixed_plan(),0);e.tick(0,0)
        self.assertEqual(b.calls,[('high',220*MIB)])

    def test_high_and_reclaim_do_not_spend_the_same_goal_twice(self):
        b=Backend();e=Executor(b);p=replace(fixed_plan(target=192),goal_bytes=8*MIB)
        e.update(p,0);e.tick(0,8*MIB)
        self.assertEqual(b.calls,[('high',192*MIB)])
        self.assertEqual(e.used,8*MIB)

    def test_oversize_high_write_is_deferred_while_bounded_reclaim_makes_progress(self):
        b=Backend();e=Executor(b);p=replace(fixed_plan(target=100),method_budgets=(('high',100*MIB),('reclaim',100*MIB)))
        e.update(p,0)
        for i in range(10):e.tick(i*.2,100*MIB)
        high=[v for kind,v in b.calls if kind=='high']
        self.assertEqual(high,[100*MIB])
        self.assertTrue(all(v<=32*MIB for kind,v in b.calls if kind=='reclaim'))
        self.assertLessEqual(e.used,100*MIB)

    def test_current_rss_floor_prevents_reclaim_of_hot_pages(self):
        b=Backend();e=Executor(b);p=fixed_plan()
        p=replace(p,actions=(replace(p.actions[0],safe_rss_bytes=200*MIB),))
        e.update(p,0);e.tick(0,100*MIB)
        self.assertFalse(any(kind=='reclaim'for kind,_ in b.calls))

    def test_pause_restores_and_resume_reinstalls_the_same_high_without_refill(self):
        b=Backend();bad=[False];e=Executor(b,foreground_feedback=lambda now:bad[0]);e.update(fixed_plan(),0)
        e.tick(0,100*MIB);spent=e.used;bad[0]=True;e.tick(.2,100*MIB)
        self.assertEqual(e.state,'paused');self.assertIsNone(b.high)
        bad[0]=False;e.tick(1.1,100*MIB);e.tick(2.2,100*MIB)
        self.assertEqual(e.state,'active');self.assertEqual(b.high,220*MIB)
        self.assertEqual(e.plan.expires_s,10);self.assertEqual(e.budget_ceiling,100*MIB)
        self.assertGreaterEqual(e.used,spent)
        self.assertEqual({v for kind,v in b.calls if kind=='high'and v is not None},{220*MIB})

    def test_pressure_terminates_and_restores_high(self):
        b=Backend();e=Executor(b);e.update(fixed_plan(),0);e.tick(0,100*MIB)
        b.refault=4096;e.tick(1.1,100*MIB)
        self.assertEqual(e.state,'terminated');self.assertIsNone(b.high)

    def test_prediction_cannot_replace_the_fixed_high(self):
        b=Backend();e=Executor(b);p=fixed_plan();e.update(p,0)
        with self.assertRaisesRegex(ValueError,'fixed sequence high'):
            e.update(replace(p,revision=1,sequence_high=replace(p.sequence_high,target_bytes=230*MIB)),.5)

    def test_foreground_exclusion_restores_high_without_changing_its_chosen_value(self):
        b=Backend();e=Executor(b);p=fixed_plan();e.update(p,0);e.tick(0,100*MIB)
        e.update(replace(p,revision=1,sequence_high_enabled=False),.5);e.tick(.5,100*MIB)
        self.assertIsNone(b.high)
        self.assertEqual(e.plan.sequence_high,p.sequence_high)

    def test_administrator_high_is_not_relaxed_to_fit_the_fixed_target(self):
        b=Backend();b.high=210*MIB;e=Executor(b);e.update(fixed_plan(),0);e.tick(0,100*MIB)
        self.assertEqual(e.state,'terminated');self.assertEqual(b.high,210*MIB)

    def test_deadline_records_unfilled_space_and_stops_new_reclaim(self):
        b=Backend();p=fixed_plan();c=Candidate('a',1000,200*MIB,(),sequence_high=p.sequence_high)
        co=Coordinator([c],{'a':b},'fixed',10,plans=[p],deadline_s=1)
        co._accept([p],0);co.tick(1,lambda:80*MIB)
        self.assertEqual(co.events[-1]['unfilled_bytes'],80*MIB)
        self.assertFalse(any(kind=='reclaim'for kind,_ in b.calls))
        self.assertEqual(b.high,220*MIB);co.close(2)

    def test_closed_loop_fills_peak_headroom_without_changing_high_or_refilling_budget(self):
        b=Backend();p=fixed_plan();c=Candidate('a',1000,200*MIB,(),
            cold_cap_bytes=80*MIB,cold_valid_until_s=8,sequence_high=p.sequence_high)
        co=Coordinator([c],{'a':b},'fixed',10,plans=[p],deadline_s=1)
        co._accept([p],0)
        need=lambda:max(0,b.usage()-160*MIB)
        for now in (0,.2,.4,.5,.6,.8):
            e=co.executors['a']
            co.update_forecast([c],[replace(e.plan,revision=e.plan.revision+1)],1,now)
            co.tick(now,need)
        self.assertEqual(need(),0)
        self.assertEqual(co.executors['a'].used,40*MIB)
        self.assertEqual(co.executors['a'].budget_ceiling,100*MIB)
        self.assertEqual([v for kind,v in b.calls if kind=='high'],[220*MIB])
        co.tick(1,need)
        self.assertEqual(co.events[-1]['unfilled_bytes'],0)
        co.close(2)


if __name__=='__main__':unittest.main()
