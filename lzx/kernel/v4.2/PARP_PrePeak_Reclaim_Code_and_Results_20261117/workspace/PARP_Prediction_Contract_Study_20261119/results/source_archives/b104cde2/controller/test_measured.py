import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from dataclasses import replace

from signals import classify
from plan import Action,AppPlan,Calibration,Trial,MIB,Phase,Candidate,action_plan
from executor import Executor,Guard
from replay_forecast import OnlineReplay
from safety_store import SafetyPoint,select


class Backend:
    def __init__(self):
        self.value=200*MIB;self.high=None;self.refault=0;self.psi=0;self.calls=[]
    def check(self):pass
    def usage(self):return self.value
    def rss(self):return self.value
    def feedback(self):return self.refault,self.psi
    def protections(self):return 0,0
    def maximum(self):return 2**63-1
    def read_high(self):return self.high
    def write_high(self,v):
        self.calls.append(('high',v));self.high=v
        if v is not None:self.value=min(self.value,v)
    def reclaim(self,n):self.calls.append(('reclaim',n));self.value-=n
    def read_freeze(self):return 0
    def write_freeze(self,v):self.calls.append(('freeze',v))


def plan(actions=None):
    actions=actions or (Action(0,10,'high',100*MIB,100*MIB,100*MIB,'measured-unit'),)
    return AppPlan('a','p',0,0,10,100*MIB,actions,goal_bytes=100*MIB)


class NumericTests(unittest.TestCase):
    def test_low_fault_stable_envelope(self):
        self.assertEqual(classify([200*MIB,0,205*MIB,0],[1,0,1,0])['mode'],'high')
    def test_fault_burst_after_demand_rise(self):
        self.assertEqual(classify([200*MIB,190*MIB,500*MIB,500*MIB],[1,1,130000,1000])['mode'],'reclaim')
    def test_faults_actually_change_decision(self):
        w=[200*MIB,190*MIB,500*MIB,500*MIB]
        self.assertEqual(classify(w,[1,1,1,1])['mode'],'observe')
        self.assertEqual(classify(w,[1,1,130000,1000])['mode'],'reclaim')
    def test_no_rss_comparison_gate(self):
        # No RSS argument or requirement that future demand be below old RSS.
        self.assertEqual(classify([1*MIB,1*MIB,300*MIB,300*MIB],[0,0,100000,1000])['mode'],'reclaim')
    def test_missing_or_negative_cannot_be_zero_filled(self):
        for w,p in (([None,1],[0,0]),([1,2],[0,-1]),([1],[0,1])):
            with self.assertRaises(ValueError):classify(w,p)
    def test_ambiguous_idle_does_not_force_reclaim(self):
        self.assertEqual(classify([0,0,0],[0,0,0])['mode'],'observe')


class OnlineTests(unittest.TestCase):
    def source(self):
        return OnlineReplay([dict(time_s=0,total=120,apps={'a':100}),
            dict(time_s=5,total=230,apps={'a':180}),dict(time_s=10,total=190,apps={'a':150})],{'a':[]})
    def test_real_feedback_changes_gap(self):
        source=self.source()
        self.assertEqual(source.predict(0,60).required_bytes,50)
        self.assertEqual(source.predict(4,90).required_bytes,20)
        self.assertEqual(source.predict(6,20).required_bytes,0)
        self.assertEqual(len(source.calls),3)
    def test_non_app_growth_included_once(self):
        f=self.source().predict(0,60)
        self.assertEqual(f.curves['non_app'][-2][1],30)
    def test_exhausted_reference_rejected(self):
        with self.assertRaises(ValueError):self.source().predict(11,10)


class ExecutionTests(unittest.TestCase):
    def test_high_is_held_without_max_toggling(self):
        b=Backend();e=Executor(b);e.update(plan(),0)
        e.tick(0,100*MIB);first=b.high
        e.tick(.2,0)
        self.assertEqual(b.high,first)
        self.assertNotIn(('high',None),b.calls)
    def test_growth_relaxes_even_without_remaining_budget(self):
        b=Backend();e=Executor(b)
        p=plan((Action(0,2,'high',100*MIB,100*MIB,100*MIB,'x'),
                Action(2,10,'high',300*MIB,300*MIB,300*MIB,'x')))
        e.update(p,0);e.tick(0,100*MIB);e.used=e.budget_ceiling
        e.tick(2.1,0)
        self.assertIsNone(b.high)
    def test_foreground_pause_does_not_freeze_application(self):
        b=Backend();bad=[False];e=Executor(b,foreground_feedback=lambda now:bad[0]);e.update(plan(),0)
        e.tick(0,100*MIB);spent=e.used;bad[0]=True;e.tick(.2,100*MIB)
        self.assertEqual(e.state,'paused');self.assertIsNone(b.high)
        self.assertFalse(any(name=='freeze' for name,_ in b.calls))
        bad[0]=False;e.tick(1.1,100*MIB);e.tick(2.2,100*MIB)
        self.assertEqual(e.state,'active');self.assertGreaterEqual(e.used,spent)
        self.assertEqual(e.plan.expires_s,10)
    def test_prediction_cannot_refill_budget(self):
        b=Backend();e=Executor(b);e.update(plan(),0);e.tick(0,100*MIB)
        used=e.used
        with self.assertRaises(ValueError):e.update(replace(e.plan,revision=1,budget_bytes=101*MIB),.2)
        self.assertEqual(e.used,used)
    def test_rss_floor_independent_of_cgroup_floor(self):
        b=Backend();b.rss=lambda:100*MIB;e=Executor(b);e.update(plan(),0);e.tick(0,100*MIB)
        self.assertFalse(b.calls)
    def test_method_specific_ceiling_is_not_global_ceiling(self):
        b=Backend();e=Executor(b)
        p=replace(plan(),method_budgets=(('high',8*MIB),('reclaim',100*MIB)))
        e.update(p,0);e.tick(0,100*MIB);e.tick(.2,100*MIB)
        self.assertEqual(e.used_by_method['high'],8*MIB)
        self.assertEqual(e.remaining,92*MIB)
    def test_revision_cannot_refill_method_ceiling(self):
        b=Backend();e=Executor(b)
        p=replace(plan(),method_budgets=(('high',8*MIB),('reclaim',100*MIB)))
        e.update(p,0);e.tick(0,100*MIB)
        with self.assertRaises(ValueError):
            e.update(replace(p,revision=1,method_budgets=(('high',16*MIB),('reclaim',100*MIB))),.2)


