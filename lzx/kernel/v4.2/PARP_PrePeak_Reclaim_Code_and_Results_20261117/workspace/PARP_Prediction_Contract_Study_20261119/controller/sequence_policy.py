"""Fixed sequence envelope plus fine reclaim; no WSS-curve class gates reclamation."""
from plan import SequenceHigh, natural, seconds, up

POLICY_VERSION = 'fixed-sequence-high-pre-growth-reclaim-v2'


def validate_windows(windows):
    if not windows:
        raise ValueError('measured sequence windows required')
    previous_end = None
    for row in windows:
        start, end = seconds(row['start_s']), seconds(row['end_s'])
        if end <= start or row.get('valid') is False:
            raise ValueError('valid measured sequence interval required')
        if previous_end is not None and abs(start - previous_end) > .001:
            raise ValueError('sequence windows have a gap or overlap')
        natural(row['wss_bytes']); natural(row['pgfault'])
        previous_end = end


def sequence_high(windows, floors, initial_usage, high_cap, calibration_key):
    """Freeze the full-sequence peak, calibrated total occupancy and request ceiling."""
    validate_windows(windows)
    natural(initial_usage); natural(high_cap)
    if not floors:
        raise ValueError('measured sequence retention required')
    rss = max(row.rss_bytes for row in floors)
    cgroup = max(row.cgroup_bytes for row in floors)
    target = up(max(max(row['wss_bytes'] for row in windows), cgroup,
                    max(0, initial_usage - high_cap)))
    return SequenceHigh(target, rss, cgroup, calibration_key)


def reclaim_decision(windows, at, cold_bytes, cold_valid_until, lead_s):
    """Return timing/working-memory constraints; the executor handles gap and feedback.

    Stable, falling, rising, idle and oscillating curves all enter the same path.
    A cold certificate is valid only before its next reuse; lead time stops new
    requests before that boundary. Fault counts remain diagnostics, not proof of
    cold pages or a substitute for live refault/PSI/foreground protection.
    """
    seconds(at); natural(cold_bytes); seconds(cold_valid_until); seconds(lead_s)
    stop = max(0., cold_valid_until - lead_s)
    nearby = [w for w in windows if w['end_s'] > at and w['start_s'] < at + lead_s]
    keep_rss = max((w['wss_bytes'] for w in nearby), default=0)
    allowed = cold_bytes > 0 and at < stop and bool(nearby)
    return dict(mode='reclaim' if allowed else 'observe', stop_s=stop,
                keep_rss_bytes=keep_rss,
                reason='cold_allowance_with_live_gap_and_feedback' if allowed else 'no_valid_cold_window')
