"""Candidate fix: distinguish a NEW peak after the old protection has expired."""
from prepeak_timing import PrepeakScheduler, GrowthPeriod


class RollingScheduler(PrepeakScheduler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.epoch = 0

    def update(self, now, need, profiles, capacities, forecast_end_s):
        old = self.state
        if old is None or now >= old.protected_until_s:
            self.epoch += 1
        original = self.periods
        self.periods = tuple(GrowthPeriod(f"{self.epoch}:{p.key}", p.start_s, p.end_s)
                             for p in original)
        try:
            return super().update(now, need, profiles, capacities, forecast_end_s)
        finally:
            self.periods = original
