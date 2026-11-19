"""Candidate forecast OUTPUT contract. No learned-model inputs are prescribed here."""
from dataclasses import dataclass, replace
import math
from signals import Rules,for_phase

PAGE = 4096
MIB = 1024 ** 2


def natural(value, name="bytes"):
    if type(value) is not int or not 0 <= value < 2**63:
        raise ValueError(f"{name}: nonnegative integer required")
    return value


def seconds(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError("finite nonnegative relative time required")
    return float(value)


def down(value, page=PAGE):
    return max(0, int(value)) // page * page


def up(value, page=PAGE):
    return (int(value) + page - 1) // page * page


@dataclass(frozen=True)
class Trial:
    """Comparable *measured* RSS/cgroup/PF/time point; not a WSS percentage."""
    rss_bytes: int
    cgroup_bytes: int
    pgfault: int
    elapsed_ms: float

    def __post_init__(self):
        for name in ("rss_bytes", "cgroup_bytes", "pgfault"):
            natural(getattr(self, name), name)
        # RSS is mapped residency; memory.current is cgroup ownership accounting.
        # Shared pages may be mapped here but charged elsewhere. Neither counter
        # can be derived from the other or assumed always larger.
        seconds(self.elapsed_ms)


@dataclass(frozen=True)
class Calibration:
    key: str
    baseline: Trial
    trials: tuple
    pf_relative: float
    pf_absolute: int | None
    time_relative: float | None
    time_absolute_ms: float
    measurement_id: str = ''
    method: str = ''

    def select(self):
        """Conservative contiguous descent: never leap past a rejected RSS level."""
        for n in (self.pf_relative, self.time_absolute_ms):
            seconds(n)
        if self.time_relative is not None:seconds(self.time_relative)
        if self.pf_absolute is not None:natural(self.pf_absolute)
        if not self.key:
            raise ValueError("calibration scope/phase key required")
        selected = self.baseline
        seen = set()
        for trial in sorted(self.trials, key=lambda t: -t.rss_bytes):
            if trial.rss_bytes >= self.baseline.rss_bytes or trial.rss_bytes in seen:
                raise ValueError("distinct lower RSS trial levels required")
            seen.add(trial.rss_bytes)
            # Positive extra faults with a zero baseline remain unvalidated
            # unless an absolute allowance was explicitly supplied.
            if self.baseline.pgfault==0 and trial.pgfault>0 and self.pf_absolute is None:break
            fault_limit = self.baseline.pgfault + max(self.pf_absolute or 0, self.baseline.pgfault * self.pf_relative)
            time_limit = (self.baseline.elapsed_ms + max(self.time_absolute_ms,self.baseline.elapsed_ms*self.time_relative)
                          if self.time_relative is not None else None)
            if trial.pgfault > fault_limit or (time_limit is not None and trial.elapsed_ms > time_limit):
                break
            # The high/reclaim floor is measured cgroup occupancy at this RSS,
            # including required non-RSS occupancy. Never copy a process RSS to high.
            if trial.cgroup_bytes > selected.cgroup_bytes:
                break
            selected = trial
        return selected


@dataclass(frozen=True)
class Phase:
    start_s: float
    end_s: float
    calibration: Calibration
    reuse: str  # legacy metadata ONLY; measured policy never reads this label
    wss_bytes: int = 0
    pgfault: int = 0  # diagnostic only: a total fault count is NOT distinct-new-page bytes
    mode: str | None = None  # Explicit fine-control decision; None keeps the legacy API.
    keep_rss_bytes: int = 0

    def __post_init__(self):
        seconds(self.start_s); seconds(self.end_s)
        if self.end_s <= self.start_s or self.reuse not in ("reuse", "turnover", "idle", "unknown"):
            raise ValueError("invalid phase")
        natural(self.wss_bytes); natural(self.pgfault)
        natural(self.keep_rss_bytes)
        if self.mode not in (None, 'reclaim', 'observe'):
            raise ValueError('invalid fine-control mode')


@dataclass(frozen=True)
class SequenceHigh:
    """One immutable high for the whole operation sequence, independent of allocation."""
    target_bytes: int
    safe_rss_bytes: int
    safe_cgroup_bytes: int
    calibration_key: str

    def __post_init__(self):
        for value in (self.target_bytes, self.safe_rss_bytes, self.safe_cgroup_bytes):
            natural(value)
        if self.target_bytes < self.safe_cgroup_bytes or not self.calibration_key:
            raise ValueError('fixed high requires a calibrated occupancy floor')


@dataclass(frozen=True)
class Action:
    start_s: float
    end_s: float
    mode: str
    safe_rss_bytes: int
    safe_cgroup_bytes: int
    high_bytes: int | None
    calibration_key: str

    def __post_init__(self):
        seconds(self.start_s); seconds(self.end_s)
        if self.end_s <= self.start_s or self.mode not in ("high", "reclaim", "observe"):
            raise ValueError("invalid action")
        natural(self.safe_rss_bytes); natural(self.safe_cgroup_bytes)
        if not self.calibration_key:
            raise ValueError("paired RSS/cgroup calibration key required")
        if self.high_bytes is not None:
            natural(self.high_bytes)
            if self.high_bytes < self.safe_cgroup_bytes:
                raise ValueError("high below calibrated occupancy")
        if self.mode == "high" and self.high_bytes is None:
            raise ValueError("high action requires an explicit target")


@dataclass(frozen=True)
class ReclaimWindow:
    key: str
    start_s: float
    finish_by_s: float
    protected_until_s: float
    batch_bound_s: float

    def __post_init__(self):
        for v in (self.start_s, self.finish_by_s, self.protected_until_s, self.batch_bound_s):
            seconds(v)
        if (not self.key or self.start_s > self.finish_by_s or
                self.finish_by_s > self.protected_until_s or self.batch_bound_s <= 0):
            raise ValueError('invalid reclaim completion window')


@dataclass(frozen=True)
class AppPlan:
    app: str
    plan_id: str
    revision: int
    issued_s: float
    expires_s: float
    budget_bytes: int
    actions: tuple
    source: str = "script_oracle_candidate"
    goal_bytes: int | None = None  # desired observed release, distinct from request budget
    method_budgets: tuple = ()
    sequence_high: SequenceHigh | None = None
    sequence_high_enabled: bool = True
    reclaim_window: ReclaimWindow | None = None

    def __post_init__(self):
        if not self.app or not self.plan_id:
            raise ValueError("app/plan identity required")
        natural(self.revision); natural(self.budget_bytes)
        if self.reclaim_window is not None and not isinstance(self.reclaim_window, ReclaimWindow):
            raise ValueError('validated reclaim window required')
        if self.sequence_high is not None and not isinstance(self.sequence_high, SequenceHigh):
            raise ValueError('validated sequence high required')
        if type(self.sequence_high_enabled) is not bool:
            raise ValueError('boolean sequence-high eligibility required')
        if self.sequence_high is not None and any(a.mode == 'high' for a in self.actions):
            raise ValueError('fixed sequence high cannot be mixed with rolling high actions')
        for method,amount in self.method_budgets:
            if method not in ('high','reclaim'):raise ValueError('invalid method budget')
            natural(amount)
        if self.goal_bytes is not None:
            natural(self.goal_bytes)
            if self.goal_bytes>self.budget_bytes:raise ValueError("goal exceeds request budget")
        seconds(self.issued_s); seconds(self.expires_s)
        if self.expires_s <= self.issued_s:
            raise ValueError("positive, fixed plan lifetime required")
        end = 0.0
        for action in self.actions:
            if action.start_s < end or action.end_s > self.expires_s-self.issued_s:
                raise ValueError("actions overlap, are unordered, or exceed expiry")
            end = action.end_s


@dataclass(frozen=True)
class Candidate:
    app: str
    protection: float
    usage_bytes: int
    phases: tuple
    background: bool = True
    pausable: bool = False
    postponed_growth_bytes: int = 0
    cold_cap_bytes: int | None = None
    cold_valid_until_s: float | None = None
    attempt_cap_bytes: int | None = None
    policy_rules: Rules = Rules()
    method_attempt_caps: tuple = ()
    sequence_high: SequenceHigh | None = None

    def __post_init__(self):
        if isinstance(self.protection, bool) or not math.isfinite(self.protection) or not 0 <= self.protection <= 32767:
            raise ValueError("v4.2 protection must be finite Q15, low first")
        natural(self.usage_bytes); natural(self.postponed_growth_bytes)
        if self.cold_cap_bytes is not None:natural(self.cold_cap_bytes,'external cold allowance')
        if self.cold_valid_until_s is not None:seconds(self.cold_valid_until_s)
        if self.attempt_cap_bytes is not None:natural(self.attempt_cap_bytes,'calibrated attempt ceiling')


def predicted_gap(free_bytes, reserve_bytes, curves):
    """Aligned absolute growth since the common anchor, not sums of separate maxima.

    Each curve [(time, additional_occupancy), ...] is piecewise constant, starts
    at time zero and may include self-release. Shared pages must be charged once.
    free_bytes is direct headroom, NOT MemAvailable (which includes cold cache).
    """
    natural(free_bytes); natural(reserve_bytes)
    times = {0.0}
    for curve in curves:
        if not curve or curve[0][0] != 0:
            raise ValueError("each curve must start at the same zero anchor")
        previous = -1.0
        for t, value in curve:
            seconds(t)
            if t <= previous or type(value) is not int:
                raise ValueError("ordered times and integer growth required")
            previous = t; times.add(float(t))
    totals = []
    for t in sorted(times):
        total = sum(next(v for at, v in reversed(curve) if at <= t) for curve in curves)
        totals.append((t, total))
    peak_t, peak = max(totals, key=lambda p: p[1])
    return dict(required_bytes=max(0, peak+reserve_bytes-free_bytes),
                peak_s=peak_t, growth_bytes=peak, aligned_totals=totals)


def action_plan(candidate, allocation, plan_id, expires_s, issued_s=0.0, revision=0, attempt_budget=None):
    """Reference rule inside the forecast-function facade; executor doesn't reclassify."""
    natural(allocation)
    actions = []
    for index,phase in enumerate(candidate.phases):
        if not isinstance(phase.calibration,Calibration) or not phase.calibration.measurement_id:
            raise ValueError('native measured calibration required; candidate floors are not accepted')
        floor = phase.calibration.select()
        mode = (phase.mode or 'observe') if candidate.sequence_high is not None else for_phase(candidate.phases,index,candidate.policy_rules)['mode']
        if not candidate.background:mode='observe'
        if mode!=phase.calibration.method:mode='observe'
        target = up(max(floor.cgroup_bytes, candidate.usage_bytes-allocation)) if mode == "high" else None
        safe_rss=max(floor.rss_bytes,phase.keep_rss_bytes) if candidate.sequence_high is not None else floor.rss_bytes
        actions.append(Action(phase.start_s, phase.end_s, mode, safe_rss,
                              floor.cgroup_bytes, target, phase.calibration.key))
    return AppPlan(candidate.app, plan_id, revision, issued_s, expires_s,
                   down(min(allocation if attempt_budget is None else attempt_budget,candidate.usage_bytes)),
                   tuple(actions),goal_bytes=down(allocation),method_budgets=candidate.method_attempt_caps,
                   sequence_high=candidate.sequence_high,sequence_high_enabled=candidate.background)


def revise_allocation(plan,candidate,total_goal):
    """Intra-app plan function owns target calculation, including on reassignment.

    The multi-app coordinator supplies an amount; it does not calculate high.
    Original lifetime, request ceiling and spending live in the executor.
    """
    natural(total_goal)
    if plan.app!=candidate.app or total_goal>plan.budget_bytes:raise ValueError('invalid allocation revision')
    if plan.sequence_high != candidate.sequence_high:
        raise ValueError('allocation cannot change the sequence high')
    actions=tuple(replace(a,high_bytes=max(a.safe_cgroup_bytes,candidate.usage_bytes-total_goal))
                  if a.mode=='high' else a for a in plan.actions)
    return replace(plan,revision=plan.revision+1,goal_bytes=total_goal,actions=actions)


def distribute(required_bytes, candidates, plan_id, expires_s, allow_freeze=False, release_by_s=None):
    """RankProp v4.2 scores ORDER applications; do not multiply budgets by them.

    Freeze credit must be a *certified reduction of the aligned peak growth* for
    this workload/plan, not an application's independent maximum or its RSS.
    """
    natural(required_bytes)
    release_by_s=expires_s if release_by_s is None else seconds(release_by_s)
    ordered = sorted(candidates, key=lambda c: (c.protection, c.app))
    if len({c.app for c in ordered}) != len(ordered):
        raise ValueError("unique application bindings required")
    caps = {}
    for c in ordered:
        # Reserve a future opportunity only if it begins BEFORE the deadline.
        # Executor still enforces the floor of the CURRENT phase on every batch.
        opportunities=[p for i,p in enumerate(c.phases) if p.start_s<release_by_s and c.background
                       and ((p.mode=='reclaim' and p.calibration.method=='reclaim') if c.sequence_high is not None else
                            (for_phase(c.phases,i,c.policy_rules)['mode']!='observe' and
                             for_phase(c.phases,i,c.policy_rules)['mode']==p.calibration.method))]
        caps[c.app]=max((down(max(0,c.usage_bytes-p.calibration.select().cgroup_bytes)) for p in opportunities),default=0)
        if c.cold_cap_bytes is not None:caps[c.app]=min(caps[c.app],down(c.cold_cap_bytes))
        if c.cold_valid_until_s is not None and c.cold_valid_until_s<release_by_s:caps[c.app]=0
    freezes = []
    adjusted = required_bytes
    if allow_freeze:
        for c in ordered:
            if adjusted <= sum(caps.values()):
                break
            if c.background and c.pausable and c.postponed_growth_bytes:
                freezes.append(c.app)
                adjusted = max(0, adjusted-c.postponed_growth_bytes)
    remaining, plans = adjusted, []
    for c in ordered:
        allocation = down(min(remaining, caps[c.app]))
        plans.append(action_plan(c, allocation, plan_id+":"+c.app, expires_s,
                                 attempt_budget=max(caps[c.app],c.attempt_cap_bytes or 0)))
        remaining -= allocation
    return dict(plans=plans, freeze_apps=freezes, required_after_stagger=adjusted,
                unfilled_bytes=remaining, capacity_bytes=sum(caps.values()))


def plan_machine(free_bytes,reserve_bytes,curves,candidates,plan_id,expires_s,allow_freeze=False,now_s=0.0):
    """Script forecast facade: all values below are OUTPUTs, not proposed ML inputs.

    Recompute aligned whole-machine growth after each eligible freeze. This
    prevents incorrectly crediting a background growth peak that occurs later
    than the machine peak, or losing an expected self-free while frozen.
    """
    active=dict(curves)
    seconds(now_s)
    gap=predicted_gap(free_bytes,reserve_bytes,list(active.values()))
    result=distribute(gap['required_bytes'],candidates,plan_id,expires_s,release_by_s=gap['peak_s'])
    freezes=[]
    if allow_freeze:
        for c in sorted(candidates,key=lambda c:(c.protection,c.app)):
            if not result['unfilled_bytes']:break
            if not (c.background and c.pausable and c.app in active):continue
            # A freeze postpones FUTURE growth. Already charged memory stays.
            current=next(v for at,v in reversed(active[c.app]) if at<=now_s)
            held=[(at,v) for at,v in active[c.app] if at<now_s]+[(now_s,current)]
            proposed=dict(active);proposed[c.app]=held
            changed=predicted_gap(free_bytes,reserve_bytes,list(proposed.values()))
            if changed['required_bytes']>=gap['required_bytes']:continue
            freezes.append(c.app);active=proposed;gap=changed
            result=distribute(gap['required_bytes'],candidates,plan_id,expires_s,release_by_s=gap['peak_s'])
    result.update(freeze_apps=freezes,whole_machine=gap)
    return result
