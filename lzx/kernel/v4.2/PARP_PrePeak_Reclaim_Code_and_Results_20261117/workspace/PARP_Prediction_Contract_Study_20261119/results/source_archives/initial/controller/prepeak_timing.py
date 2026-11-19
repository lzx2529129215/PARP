"""Curve-derived growth periods and measured, whole-gap reclaim scheduling.

The prominence/quiet defaults are experimental signal filters, not a learned
operation recognizer or a claim that a universal prediction horizon was found.
"""
from dataclasses import dataclass
import math

from plan import PAGE, natural, seconds

PROMINENCE_FRACTION = .05
QUIET_S = 1.0
UPDATE_S = .5


@dataclass(frozen=True)
class GrowthPeriod:
    key: str
    start_s: float
    end_s: float


def growth_periods(occupancy, prominence_fraction=PROMINENCE_FRACTION, quiet_s=QUIET_S):
    """Backtrack a significant rise to its preceding low; retain elevated plateaus.

    A short dip cannot reopen reclaim between two parts of the same burst. A
    return near the preceding low must persist for quiet_s before protection
    ends. Unfinished elevated periods remain protected through the known trace.
    Only occupancy samples are read, never operation names or scheduled times.
    """
    if not 0 < prominence_fraction <= 1 or seconds(quiet_s) <= 0:
        raise ValueError('positive prominence and quiet interval required')
    if not occupancy:
        raise ValueError('measured occupancy required')
    previous = -1.
    for row in occupancy:
        if seconds(row['time_s']) <= previous:
            raise ValueError('strict occupancy times required')
        natural(row['total']); previous = row['time_s']
    threshold = max(PAGE, math.ceil((max(r['total'] for r in occupancy) -
                                    min(r['total'] for r in occupancy))*prominence_fraction))
    base = occupancy[0]; active = None; quiet_from = None; result = []
    for row in occupancy[1:]:
        if active is None:
            if row['total'] <= base['total']:
                base = row
            elif row['total'] - base['total'] >= threshold:
                active = base; quiet_from = None
        elif row['total'] <= active['total'] + threshold:
            if quiet_from is None:
                quiet_from = row['time_s']
            if row['time_s'] - quiet_from >= quiet_s:
                result.append(GrowthPeriod(str(len(result)), active['time_s'], row['time_s']))
                base = row; active = None; quiet_from = None
        else:
            quiet_from = None
    if active is not None:
        result.append(GrowthPeriod(str(len(result)), active['time_s'], occupancy[-1]['time_s']))
    return tuple(result), threshold


def remaining_growth(forecast, at):
    """Subtract growth already realized since this forecast's issue time once.

    Re-evaluating feedback between 0.5-s forecasts must not charge an entire
    cached rise against free space that already paid for part of that rise.
    """
    curves = forecast.curves
    times = sorted({t for curve in curves.values() for t, _ in curve})
    def total(t):
        return sum(next(v for x, v in reversed(c) if x <= t) for c in curves.values())
    current = total(max(forecast.issued_s, at))
    return max(0, max([current] + [total(t) for t in times if t > at]) - current)


@dataclass(frozen=True)
class ReleaseProfile:
    bytes_per_s: int
    batch_bound_s: float
    credited_bytes: int
    source: str = 'conservative calibration occupancy-drop estimate; not exact old-page speed'


def release_profile(point, trial, guard, lead_s):
    """Use credited release, syscall time AND feedback/batch gaps; never request/s.

    Both the slowest positive batch and the complete measured request span
    bound the estimate. Failed/zero-drop writes receive no released-byte credit.
    """
    writes = [w for w in trial.get('calibration_writes', [])
              if w['name'] == 'memory.reclaim' and w.get('end_ns', 0) >= w['start_ns']]
    bound = max(lead_s, max(((w['end_ns']-w['start_ns'])/1e9 + UPDATE_S
                            for w in writes), default=lead_s))
    positive = []
    for w in writes:
        credit = min(int(w['value']), max(0, w['before_bytes']-w['after_bytes']))
        if credit and not w.get('error'):
            positive.append((credit, (w['end_ns']-w['start_ns'])/1e9 +
                             guard.tick_s + UPDATE_S))
    credited = min(sum(c for c, _ in positive), point.observed_net_release_bytes,
                   point.requested_bytes)
    if not writes or not positive or credited <= 0:
        return ReleaseProfile(0, bound, 0)
    span = (max(w['end_ns'] for w in writes)-min(w['start_ns'] for w in writes))/1e9
    rate = min(credited/(span+guard.tick_s+UPDATE_S), *(c/d for c, d in positive))
    return ReleaseProfile(max(0, int(rate)), bound, credited)


@dataclass(frozen=True)
class TimingState:
    key: str
    start_s: float
    finish_by_s: float
    protected_until_s: float
    estimated_total_s: float
    remaining_bytes: int
    unallocated_bytes: int
    feasible: bool
    reason: str


class PrepeakScheduler:
    def __init__(self, occupancy, margin_s=UPDATE_S):
        self.periods, self.prominence_bytes = growth_periods(occupancy)
        self.margin_s = seconds(margin_s)
        self.state = None

    def update(self, now, need, profiles, capacities, forecast_end_s):
        seconds(now); natural(need)
        period = next((p for p in self.periods if p.end_s > now and p.start_s <= forecast_end_s), None)
        old = self.state
        # A later forecast cannot move an active period's cutoff to a later
        # peak, nor declare its protection over before the locked end.
        if old is not None and now < old.protected_until_s:
            if period is None or period.start_s >= old.protected_until_s:
                period = GrowthPeriod(old.key, old.finish_by_s, old.protected_until_s)
            else:
                period = GrowthPeriod(old.key, min(old.finish_by_s, period.start_s),
                                      max(old.protected_until_s, period.end_s))
        if period is None:
            self.state = TimingState('no_known_growth', now, now, now, 0., need, need,
                                     need == 0, 'no_known_growth')
            return self.state
        left = need; duration = 0.
        for app, capacity in capacities.items():
            profile = profiles.get(app)
            if profile is None or profile.bytes_per_s <= 0:
                continue
            assigned = min(left, capacity)
            # Summing app durations is conservative even with parallel donors.
            duration += assigned/profile.bytes_per_s
            left -= assigned
        duration += max((p.batch_bound_s for p in profiles.values()), default=0.)
        start = max(0., period.start_s-duration-self.margin_s)
        if old is not None and old.key == period.key:
            start = min(start, old.start_s)
        feasible = left == 0 and (need == 0 or now+duration+self.margin_s <= period.start_s)
        reason = ('peak_protected' if now >= period.start_s else
                  'no_gap' if need == 0 else
                  'insufficient_capacity_or_time' if not feasible else 'scheduled_before_growth')
        self.state = TimingState(period.key, start, period.start_s, period.end_s,
                                 duration, need, left, feasible, reason)
        return self.state
