"""Evidence-gated per-app, per-method RSS/cgroup safety calibration."""
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from faults import FaultPolicy


@dataclass(frozen=True)
class SafetyPoint:
    app:str
    method:str
    retained_rss_bytes:int
    retained_cgroup_bytes:int
    requested_bytes:int
    observed_net_release_bytes:int
    baseline_pgfault:int
    current_pgfault:int
    baseline_task_ms:float
    current_task_ms:float
    baseline_path:str
    baseline_sha256:str
    trial_path:str
    trial_sha256:str
    workload_key:str
    compression_enabled:bool
    compressed_pages_observed:bool

    def verify(self,policy=None):
        policy=policy or FaultPolicy(.10,None)
        for path,expected in ((self.baseline_path,self.baseline_sha256),(self.trial_path,self.trial_sha256)):
            if hashlib.sha256(Path(path).read_bytes()).hexdigest()!=expected:raise ValueError('calibration evidence missing or changed')
        if self.method not in ('high','reclaim') or not self.compression_enabled:
            raise ValueError('matching method and compression calibration required')
        if min(self.retained_rss_bytes,self.retained_cgroup_bytes,self.requested_bytes,self.observed_net_release_bytes)<0:
            raise ValueError('invalid measured level')
        return policy.compare(self.baseline_pgfault,self.current_pgfault)


def select(points,policy=None):
    """Stop at the first rejected/undetermined level; never skip over it."""
    if not points:return None
    scope={(p.app,p.method,p.workload_key) for p in points}
    if len(scope)!=1:raise ValueError('one app/method/workload per safety selection')
    selected=None
    for point in sorted(points,key=lambda p:p.requested_bytes):
        result=point.verify(policy)
        if result['within_trial_limit'] is not True:break
        if point.observed_net_release_bytes<=0:break
        if selected is not None and (point.retained_rss_bytes>selected.retained_rss_bytes or
                                     point.retained_cgroup_bytes>selected.retained_cgroup_bytes):break
        selected=point
    return selected
