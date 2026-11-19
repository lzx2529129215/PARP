"""Same provider output is consumed in simulation, native trials and future ML."""
from dataclasses import asdict, replace
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "controller"))
from adapters import ColdReply, attach_external_outputs
from coordinator import Coordinator
from executor import Guard
from plan import Candidate, Calibration, Phase, ReclaimWindow, Trial, action_plan
from prepeak_timing import PrepeakScheduler, growth_periods, remaining_growth
from replay_forecast import Forecast
from sequence_policy import sequence_high
from oracle_scenarios import APPS, APP_IDS, MIB
from prediction_contract import IdealProvider, PredictionRequest
from scheduler_fix import RollingScheduler


class Pipeline:
    def __init__(self, scenario, variant, backends, root_usage, limit_bytes, initial,
                 calibrated, profile, tag, clock, *, horizon=20., parallel=False,
                 advance_forecast=True, timing="baseline"):
        self.s, self.backends, self.root_usage = scenario, backends, root_usage
        self.limit, self.initial, self.calibrated = limit_bytes, initial, calibrated
        self.profile, self.tag, self.clock = profile, tag, clock
        self.variant, self.horizon, self.timing = variant, horizon, timing
        self.provider = IdealProvider(scenario, variant, initial, backends, advance_forecast)
        self.history = {a: [] for a in APPS}
        self.frames, self.coordinator, self.last_update = [], None, -1.
        self.parallel, self.guard = parallel, Guard()
        self.highs = {}
        for a in APPS:
            row = initial[a]
            # Fixed high is a common fixture/calibration envelope, deliberately
            # independent of which rolling-output variant wins this comparison.
            growth = max(scenario.demand(a, t)-scenario.demand(a, 0.)
                         for t in [0., *(e.at for e in scenario.events)])
            floor = Trial(row["rss"]+growth, row["usage"]+growth+8*MIB, 0, 0.)
            windows = [{"start_s": 0., "end_s": scenario.end_s,
                        "wss_bytes": max(row["rss"]+growth, scenario.wss(a, scenario.end_s)),
                        "pgfault": 0, "valid": True}]
            self.highs[a] = sequence_high(windows, [floor], row["usage"], 0, "fixed-reference-"+a)
        scheduler_type = RollingScheduler if timing == "epoch" else PrepeakScheduler
        self.scheduler = scheduler_type([{"time_s": 0., "total": 0}, {"time_s": horizon, "total": 0}])
        self.update(0.)

    def need(self):
        now = self.clock()
        if now >= self.forecast.expires_s:
            return 0
        growth = remaining_growth(self.forecast, now)
        return max(0, growth+self.s.reserve_bytes-max(0, self.limit-self.root_usage())) if growth else 0

    def update(self, now):
        snapshots = {}
        for a, b in self.backends.items():
            started = self.clock()
            snapshots[a] = {"rss_bytes": b.rss(), "usage_bytes": b.usage(),
                            "refaults": b.feedback()[0], "read_started_s": started}
            snapshots[a]["read_finished_s"] = self.clock()
        now = max(now, self.clock())
        inter = self.provider.predict_inter_app(now, self.horizon)
        intra, requests = {}, {}
        for a in APPS:
            self.history[a].append({"time_s": now, **snapshots[a]})
            self.history[a] = [r for r in self.history[a] if r["time_s"] >= now-8.]
            request = PredictionRequest(a, now, self.horizon, .5, tuple(self.history[a]), inter)
            requests[a] = asdict(request)
            intra[a] = self.provider.predict_intra_app(request)
        curves = {}
        for a, reply in intra.items():
            base_wss = reply["current_wss_bytes"]
            curves[a] = [(now, 0)] + [
                (r["end_s"], r.get("occupancy_delta_bytes", r["wss_bytes"]-base_wss))
                for r in reply["windows"]]
        times = sorted({t for c in curves.values() for t, _ in c})
        occupancy = [{"time_s": t, "total": sum(next(v for x, v in reversed(c) if x <= t)
                                                  for c in curves.values())} for t in times]
        # growth_periods requires nonnegative absolute occupancy. Translation
        # preserves curve shape and aligned changes, including predicted frees.
        offset = -min(0, min(r["total"] for r in occupancy))
        occupancy = [{**r, "total": r["total"]+offset} for r in occupancy]
        self.forecast = Forecast(now, min([inter["expires_s"], *(r["expires_s"] for r in intra.values())]),
                                 curves, {a: r["windows"] for a, r in intra.items()}, 0,
                                 now+self.horizon, "aligned_rolling_provider_outputs")
        self.scheduler.periods, _ = growth_periods(occupancy)
        need = self.need()
        supply_cap = self.calibrated["approved_bytes"]
        if self.timing == "epoch":
            e = self.coordinator.executors["cold"] if self.coordinator else None
            supply_cap = min(max(0, supply_cap-(e.used if e else 0)),
                             intra["cold"].get("reclaimable_bytes", supply_cap))
        state = self.scheduler.update(now, need, {"cold": self.profile},
                                      {"cold": supply_cap}, now+self.horizon)
        candidates, replies = [], []
        for a in APPS:
            row, reply = self.initial[a], intra[a]
            executor = self.coordinator.executors[a] if self.coordinator else None
            released = executor.observed_release if executor else 0
            if a == "cold":
                before = Trial(row["rss"], row["usage"], 0, 0.)
                after = Trial(self.calibrated["safe_rss"], self.calibrated["safe_usage"],
                              self.calibrated["idle_faults"], 0.)
                calibration = Calibration("physical-cold", before, (after,), .1, 64, None, 0.,
                                          measurement_id=self.calibrated["measurement_id"], method="reclaim")
                # Adapter converts CURRENT additional supply to the legacy
                # cumulative cap. It never grants new attempt budget.
                current_supply = reply.get("reclaimable_bytes", max(0, self.s.cold_bytes-released))
                cold_cap = min(self.calibrated["approved_bytes"], released+current_supply)
                attempt_cap = self.calibrated["approved_bytes"]
            else:
                calibration = Calibration("hot-"+a, Trial(row["rss"], row["usage"], 0, 0.), (),
                                          .1, 64, None, 0., measurement_id="whole-hot-fixture", method="observe")
                cold_cap = attempt_cap = current_supply = 0
            valid_until = reply.get("safe_until_s", now+self.horizon)
            if valid_until <= now:
                cold_cap = released
                valid_until = now+.001  # zero additional supply, no unsafe credit
            mode = "reclaim" if (a == "cold" and current_supply and state.key != "no_known_growth"
                                  and now < state.finish_by_s) else "observe"
            left = min(state.start_s, self.s.end_s-.001) if mode == "reclaim" else 0.
            right = min(state.finish_by_s, self.s.end_s) if mode == "reclaim" else self.s.end_s
            if right <= left:
                mode, left, right = "observe", 0., self.s.end_s
            keep = max(r["wss_bytes"] for r in reply["windows"])
            phase = Phase(left, right, calibration, "unknown", mode=mode, keep_rss_bytes=keep)
            candidates.append(Candidate(a, 0, row["usage"], (phase,), cold_cap_bytes=cold_cap,
                                        cold_valid_until_s=valid_until, attempt_cap_bytes=attempt_cap,
                                        method_attempt_caps=(("high", 0), ("reclaim", attempt_cap)),
                                        sequence_high=self.highs[a]))
            replies.append(ColdReply(a, cold_cap, now, valid_until, reply["source"]))
        candidates, excluded = attach_external_outputs(candidates, inter["entries"],
                                                       {v: k for k, v in APP_IDS.items()}, replies, now)
        plans = []
        for candidate in candidates:
            old = self.coordinator.executors[candidate.app].plan if self.coordinator else None
            budget = old.budget_bytes if old else candidate.attempt_cap_bytes
            plan = action_plan(candidate, (old.goal_bytes or 0) if old else 0,
                               self.tag+":"+candidate.app, self.s.end_s, attempt_budget=budget,
                               revision=old.revision+1 if old else 0)
            plans.append(replace(plan, reclaim_window=ReclaimWindow(state.key, state.start_s,
                                 state.finish_by_s, state.protected_until_s, self.profile.batch_bound_s)))
        if self.coordinator is None:
            self.coordinator = Coordinator(candidates, self.backends, self.tag, self.s.end_s,
                                           plans=plans, deadline_s=state.finish_by_s,
                                           parallel=self.parallel, clock=self.clock)
            self.coordinator._accept(plans, now)
        else:
            self.coordinator.update_forecast(candidates, plans, state.finish_by_s, now)
        self.frames.append({"time_s": now, "requests": requests, "inter_app": inter, "intra_app": intra,
                            "need_bytes": need, "timing": asdict(state), "excluded": excluded})
        self.last_update = now

    def tick(self, now):
        if now-self.last_update >= .5-1e-8 and now < self.s.end_s:
            self.update(now)
        self.coordinator.tick(now, self.need)

    def close(self, now):
        self.coordinator.close(now)
        return {"frames": self.frames, "coordinator_events": self.coordinator.events,
                "executor_events": {a: e.events for a, e in self.coordinator.executors.items()},
                "errors": self.coordinator.errors}
