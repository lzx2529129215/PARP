"""One rolling output contract for script oracle and a future learned provider."""
from dataclasses import dataclass
import math
from oracle_scenarios import APPS, APP_IDS

VARIANTS = ("wss_only", "occupancy", "occupancy_supply", "occupancy_supply_faults")


@dataclass(frozen=True)
class PredictionRequest:
    app: str
    now_s: float
    horizon_s: float
    step_s: float
    observations: tuple
    inter_app: dict


def validate_output(reply, request):
    if reply["app"] != request.app or reply["issued_s"] != request.now_s:
        raise ValueError("app/generation mismatch")
    n = round(request.horizon_s / request.step_s)
    if n <= 0 or abs(n * request.step_s - request.horizon_s) > 1e-8:
        raise ValueError("horizon must contain complete output windows")
    if reply["horizon_s"] != request.horizon_s or reply["step_s"] != request.step_s:
        raise ValueError("output resolution/range differs from request")
    if not request.now_s < reply["expires_s"]:
        raise ValueError("expired forecast")
    if len(reply["windows"]) != n:
        raise ValueError("exactly H/step FUTURE windows required")
    for i, row in enumerate(reply["windows"]):
        left = request.now_s + i * request.step_s
        if abs(row["start_s"] - left) > 1e-8 or abs(row["end_s"] - left - request.step_s) > 1e-8:
            raise ValueError("rolling absolute output boundaries required")
        if type(row["wss_bytes"]) is not int or row["wss_bytes"] < 0:
            raise ValueError("invalid WSS bytes")
        if "occupancy_delta_bytes" in row and type(row["occupancy_delta_bytes"]) is not int:
            raise ValueError("signed incremental occupancy bytes required")
    if "reclaimable_bytes" in reply:
        if type(reply["reclaimable_bytes"]) is not int or reply["reclaimable_bytes"] < 0:
            raise ValueError("current additional reclaimable bytes required")
        if reply["safe_until_s"] < request.now_s:
            raise ValueError("cold certificate is already stale")


class IdealProvider:
    """Truth is private test-side state, never a proposed learned-model input."""
    def __init__(self, scenario, variant, initial, backends, advance_forecast=True):
        if variant not in VARIANTS:
            raise ValueError("unknown output ablation")
        self.s, self.variant, self.initial, self.backends = scenario, variant, initial, backends
        self.advance_forecast = advance_forecast

    def predict_inter_app(self, now, horizon):
        observed_visits = [(e.at, e.app) for e in self.s.events if e.delta > 0 and e.at <= now]
        if self.s.cold_reuse_s is not None and self.s.cold_reuse_s <= now:
            observed_visits.append((self.s.cold_reuse_s, "cold"))
        current = max(observed_visits)[1] if observed_visits else "hot"
        visits = []
        if self.advance_forecast:
            visits = [{"app": e.app, "at_s": e.at} for e in self.s.events
                      if e.delta != 0 and now < e.at <= now+horizon]
            if self.s.cold_reuse_s is not None and now < self.s.cold_reuse_s <= now+horizon:
                visits.append({"app": "cold", "at_s": self.s.cold_reuse_s})
        return {"issued_s": now, "expires_s": now+1., "visits": visits,
                "entries": [(APP_IDS[a], 0 if a == "cold" else 32767,
                             3 if a == "cold" else (1 if a == current else 2), int(a == current)) for a in APPS]}

    def predict_intra_app(self, request):
        s, a, now, h, step = self.s, request.app, request.now_s, request.horizon_s, request.step_s
        if not request.observations or request.observations[-1]["time_s"] > now:
            raise ValueError("a causal observed request is required")
        visit = any(v["app"] == a for v in request.inter_app["visits"])
        rows = []
        for i in range(round(h/step)):
            left, right = now+i*step, now+(i+1)*step
            # A boundary event is represented in the preceding window at its
            # earliest start: conservative timing, bounded by one output step.
            future_at = right if self.advance_forecast and visit else now
            delta = s.demand(a, future_at) - s.demand(a, now)
            row = {"start_s": left, "end_s": right, "wss_bytes": s.wss(a, future_at)}
            if self.variant != "wss_only":
                row["occupancy_delta_bytes"] = delta
            if self.variant.endswith("faults"):
                row["pgfault"] = max(0, delta)//4096
            rows.append(row)
        reply = {"app": a, "issued_s": now, "expires_s": now+1., "horizon_s": h,
                 "step_s": step, "current_wss_bytes": s.wss(a, now), "windows": rows,
                 "source": "scripted_truth_not_a_trained_model"}
        if self.variant.startswith("occupancy_supply"):
            # CURRENT additional cold bytes, not a refreshed cumulative budget.
            released = max(0, self.initial[a]["usage"]-self.backends[a].usage())
            reuse = s.cold_reuse_s
            safe_until = min(now+h, reuse) if reuse is not None else now+h
            # Whole-app hot during this forecast => no donation from that app.
            supply = max(0, s.cold_bytes-released) if a == "cold" else 0
            if a == "cold" and reuse is not None and reuse <= now+h:
                supply = 0
            reply.update(reclaimable_bytes=supply, safe_until_s=max(now, safe_until))
        validate_output(reply, request)
        return reply
