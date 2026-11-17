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
    def __init__(self, backend, guard=None,foreground_feedback=None,*,clock=None):
        self.backend, self.guard = backend, guard or Guard()
        self.foreground_feedback=foreground_feedback
        self.clock=clock
        self.window_block_reason=None
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
        self.fixed_high_deferred=None

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
        if old.reclaim_window is not None:
            previous=old.reclaim_window;current=plan.reclaim_window
            if current is None:
                raise ValueError('prediction cannot remove the reclaim completion fence')
            if current.key==previous.key and current.key!='no_known_growth':
                if (current.finish_by_s>previous.finish_by_s or
                        current.protected_until_s<previous.protected_until_s):
                    raise ValueError('same peak cutoff cannot move later or protection end earlier')
            elif current.key!=previous.key and now<previous.protected_until_s:
                raise ValueError('prediction cannot reopen an active protected peak')
        if plan.sequence_high != old.sequence_high:
            raise ValueError('prediction cannot change the fixed sequence high')
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

    def _maintain_sequence_high(self, now, maintenance_only):
        fixed=self.plan.sequence_high
        if not self.plan.sequence_high_enabled:
            self._restore_high()
            return False
        if fixed.target_bytes < max(self.backend.protections()):
            self.close(now,'fixed_high_protection_conflict')
            return False
        if self.owned_high is not None:return True
        window=self.plan.reclaim_window
        at=max(now,self.clock()) if self.clock is not None else now
        if (window is not None and at+window.batch_bound_s>window.finish_by_s
                and at<window.protected_until_s):
            # Keep the selected WSS-based target; a deferred/restored cap must
            # not create a new synchronous contraction in the protected peak.
            return True
        # A blocking high write belongs on the existing per-app worker, not
        # on the coordinator's maintenance pass.
        if maintenance_only:return False
        original=self.backend.read_high()
        ceiling=min(self.backend.maximum(),original if original is not None else 2**63-1)
        if fixed.target_bytes>ceiling:
            self.close(now,'administrator_limit_conflict')
            return False
        usage=self.backend.usage()
        charge=down(max(0,usage-fixed.target_bytes))
        method_limit=dict(self.plan.method_budgets).get('high',self.budget_ceiling)
        allowance=min(self.remaining,max(0,method_limit-self.used_by_method['high']),self.guard.max_batch_bytes)
        if hasattr(self.backend,'rss'):
            allowance=min(allowance,max(0,self.backend.rss()-fixed.safe_rss_bytes))
        if charge>allowance:
            # Keep the chosen target immutable. Fine reclaim may first bring
            # usage close enough for one budgeted high write; never ramp high.
            if self.fixed_high_deferred!='budget_or_retention':
                self.log(now,'fixed_high_deferred',target_bytes=fixed.target_bytes,
                         attempted_reduction_bytes=charge,allowed_bytes=allowance)
                self.fixed_high_deferred='budget_or_retention'
            return True
        self.fixed_high_deferred=None
        self.saved_high=original;self.owned_high=fixed.target_bytes
        self.used+=charge;self.used_by_method['high']+=charge
        self.backend.write_high(fixed.target_bytes)
        if window is not None and self.clock is not None:
            finished=max(now,self.clock())
            if finished>window.finish_by_s:
                self.log(finished,'high_completion_overrun',finish_by_s=window.finish_by_s,
                         late_s=finished-window.finish_by_s)
                self._pause(finished,'high_completion_overrun')
                return False
        after=self.backend.usage()
        self.observed_release+=max(0,usage-after);self.previous_usage=after
        self.log(now,'fixed_high',target_bytes=fixed.target_bytes,reserved_bytes=charge,used_bytes=self.used)
        return True

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
        if self.clock is not None:now=max(now,self.clock())
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
            fixed=self.plan.sequence_high is not None
            if fixed:
                if self.state=='paused':return
                if not self._maintain_sequence_high(now,maintenance_only):return
                if self.plan.goal_bytes is not None:
                    own_need=max(0,self.plan.goal_bytes-self.observed_release)
                    needed_bytes=own_need if needed_bytes is None else min(needed_bytes,own_need)
                if action is None or action.mode=='observe':return
            elif action is None or action.mode=="observe":
                self._restore_high();return
            # Even when paused or budget exhausted, never retain an outdated low high.
            floor=up(max(action.safe_cgroup_bytes,*self.backend.protections()))
            relax_to=max(floor,action.high_bytes or 0) if action.mode=='high' else floor
            if not fixed and self.owned_high is not None and (relax_to>self.owned_high or action.mode!='high'):
                self._restore_high()
                self.log(now,"high_relaxed_for_growth",safe_cgroup_bytes=floor)
            if self.state=="paused":return
            usage=self.backend.usage()
            if not fixed and self.owned_high is not None and usage>self.owned_high:
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
            if not fixed:self._restore_high()
            amount=down(min(self.batch,allowance))
            if not amount:return
            window=self.plan.reclaim_window
            if window is not None:
                at=max(now,self.clock()) if self.clock is not None else now
                reason=('before_scheduled_start' if at<window.start_s else
                        'cannot_finish_before_growth' if at+window.batch_bound_s>window.finish_by_s else None)
                if reason is not None:
                    if reason!=self.window_block_reason:
                        self.log(at,'reclaim_window_blocked',reason=reason,finish_by_s=window.finish_by_s)
                        self.window_block_reason=reason
                    return
                self.window_block_reason=None
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
            if window is not None and self.clock is not None:
                finished=max(now,self.clock())
                if finished>window.finish_by_s:
                    self.log(finished,'reclaim_completion_overrun',finish_by_s=window.finish_by_s,
                             late_s=finished-window.finish_by_s)
                    self._pause(finished,'reclaim_completion_overrun')
                    return
            self.batch=min(self.guard.max_batch_bytes,self.batch*2)
            self.no_progress=0 if drop else self.no_progress+1
            if self.no_progress>=3:self._pause(now,"no_progress")
        except (OSError,RuntimeError):
            self.close(now,"backend_failure")
            raise
