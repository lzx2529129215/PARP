"""Online scripted predictor: rolling measured futures plus current memory feedback.

This replays real no-policy reference traces. It is not a learned predictor and
does not claim knowledge of the altered future after reclamation.
"""
from dataclasses import dataclass
import bisect


@dataclass(frozen=True)
class Forecast:
    issued_s:float
    expires_s:float
    curves:dict
    signals:dict
    required_bytes:int
    deadline_s:float
    source:str='online_replay_of_measured_reference'


class OnlineReplay:
    def __init__(self,occupancy,signals,*,horizon_s=20.0,valid_for_s=1.0):
        if not occupancy or horizon_s<=0 or valid_for_s<=0:raise ValueError('measured reference and positive horizons required')
        self.occupancy=occupancy;self.signals=signals
        self.horizon=horizon_s;self.valid_for=valid_for_s;self.calls=[]
        self.times=[r['time_s'] for r in occupancy]
        if self.times!=sorted(set(self.times)):raise ValueError('strict reference times required')
        apps=set(occupancy[0]['apps'])
        if any(set(r['apps'])!=apps for r in occupancy):raise ValueError('reference scope changed')
    def predict(self,now,free_bytes,reserve_bytes=0):
        if now<0 or free_bytes<0 or reserve_bytes<0:raise ValueError('nonnegative time/headroom required')
        current=self.occupancy[max(0,bisect.bisect_right(self.times,now)-1)]
        future=[r for r in self.occupancy if now<r['time_s']<=now+self.horizon]
        if not future:raise ValueError('measured future exhausted; do not invent a tail')
        curves={app:[(now,0)] for app in current['apps']}
        # Root includes kernel/unassigned charges; preserve that measured part.
        current_extra=current['total']-sum(current['apps'].values())
        curves['non_app']=[(now,0)]
        totals=[(now,0)]
        for row in future:
            total=row['total']-current['total']
            totals.append((row['time_s'],total))
            for app in current['apps']:curves[app].append((row['time_s'],row['apps'][app]-current['apps'][app]))
            extra=row['total']-sum(row['apps'].values())
            curves['non_app'].append((row['time_s'],extra-current_extra))
        peak_t,peak=max(totals,key=lambda item:item[1])
        required=max(0,peak+reserve_bytes-free_bytes)
        deadline=next((t for t,value in totals if value+reserve_bytes>free_bytes),peak_t)
        signals={app:tuple(row for row in rows if row['end_s']>now and row['start_s']<now+self.horizon)
                 for app,rows in self.signals.items()}
        forecast=Forecast(now,now+self.valid_for,curves,signals,required,deadline)
        self.calls.append(dict(issued_s=now,free_bytes=free_bytes,required_bytes=required,deadline_s=deadline,
                               source=forecast.source))
        return forecast
