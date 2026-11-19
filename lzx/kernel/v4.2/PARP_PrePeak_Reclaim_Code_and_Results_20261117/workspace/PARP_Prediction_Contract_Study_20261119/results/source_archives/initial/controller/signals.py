"""Numeric WSS/pgfault policy. It never accepts a scenario name or reuse label."""
from dataclasses import dataclass
import math


@dataclass(frozen=True)
class Rules:
    # Configurable engineering trial values; not claimed optimal thresholds.
    lookahead_s:float=4.0
    stable_relative:float=.15
    turnover_rise_relative:float=.20
    reuse_faults_per_working_page:float=.10
    turnover_faults_per_working_page:float=.50
    page_bytes:int=4096


def classify(wss_bytes,pagefault,*,rules=None):
    rules=rules or Rules()
    if len(wss_bytes)!=len(pagefault):raise ValueError('matched WSS/pgfault windows required')
    if any(type(v)is not int or v<0 for v in (*wss_bytes,*pagefault)):
        raise ValueError('measured nonnegative WSS bytes and fault counts required')
    if len(wss_bytes)<2:return dict(mode='observe',reason='insufficient_measured_windows')
    peak=max(wss_bytes)
    if not peak:return dict(mode='observe',reason='no_positive_working_set')
    split=max(1,len(wss_bytes)//2)
    early=max(wss_bytes[:split]);late=max(wss_bytes[split:])
    early_pf=sum(pagefault[:split]);late_pf=sum(pagefault[split:])
    working_pages=max(1,sum(wss_bytes)/rules.page_bytes)
    density=sum(pagefault)/working_pages
    envelope_change=abs(late-early)/peak
    result=dict(mode='observe',reason='ambiguous_numeric_evidence',early_wss_peak=early,late_wss_peak=late,
                fault_density=density,envelope_change=envelope_change)
    # Compare both halves' demand envelope, allowing genuine idle samples in
    # between accesses. A total pagefault count is a clue, not distinct-new-page bytes.
    if envelope_change<=rules.stable_relative and density<=rules.reuse_faults_per_working_page:
        result.update(mode='high',reason='stable_wss_envelope_low_faults')
    elif (len(wss_bytes)>=3 and late>early and
          late-early>=max(rules.page_bytes,early*rules.turnover_rise_relative) and late_pf>early_pf and
          late_pf/max(1,sum(wss_bytes[split:])/rules.page_bytes)>=rules.turnover_faults_per_working_page):
        result.update(mode='reclaim',reason='wss_rise_with_fault_burst')
    return result


def for_phase(phases,index,rules=None):
    rules=rules or Rules()
    anchor=phases[index].start_s
    future=[p for p in phases[index:] if p.start_s<anchor+rules.lookahead_s]
    return classify([p.wss_bytes for p in future],[p.pgfault for p in future],rules=rules)
