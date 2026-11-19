"""Service every app's safety/timers, then allocate by protection under one goal."""
from plan import distribute,down,natural,revise_allocation
from executor import Executor
from concurrent.futures import ThreadPoolExecutor


class Coordinator:
    def __init__(self,candidates,backends,plan_id,expires_s,guard=None,*,plans=None,
                 deadline_s=None,safe_rates=None,parallel=False,foreground_feedback=None,clock=None):
        self.candidates=sorted(candidates,key=lambda c:(c.protection,c.app))
        if len({c.app for c in candidates})!=len(candidates):raise ValueError('duplicate app')
        if set(backends)!={c.app for c in candidates}:raise ValueError('binding mismatch')
        self.backends=backends
        self.plan_id=plan_id
        self.expires=expires_s
        self.guard=guard
        self.foreground_feedback=foreground_feedback
        self.clock=clock
        self.deadline=expires_s if deadline_s is None else deadline_s
        self.rates=dict(safe_rates or {})
        for value in self.rates.values():
            if natural(value,'safe release rate')==0:raise ValueError('safe release rate must be positive')
        self.executors={}
        self.errors=[]
        self.events=[]
        self.prepared=plans
        self.deadline_recorded=False
        self.pool=ThreadPoolExecutor(max_workers=max(1,len(candidates))) if parallel else None
        self.inflight={}
        self.pending_quota={}
        self.app_for_executor={}
        self.pending_forecasts={}

    def _accept(self,plans,now):
        if {p.app for p in plans}!={c.app for c in self.candidates}:raise ValueError('plan/app mismatch')
        for plan in plans:
            e=Executor(self.backends[plan.app],self.guard.get(plan.app) if isinstance(self.guard,dict) else self.guard,
                       self.foreground_feedback,clock=self.clock)
            e.update(plan,now)
            self.executors[plan.app]=e
            self.app_for_executor[e]=plan.app

    def _busy(self,e):
        future=self.inflight.get(e)
        if future is None:return False
        if not future.done():return True
        self.inflight.pop(e)
        self.pending_quota.pop(e,None)
        try:future.result()
        except (RuntimeError,OSError) as error:self.errors.append((self.app_for_executor[e],str(error)))
        if e in self.pending_forecasts and e.plan is not None:
            from dataclasses import replace
            plan,now=self.pending_forecasts.pop(e)
            if now<e.plan.expires_s:
                e.update(replace(plan,revision=e.plan.revision+1,budget_bytes=min(plan.budget_bytes,e.budget_ceiling)),now)
        return False

    def update_forecast(self,candidates,plans,deadline_s,now):
        """Rolling numeric forecasts preserve original plan identity, budget and expiry."""
        if {c.app for c in candidates}!=set(self.executors):raise ValueError('live binding changed')
        self.candidates=sorted(candidates,key=lambda c:(c.protection,c.app))
        if deadline_s!=self.deadline:self.deadline_recorded=False
        self.deadline=deadline_s
        from dataclasses import replace
        for plan in plans:
            e=self.executors[plan.app]
            if e.plan is None:continue
            if plan.plan_id!=e.plan.plan_id or plan.issued_s!=e.plan.issued_s or plan.expires_s!=e.plan.expires_s:
                raise ValueError('forecast changed original lifetime/identity')
            if plan.sequence_high!=e.plan.sequence_high:
                raise ValueError('forecast cannot change the fixed sequence high')
            if plan.budget_bytes>e.budget_ceiling:raise ValueError('forecast cannot refill budget')
            if self._busy(e):self.pending_forecasts[e]=(plan,now)
            else:e.update(replace(plan,revision=e.plan.revision+1),now)

    def _safe_tick(self,e,now,need=None,maintenance=False):
        if self._busy(e):return
        if e.plan is None:return
        if self.pool is not None and not maintenance:
            quota=max(0,(e.plan.goal_bytes or 0)-e.observed_release) if e.plan else 0
            self.pending_quota[e]=quota if need is None else min(quota,need)
            def execute():
                at=max(now,self.clock()) if self.clock is not None else now
                return e.tick(at,need)
            self.inflight[e]=self.pool.submit(execute)
            return
        app=e.plan.app if e.plan else None
        try:e.tick(now,need,maintenance_only=maintenance)
        except (RuntimeError,OSError) as error:self.errors.append((app,str(error)))

    def _rebalance(self,now,need):
        busy={e for e in self.executors.values() if self._busy(e)}
        remaining=max(0,need-sum(self.pending_quota.get(e,0) for e in busy))
        for c in self.candidates:
            e=self.executors[c.app]
            if e in busy:continue
            if e.plan is None:continue
            future=[a for a in e.plan.actions if a.end_s>now and a.start_s<self.deadline and a.mode!='observe']
            room=max((max(0,self.backends[c.app].usage()-a.safe_cgroup_bytes) for a in future),default=0)
            pending=max(0,self.backends[c.app].usage()-e.owned_high) if e.owned_high is not None and e.plan.sequence_high is None else 0
            method_limits=dict(e.plan.method_budgets)
            method_room=max((max(0,method_limits.get(a.mode,e.budget_ceiling)-e.used_by_method[a.mode])
                             for a in future),default=0)
            cap=min(e.remaining+pending,method_room+pending,room,max(0,e.budget_ceiling-e.observed_release))
            if not c.background or e.state!='active':cap=0
            if c.cold_cap_bytes is not None:cap=min(cap,max(0,c.cold_cap_bytes-e.observed_release))
            if c.cold_valid_until_s is not None and (now>=c.cold_valid_until_s or self.deadline>c.cold_valid_until_s):cap=0
            if c.app in self.rates:cap=min(cap,int(self.rates[c.app]*max(0,self.deadline-now)))
            assigned=down(min(remaining,cap))
            total_goal=min(e.budget_ceiling,e.observed_release+assigned)
            if e.plan.goal_bytes!=total_goal:
                old_goal=e.plan.goal_bytes
                e.update(revise_allocation(e.plan,c,total_goal),now)
                self.events.append(dict(time_s=now,event='allocated',app=c.app,remaining_goal_bytes=assigned,
                                        previous_total_goal_bytes=old_goal,total_goal_bytes=total_goal,
                                        attempt_budget_bytes=e.budget_ceiling,already_reserved_bytes=e.used))
            remaining-=assigned
        return remaining

    def tick(self,now,remaining_need):
        need=natural(remaining_need(),'remaining whole-machine deficit')
        if not self.executors:
            plans=self.prepared
            if plans is None:plans=distribute(need,self.candidates,self.plan_id,self.expires,release_by_s=self.deadline)['plans']
            self._accept(plans,now)
        # Safety processing must never be starved by another active donor.
        for e in self.executors.values():self._safe_tick(e,now,maintenance=True)
        need=natural(remaining_need())
        if now>=self.deadline:
            if not self.deadline_recorded:
                self.events.append(dict(time_s=now,event='release_deadline',unfilled_bytes=need,
                                        deadline_s=self.deadline,
                                        inflight_apps=[self.app_for_executor[e] for e in list(self.inflight) if self._busy(e)]))
                self.deadline_recorded=True
            for e in self.executors.values():self._safe_tick(e,now,0)
            return
        self._rebalance(now,need)
        for c in self.candidates:self._safe_tick(self.executors[c.app],now,natural(remaining_need()))

    def close(self,now):
        if self.pool is not None:
            # Kernel writes cannot be forcibly cancelled. A production supervisor
            # still needs to handle a service killed while a syscall is blocked.
            self.pool.shutdown(wait=True)
            self.pool=None
        for e in list(self.inflight):self._busy(e)
        for e in self.executors.values():e.close(now,'coordinator_exit')
