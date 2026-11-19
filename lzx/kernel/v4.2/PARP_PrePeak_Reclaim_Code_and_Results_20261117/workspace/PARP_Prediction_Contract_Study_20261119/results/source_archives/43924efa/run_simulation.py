"""Fast deterministic logical screening; never reported as native VM evidence."""
from dataclasses import asdict
import json
from pathlib import Path
import traceback
import argparse
from oracle_scenarios import APPS, MIB, scenarios
from prediction_contract import VARIANTS
from study_pipeline import Pipeline, ROOT
from prepeak_timing import ReleaseProfile


class Clock:
    value = 0.
    def __call__(self): return self.value


class Plant:
    def __init__(self, usage, clock):
        self.value, self.clock, self.high, self.calls = usage, clock, None, []
    def usage(self): return self.value
    def rss(self): return self.value
    def check(self): pass
    def feedback(self): return 0, 0.
    def protections(self): return 0, 0
    def maximum(self): return 2**40
    def read_high(self): return self.high
    def write_high(self, value): self.high = value
    def reclaim(self, amount):
        self.value -= amount
        self.calls.append({"time_s": self.clock(), "bytes": amount})


def simulate(s, variant, horizon, rate=16, timing="baseline"):
    clock = Clock()
    plants = {"cold": Plant(528*MIB, clock), "hot": Plant(s.hot_bytes+16*MIB, clock),
              "incoming": Plant(s.incoming_base_bytes+16*MIB, clock)}
    initial = {a: {"rss": b.rss(), "usage": b.usage()} for a, b in plants.items()}
    root = lambda: sum(b.usage() for b in plants.values())
    quota = root()+s.initial_headroom_bytes
    calibration = {"approved_bytes": 384*MIB, "safe_rss": 144*MIB, "safe_usage": 144*MIB,
                   "idle_faults": 0, "measurement_id": "SIMULATION_NOT_NATIVE"}
    pipe, report = None, None
    events = sorted(s.events, key=lambda e: (e.at, e.delta > 0))
    index, checks, cold_refault_bytes = 0, [], 0
    reuse_done = False
    try:
        pipe = Pipeline(s, variant, plants, root, quota, initial, calibration,
                        ReleaseProfile(rate*MIB, .6, 384*MIB), s.name, clock,
                        horizon=horizon, timing=timing)
        for tick in range(round(s.end_s/.05)):
            clock.value = round(tick*.05, 6)
            while index < len(events) and events[index].at <= clock.value:
                e = events[index]
                if e.delta > 0:
                    checks.append({"time_s": clock.value, "deficit_bytes": max(0, e.delta-(quota-root()))})
                plants[e.app].value += e.delta
                index += 1
            if not reuse_done and s.cold_reuse_s is not None and clock.value >= s.cold_reuse_s:
                cold_refault_bytes = max(0, initial["cold"]["usage"]-plants["cold"].usage())
                plants["cold"].value += cold_refault_bytes
                reuse_done = True
            pipe.tick(clock.value)
        report = pipe.close(s.end_s)
        pipe = None
        errors = report["errors"]
    except Exception as error:
        errors = [repr(error)]
        if pipe is not None:
            report = pipe.close(min(clock.value, s.end_s))
    calls = plants["cold"].calls
    max_gap = max((c["deficit_bytes"] for c in checks), default=0)
    no_need = (not s.events or s.name in ("aligned_free", "no_deficit"))
    result = {"scenario": s.name, "variant": variant, "horizon_s": horizon, "rate_MiB_s": rate,
              "timing": timing,
              "simulation_only": True, "errors": errors, "max_preallocation_gap_MiB": max_gap/MIB,
              "requested_MiB": sum(c["bytes"] for c in calls)/MIB,
              "cold_refault_MiB": cold_refault_bytes/MIB,
              "unnecessary_reclaim": bool(calls) and no_need,
              "checks": checks, "calls": calls,
              "passed": not errors and max_gap == 0 and cold_refault_bytes == 0 and not (bool(calls) and no_need)}
    result["expected_infeasible"] = s.name == "cold_revisit"
    result["correctly_blocked"] = result["expected_infeasible"] and not errors and not calls and max_gap > 0
    first_need = next((f for f in (report or {}).get("frames",[]) if f["need_bytes"] > 0), None)
    result["first_need_timing"] = first_need["timing"] if first_need else None
    result["conservative_lead_qualified"] = first_need is None or first_need["timing"]["feasible"]
    return result, report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--timing", choices=("baseline", "epoch"), default="baseline")
    args = parser.parse_args()
    output = ROOT/"results"
    output.mkdir(exist_ok=True)
    results = []
    for s in scenarios():
        for variant in VARIANTS:
            for h in (12., 20., 30.):
                result, report = simulate(s, variant, h, timing=args.timing)
                results.append(result)
                if result["errors"]:
                    path = output/("logical_error_"+args.timing+".json")
                    if not path.exists():
                        path.write_text(json.dumps({"result": result, "trace": report}, indent=2))
    (output/("screen-"+args.timing+".json")).write_text(json.dumps(results, indent=2))
    for variant in VARIANTS:
        selected = [r for r in results if r["variant"] == variant]
        print(json.dumps({"variant": variant, "passed": sum(r["passed"] for r in selected),
                          "total": len(selected), "errors": sum(bool(r["errors"]) for r in selected)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
