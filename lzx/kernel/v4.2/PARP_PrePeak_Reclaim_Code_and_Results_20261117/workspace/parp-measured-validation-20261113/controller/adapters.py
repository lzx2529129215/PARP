"""Boundary adapters only. Do not implement or train another heat predictor."""
from dataclasses import dataclass,replace
from plan import natural,seconds


@dataclass(frozen=True)
class ColdReply:
    app:str
    cold_bytes:int
    observed_s:float
    valid_until_s:float
    source:str

    def __post_init__(self):
        natural(self.cold_bytes);seconds(self.observed_s);seconds(self.valid_until_s)
        if not self.app or not self.source or self.valid_until_s<=self.observed_s:raise ValueError('invalid cold reply')


def attach_external_outputs(candidates,entries,id_to_app,cold_replies,now):
    """Accept actual fuse() tuple shape: (appid, protection_Q15, rank, flags).

    Q15 high=protected; bit 0=foreground. Missing/stale outputs are excluded,
    never silently converted to zero protection or unlimited cold memory.
    ColdReply is a provisional adapter shape to align with the teammate later.
    """
    scores={};excluded={}
    for row in entries:
        if len(row)!=4:raise ValueError('v4.2 score tuple required')
        appid,score,rank,flags=row
        natural(appid);natural(score);natural(rank);natural(flags)
        if score>32767 or appid not in id_to_app:raise ValueError('invalid score/binding')
        app=id_to_app[appid]
        if app in scores:raise ValueError('duplicate app score')
        scores[app]=(score,flags)
    replies={r.app:r for r in cold_replies}
    if len(replies)!=len(cold_replies):raise ValueError('duplicate cold reply')
    result=[]
    for c in candidates:
        reason=None
        if c.app not in scores:reason='missing_protection_score'
        elif scores[c.app][1]&1:reason='foreground'
        reply=replies.get(c.app)
        if reply is None:reason=reason or 'missing_cold_reply'
        elif not reply.observed_s<=now<reply.valid_until_s:reason=reason or 'stale_cold_reply'
        if reason:
            excluded[c.app]=reason
            result.append(replace(c,background=False,cold_cap_bytes=0))
        else:
            result.append(replace(c,protection=scores[c.app][0],cold_cap_bytes=reply.cold_bytes,
                                  cold_valid_until_s=reply.valid_until_s))
    return result,excluded
