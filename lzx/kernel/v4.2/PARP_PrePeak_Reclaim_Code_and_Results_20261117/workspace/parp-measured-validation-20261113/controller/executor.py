"""Userspace plan executor. Works with native cgroup files or an injected plant."""
from dataclasses import dataclass
import errno
from plan import AppPlan, PAGE, MIB, natural, seconds, up, down


@dataclass(frozen=True)
class Guard:
    # Explicit engineering TRIAL settings, not a validated optimal profile.
    interval_s: float = 1.0
    refault_mild: int = 128
    refault_severe: int = 2048
    psi_mild: float = 0.10
    psi_severe: float = 0.50
    quiet_windows: int = 2
    repeated_limit: int = 3
    initial_batch_bytes: int = 8*MIB
    max_batch_bytes: int = 32*MIB
    tick_s: float = 0.1
    high_wait_s: float = 1.0

    def __post_init__(self):
        for v in (self.interval_s, self.tick_s,self.high_wait_s):
            if seconds(v) <= 0: raise ValueError("positive guard interval required")
        for v in (self.refault_mild,self.refault_severe,self.quiet_windows,self.repeated_limit,self.initial_batch_bytes,self.max_batch_bytes):
            natural(v)
        if not (0 < self.quiet_windows and 0 < self.repeated_limit and 0 <= self.psi_mild < self.psi_severe <= 1
                and self.refault_mild < self.refault_severe and PAGE <= self.initial_batch_bytes <= self.max_batch_bytes <= 32*MIB):
            raise ValueError("invalid guard/batch settings")