class CalibrationTests(unittest.TestCase):
    def test_zero_baseline_positive_extra_not_certified(self):
        c=Calibration('x',Trial(100,100,0,10),(Trial(90,90,1,10),),.1,None,None,0,'unit')
        self.assertEqual(c.select().rss_bytes,100)
    def test_do_not_skip_failed_level(self):
        c=Calibration('x',Trial(100,100,100,10),
            (Trial(90,90,111,10),Trial(80,80,101,10)),.1,None,None,0,'unit')
        self.assertEqual(c.select().rss_bytes,100)
    def test_real_evidence_is_required(self):
        with tempfile.TemporaryDirectory() as name:
            p=Path(name)/'evidence.json';p.write_text('{}');sha=hashlib.sha256(p.read_bytes()).hexdigest()
            point=SafetyPoint('a','high',80,80,20,20,100,109,1,2,str(p),sha,str(p),sha,'w',True,True)
            self.assertEqual(select([point]),point)
            p.write_text('{"changed":true}')
            with self.assertRaises(ValueError):select([point])


class PlanIntegrationTests(unittest.TestCase):
    def candidate(self,w,p,method,label):
        cal=Calibration('u',Trial(1000*MIB,1000*MIB,100,10),
            (Trial(800*MIB,800*MIB,105,12),),.1,None,None,0,'unit-data',method)
        phases=tuple(Phase(i,i+1,cal,label,v,f) for i,(v,f) in enumerate(zip(w,p)))
        return Candidate('a',1000,1000*MIB,phases)
    def test_reuse_label_cannot_force_high(self):
        c=self.candidate([200*MIB,190*MIB,500*MIB,500*MIB],[1,1,130000,1000],'reclaim','reuse')
        self.assertEqual(action_plan(c,100*MIB,'p',4).actions[0].mode,'reclaim')
    def test_turnover_label_cannot_force_reclaim(self):
        c=self.candidate([200*MIB,200*MIB,200*MIB,200*MIB],[1,1,1,1],'high','turnover')
        self.assertEqual(action_plan(c,100*MIB,'p',4).actions[0].mode,'high')
    def test_unsafe_method_not_substituted_for_numeric_choice(self):
        c=self.candidate([200*MIB]*4,[1]*4,'reclaim','reuse')
        self.assertEqual(action_plan(c,100*MIB,'p',4).actions[0].mode,'observe')
    def test_unmeasured_calibration_cannot_be_applied(self):
        c=self.candidate([200*MIB]*4,[1]*4,'high','reuse')
        phases=tuple(replace(p,calibration=replace(p.calibration,measurement_id='')) for p in c.phases)
        with self.assertRaises(ValueError):action_plan(replace(c,phases=phases),100*MIB,'p',4)
    def test_live_forecast_revision_keeps_spending_and_expiry(self):
        from coordinator import Coordinator
        c=self.candidate([200*MIB]*4,[1]*4,'high','reuse')
        b=Backend();p=plan();co=Coordinator([c],{'a':b},'p',10,plans=[p],deadline_s=8)
        co._accept([p],0);co.tick(0,lambda:100*MIB)
        spent=co.executors['a'].used
        update=replace(p,revision=1,actions=(Action(0,10,'high',300*MIB,300*MIB,300*MIB,'next'),))
        co.update_forecast([c],[update],7,.5);co.tick(.5,lambda:0)
        self.assertEqual(co.executors['a'].used,spent)
        self.assertEqual(co.executors['a'].plan.expires_s,10)
        self.assertIsNone(b.high)
        co.close(1)
    def test_cold_interval_must_cover_predicted_shortage(self):
        from coordinator import Coordinator
        c=replace(self.candidate([200*MIB]*4,[1]*4,'high','reuse'),cold_cap_bytes=100*MIB,cold_valid_until_s=7)
        b=Backend();p=plan();co=Coordinator([c],{'a':b},'p',10,plans=[p],deadline_s=8)
        co._accept([p],0);co.tick(0,lambda:100*MIB)
        self.assertEqual(co.executors['a'].plan.goal_bytes,0)
        self.assertFalse(b.calls)
        co.close(1)


if __name__=='__main__':unittest.main()
