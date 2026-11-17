"""Compare the SAME completed workload against strategy-off, not against zero faults."""
from dataclasses import dataclass
from plan import natural,seconds


@dataclass(frozen=True)
class FaultPolicy:
    relative_limit:float=.10
    absolute_limit_when_zero:int|None=None

    def __post_init__(self):
        seconds(self.relative_limit)
        if self.absolute_limit_when_zero is not None:natural(self.absolute_limit_when_zero)

    def compare(self,baseline,current):
        natural(baseline);natural(current)
        extra=current-baseline
        if baseline:
            ratio=extra/baseline
            accepted=extra<=baseline*self.relative_limit
        else:
            ratio=None
            accepted=(extra<=0) if self.absolute_limit_when_zero is None else extra<=self.absolute_limit_when_zero
            if extra>0 and self.absolute_limit_when_zero is None:accepted=None
        return dict(baseline=baseline,current=current,additional=extra,relative_growth=ratio,
                    within_trial_limit=accepted,relative_trial_limit=self.relative_limit,
                    absolute_limit_when_zero=self.absolute_limit_when_zero,
                    needs_absolute_limit=(baseline==0 and extra>0 and self.absolute_limit_when_zero is None))