class Executor:
    def __init__(self, backend, guard=None,foreground_feedback=None):
        self.backend, self.guard = backend, guard or Guard()
        self.foreground_feedback=foreground_feedback
        self.foreground_bad=False
        self.pause_started=None
        self.plan = None
        self.state = "idle"
        self.used = 0
        self.used_by_method={'high':0,'reclaim':0}
        self.budget_ceiling = 0
        self.events = []
        self.retired_ids = set()
        self.saved_high = self.owned_high = None
        self.saved_freeze = None
        self.high_issued = set()
        self.batch = self.guard.initial_batch_bytes
        self.last_tick = -float("inf")
        self.last_feedback = None
        self.pauses = self.quiet = self.no_progress = 0
        self.observed_release=0
        self.previous_usage=None
        self.high_progress_time=None
        self.high_progress_usage=None

    def log(self, now, event, **values):
        item = dict(time_s=round(now,6),event=event,app=self.plan.app if self.plan else None,**values)
        self.events.append(item)
        return item

    def update(self, plan, now):
        if not isinstance(plan,AppPlan): raise TypeError("validated AppPlan required")
        seconds(now)
        if self.plan is None:
            if plan.plan_id in self.retired_ids: raise ValueError("retired plan cannot refill its budget")
            if now < plan.issued_s or now >= plan.expires_s: raise ValueError("plan is not currently valid")
            self.plan = plan
            self.used=0
            self.used_by_method={'high':0,'reclaim':0}
            self.high_issued.clear()
            self.pauses=self.quiet=self.no_progress=0
            self.batch=self.guard.initial_batch_bytes
            self.last_tick=-float("inf")
            self.observed_release=0
            self.previous_usage=self.backend.usage()
            self.budget_ceiling = plan.budget_bytes
            self.state = "active"
            self.last_feedback = self._feedback(now)
            return self.log(now,"accepted",budget_bytes=self.budget_ceiling,expires_s=plan.expires_s)
        old = self.plan
        if now>=old.expires_s:raise ValueError("expired plan cannot be refreshed")
        if plan.plan_id != old.plan_id or plan.app != old.app:
            raise ValueError("finish active plan before assigning a different identity")
        if plan.revision <= old.revision or plan.issued_s != old.issued_s or plan.expires_s != old.expires_s:
            raise ValueError("revision must increase; anchor and original expiry cannot change")
        if plan.budget_bytes > self.budget_ceiling:
            raise ValueError("prediction update cannot refill/increase the original budget")
        old_methods=dict(old.method_budgets);new_methods=dict(plan.method_budgets)
        if any(new_methods.get(m,plan.budget_bytes)>old_methods.get(m,self.budget_ceiling) for m in ('high','reclaim')):
            raise ValueError('prediction cannot refill/increase method budget')
        # A reduced ceiling never erases amounts already reserved.
        self.budget_ceiling = plan.budget_bytes
        self.plan = plan
        return self.log(now,"prediction_updated",used_bytes=self.used,remaining_bytes=self.remaining)

    @property
    def remaining(self):
        return max(0,self.budget_ceiling-self.used)

    def _feedback(self, now):
        refault, psi_total = self.backend.feedback()
        return now,refault,psi_total

    def _restore_high(self):
        if self.owned_high is not None:
            # Administrator changes win; ABA cannot be detected with this ABI.
            if self.backend.read_high() == self.owned_high:
                self.backend.write_high(self.saved_high)
            self.saved_high = self.owned_high = None
            self.high_progress_time=self.high_progress_usage=None

    def freeze(self, now):
        self.backend.check()
        if self.saved_freeze is None:
            self.saved_freeze = self.backend.read_freeze()
            if self.saved_freeze == 0:
                self.backend.write_freeze(1)
                self.log(now,"frozen",freed_bytes_claimed=0)

    def thaw(self, now):
        if self.saved_freeze is not None:
            if self.saved_freeze == 0 and self.backend.read_freeze() == 1:
                self.backend.write_freeze(0)
                self.log(now,"thawed")
            self.saved_freeze = None

    def close(self, now, reason="closed"):
        errors=[]
        try:self._restore_high()
        except (OSError,RuntimeError) as error:errors.append(str(error))
        try:self.thaw(now)
        except (OSError,RuntimeError) as error:errors.append(str(error))
        if self.plan:
            self.retired_ids.add(self.plan.plan_id)
            self.log(now,"terminated",reason=reason,used_bytes=self.used,remaining_bytes=self.remaining,cleanup_errors=errors)
        self.plan=None
        self.state="terminated"

    def _pause(self, now, reason):
        self._restore_high()
        self.pauses += 1
        self.quiet=0
        if self.pauses >= self.guard.repeated_limit:
            self.close(now,"repeated_"+reason)
        else:
            self.state="paused"
            self.pause_started=now
            self.log(now,"paused",reason=reason,used_bytes=self.used)

    def _guard(self, now):
        if self.last_feedback is None:
            self.last_feedback=self._feedback(now)
            return
        last, r0, p0=self.last_feedback
        if now-last+1e-9 < self.guard.interval_s:return
        current=self._feedback(now)
        _,r,p=current
        dr=max(0,r-r0)
        psi=max(0.0,p-p0)/max(1e-9,now-last)
        self.last_feedback=current
        self.log(now,"feedback",refault_delta=dr,psi_fraction=psi,state=self.state)
        # Counter reset/rebinding is not silently interpreted as a quiet window.
        if r < r0 or p < p0:
            self.close(now,"feedback_counter_reset");return
        if dr >= self.guard.refault_severe or psi >= self.guard.psi_severe:
            self.close(now,"severe_feedback");return
        if dr > self.guard.refault_mild or psi > self.guard.psi_mild:
            if self.state=="paused":self.quiet=0
            else:self._pause(now,"feedback")
        elif self.state=="paused" and not self.foreground_bad:
            self.quiet += 1
            if self.quiet >= self.guard.quiet_windows:
                self.state="active"
                self.batch=self.guard.initial_batch_bytes
                self.no_progress=0
                waited=now-self.pause_started if self.pause_started is not None else 0
                self.pause_started=None
                self.log(now,"resumed",remaining_bytes=self.remaining,expires_s=self.plan.expires_s,
                         protection_pause_s=waited)

    def tick(self, now, needed_bytes=None, *, maintenance_only=False):
        seconds(now)
        if needed_bytes is not None:natural(needed_bytes)
        if self.plan is None:return
        if now >= self.plan.expires_s:
            self.close(now,"expired");return
        try:
            self.backend.check()
            if self.owned_high is not None and self.backend.read_high()!=self.owned_high:
                self.owned_high=self.saved_high=None
                self.close(now,"external_high_changed");return
            self.foreground_bad=bool(self.foreground_feedback(now)) if self.foreground_feedback is not None else False
            if self.foreground_bad:
                if self.state!='paused':self._pause(now,'foreground_response')
                else:self.quiet=0
            self._guard(now)
            if self.plan is None:return
            usage=self.backend.usage()
            self.observed_release+=max(0,self.previous_usage-usage)
            self.previous_usage=usage
            # This is observed occupancy decline, not a kernel-returned reclaim
            # count. A whole-machine monitor supplied by the coordinator is better.
            if self.plan.goal_bytes is not None:
                own_need=max(0,self.plan.goal_bytes-self.observed_release)
                needed_bytes=own_need if needed_bytes is None else min(needed_bytes,own_need)
            age=now-self.plan.issued_s
            action=next((a for a in self.plan.actions if a.start_s<=age<a.end_s),None)
            if action is None or action.mode=="observe":
                self._restore_high();return
            # Even when paused or budget exhausted, never retain an outdated low high.
            floor=up(max(action.safe_cgroup_bytes,*self.backend.protections()))
            relax_to=max(floor,action.high_bytes or 0) if action.mode=='high' else floor
            if self.owned_high is not None and (relax_to>self.owned_high or action.mode!='high'):
                self._restore_high()
                self.log(now,"high_relaxed_for_growth",safe_cgroup_bytes=floor)
            if self.state=="paused":return
            usage=self.backend.usage()
            if self.owned_high is not None and usage>self.owned_high:
                if usage < self.high_progress_usage:
                    self.high_progress_usage=usage
                    self.high_progress_time=now
                if now-self.high_progress_time>=self.guard.high_wait_s:
                    self._pause(now,"high_no_progress")
                return # One bounded high step must settle before the next.
            # Retain a settled high while demand stays unchanged. Do not toggle
            # max after every successful batch. Growth relaxation, pause and
            # expiry above still run even with zero need or exhausted budget.
            if needed_bytes==0:return
            if maintenance_only:return
            if now-self.last_tick+1e-9<self.guard.tick_s:return
            self.last_tick=now
            allowance=min(self.remaining,max(0,usage-floor))
            method_limit=dict(self.plan.method_budgets).get(action.mode,self.budget_ceiling)
            allowance=min(allowance,max(0,method_limit-self.used_by_method[action.mode]))
            if hasattr(self.backend,'rss'):
                allowance=min(allowance,max(0,self.backend.rss()-action.safe_rss_bytes))
            if needed_bytes is not None:allowance=min(allowance,needed_bytes)
            if action.mode=="high":
                if allowance<PAGE:return
                step_allowance=min(allowance,self.batch)
                target=up(max(floor,action.high_bytes,usage-step_allowance))
                original=self.saved_high if self.owned_high is not None else self.backend.read_high()
                ceiling=min(self.backend.maximum(),original if original is not None else 2**63-1)
                if target>ceiling:
                    self.close(now,"administrator_limit_conflict");return
                charge=down(max(0,usage-target))
                if charge==0:return
                self.used+=charge # charge attempted reduction, not invented actual reclamation
                self.used_by_method['high']+=charge
                if self.owned_high is None:self.saved_high=original
                self.owned_high=target
                self.high_progress_time=now
                self.high_progress_usage=usage
                self.backend.write_high(target)
                self.log(now,"high",target_bytes=target,reserved_bytes=charge,used_bytes=self.used)
                self.batch=min(self.guard.max_batch_bytes,self.batch*2)
                return
            self._restore_high()
            amount=down(min(self.batch,allowance))
            if not amount:return
            self.used+=amount # EAGAIN/error never refunds an uncertain request
            self.used_by_method['reclaim']+=amount
            partial=False
            try:self.backend.reclaim(amount)
            except OSError as error:
                if error.errno!=errno.EAGAIN:raise
                partial=True
            after=self.backend.usage()
            drop=max(0,usage-after)
            self.observed_release+=drop
            self.previous_usage=after
            self.log(now,"reclaim",requested_bytes=amount,observed_usage_drop_bytes=drop,
                     partial=partial,used_bytes=self.used,remaining_bytes=self.remaining)
            self.batch=min(self.guard.max_batch_bytes,self.batch*2)
            self.no_progress=0 if drop else self.no_progress+1
            if self.no_progress>=3:self._pause(now,"no_progress")
        except (OSError,RuntimeError):
            self.close(now,"backend_failure")
            raise
