"""Real cgroup experiment for frozen rolling-output candidates and timing fix."""
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import sys
import time
import uuid

from oracle_scenarios import APPS, MIB, Scenario, scenarios
from study_pipeline import Pipeline, ROOT
from native_support import Case, calibrate, dump, stats
from prepeak_timing import growth_periods

RESULTS = Path(os.environ.get("PARP_RESULTS_ROOT", str(ROOT/"results")))


def run_case(base, output, spec, calibration, profile):
    s = next(s for s in scenarios() if s.name == spec["scenario"])
    directory = output/spec["tag"]
    directory.mkdir()
    case, pipe = Case(base, directory, s), None
    try:
        quota = case.usage()+s.initial_headroom_bytes
        (case.path/"memory.max").write_text(str(quota))
        initial_root = case.usage()
        if abs(quota-initial_root-s.initial_headroom_bytes) > 8*MIB:
            raise RuntimeError("initial free-space admission drift > 8 MiB")
        origin_ns = time.monotonic_ns()
        clock = lambda: (time.monotonic_ns()-origin_ns)/1e9
        pipe = Pipeline(s, spec["variant"], case.backends, case.usage, quota, case.initial,
                        calibration, profile, spec["tag"], clock, horizon=spec["horizon_s"],
                        parallel=True, advance_forecast=spec["advance"], timing="epoch")
        if s.flat_wss:
            case.workers["incoming"].command("sweep_limit", 16*MIB)
        events = [(e.at, "hot" if e.delta > 0 else "release_hot", e.app, abs(e.delta)) for e in s.events]
        if s.cold_reuse_s is not None:
            events.append((s.cold_reuse_s, "revisit", "cold", 0))
        events.sort(key=lambda e: (e[0], e[1] != "release_hot"))
        index, records, samples, next_log = 0, [], [], 0.
        # This is a negative capability probe, not a claim to have filled a gap.
        if s.name == "cold_revisit":
            for _ in range(36):
                pipe.tick(clock())
                time.sleep(.05)
            last = pipe.frames[-1]
            if last["timing"]["unallocated_bytes"] <= 0:
                raise RuntimeError("unsafe cold-revisit case was not reported infeasible")
            report = pipe.close(clock()); pipe = None
            calls = [w for b in case.backends.values() for w in b.reclaim_calls]
            result = {"status": "CORRECTLY_INFEASIBLE", "passed": not calls,
                      "spec": spec, "scenario": asdict(s), "pipeline": report,
                      "reason": "only donor becomes hot in the protected forecast; no safe cold supply"}
            dump(directory/"result.json", result)
            print(json.dumps({"event": "TRIAL_COMPLETE", "tag": spec["tag"], "status": result["status"],
                              "passed": result["passed"]}), flush=True)
            return {k:v for k,v in result.items() if k != "pipeline"}
        while clock() < s.end_s:
            now = clock()
            while index < len(events) and events[index][0] <= now:
                at, action, app, amount = events[index]
                before = case.workers[app].command("snapshot")
                feedback = {a:b.feedback() for a,b in case.backends.items()}
                usage = case.usage()
                deficit = max(0, amount-(quota-usage)) if action == "hot" else 0
                record = {"scheduled_s": at, "actual_s": clock(), "action": action, "app": app,
                          "bytes": amount, "preallocation_gap_bytes": deficit,
                          "free_before_bytes": quota-usage}
                response = case.workers[app].command(action, amount)
                response["minor_delta"] = response["minor"]-before["minor"]
                response["major_delta"] = response["major"]-before["major"]
                response["feedback_delta"] = {a:{"refaults":b.feedback()[0]-feedback[a][0],
                                                     "psi_s":b.feedback()[1]-feedback[a][1]}
                                              for a,b in case.backends.items()}
                record["response"] = response
                records.append(record)
                dump(directory/"workload_events.json", records)
                if clock()-at > 2.:
                    raise RuntimeError("workload event exceeded frozen 2-second timing admission")
                index += 1
                now = clock()
            pipe.tick(clock())
            if pipe.coordinator.errors:
                raise RuntimeError("service error: "+repr(pipe.coordinator.errors))
            usage = case.usage()
            samples.append({"time_s":clock(), "usage":usage, "free_bytes":quota-usage,
                            "remaining_need":pipe.need()})
            if now >= next_log:
                live = {"tag":spec["tag"], "time_s":round(now,2),
                        "need_MiB":round(pipe.need()/MIB,3), "event_index":index,
                        "cold_rss_MiB":round(case.backends["cold"].rss()/MIB,3)}
                dump(output/"LIVE.json", live)
                print(json.dumps(live), flush=True)
                next_log += 3.
            time.sleep(.05)
        report = pipe.close(clock()); pipe = None
        calls = [{"app":a, **w, "begin_s":(w["begin_ns"]-origin_ns)/1e9,
                  "end_s":(w["end_ns"]-origin_ns)/1e9}
                 for a,b in case.backends.items() for w in b.reclaim_calls]
        reference = [{"time_s":i*.5, "total":sum(s.demand(a,i*.5) for a in APPS)}
                     for i in range(round(s.end_s/.5)+1)]
        periods, _ = growth_periods(reference)
        during_peak = [w for w in calls if any(p.start_s <= w["end_s"] < p.end_s for p in periods)]
        allocation = [r for r in records if r["action"] == "hot"]
        max_gap = max((r["preallocation_gap_bytes"] for r in allocation), default=0)
        root_events = stats(case.path/"memory.events")
        bad_writes = [w for w in calls if w["app"] != "cold" or w["error"] is not None]
        no_need = s.name in ("aligned_free", "no_deficit", "WSS_change_no_allocation")
        result = {"status":"COMPLETE", "spec":spec, "scenario":asdict(s),
                  "initial_usage":initial_root, "quota_bytes":quota, "workload_events":records,
                  "samples":samples, "pipeline":report, "reclaim_calls":calls,
                  "max_preallocation_gap_bytes":max_gap, "peak_events":root_events,
                  "peak_allocation_ms":[r["response"]["elapsed_ms"] for r in allocation],
                  "peak_allocation_cpu_ms":[r["response"]["cpu_elapsed_ms"] for r in allocation],
                  "during_peak_reclaim_calls":len(during_peak), "bad_reclaim_calls":len(bad_writes),
                  "passed":not report["errors"] and not bad_writes and not during_peak and
                           not root_events.get("oom_kill",0)}
        first_need = next((f for f in report["frames"] if f["need_bytes"] > 0), None)
        result["first_need_timing"] = first_need["timing"] if first_need else None
        result["conservative_lead_qualified"] = first_need is None or first_need["timing"]["feasible"]
        if spec["advance"]:
            result["passed"] &= max_gap == 0 and (not no_need or not calls)
        else:
            result["passed"] &= not calls
            if not no_need:
                # Confirm this control actually had a deficit, not spare space.
                result["passed"] &= max_gap >= 16*MIB
        dump(directory/"result.json", result)
        print(json.dumps({"event":"TRIAL_COMPLETE", "tag":spec["tag"], "passed":result["passed"],
                          "gap_MiB":max_gap/MIB, "peak_ms":result["peak_allocation_ms"],
                          "peak_cpu_ms":result["peak_allocation_cpu_ms"],
                          "requested_MiB":sum(w["requested_bytes"] for w in calls)/MIB}), flush=True)
        if not result["passed"]:
            raise RuntimeError("formal acceptance failed at "+spec["tag"]+"; preserve and diagnose")
        return {k:v for k,v in result.items() if k not in ("pipeline","samples","workload_events","reclaim_calls")}
    finally:
        if pipe is not None:
            dump(directory/"interrupted_pipeline.json", pipe.close(clock()))
        case.close()


def suite():
    result = []
    # Reverse arm order on alternating pairs; three independent step pairs.
    names = ["step", "ramp", "V", "inverted_V", "oscillating", "flat_WSS_growth",
             "aligned_free", "WSS_change_no_allocation", "no_deficit", "step", "step"]
    for index,name in enumerate(names):
        for advance in ((False,True) if index%2 == 0 else (True,False)):
            result.append({"tag":f"{len(result)+1:02}-{name}-"+("ideal" if advance else "control"),
                           "scenario":name, "variant":"occupancy_supply", "horizon_s":20., "advance":advance})
    for horizon in (12.,30.):
        result.append({"tag":f"{len(result)+1:02}-step-H{int(horizon)}", "scenario":"step",
                       "variant":"occupancy_supply", "horizon_s":horizon, "advance":True})
    result.append({"tag":f"{len(result)+1:02}-cold-revisit-guard", "scenario":"cold_revisit",
                   "variant":"occupancy_supply", "horizon_s":20., "advance":True})
    return result


def main():
    if sys.platform != "linux" or os.getuid() == 0:
        raise RuntimeError("native service must run as ordinary VM user")
    RESULTS.mkdir(exist_ok=True, parents=True)
    config = json.loads(Path(os.environ.get("PARP_DELEGATION_FILE",str(RESULTS/"delegation.json"))).read_text())
    base = Path(config["base"]).resolve(strict=True)
    if base.parent != Path("/sys/fs/cgroup") or not base.name.startswith("parp-ideal-"):
        raise RuntimeError("unexpected delegated experiment subtree")
    output = RESULTS/("native-"+uuid.uuid4().hex[:10]); output.mkdir()
    specs = suite()
    selection = ROOT/"selection.json"
    if selection.exists():
        names = json.loads(selection.read_text())["tags"]
        specs = [s for s in specs if s["tag"] in names]
        if len(specs) != len(names):
            raise ValueError("unknown selected trial")
    plan = {"native":True,"trained_predictors_called":False,"timing_fix":"expired_peak_epoch",
            "specs":specs,"source_sha256":{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in ROOT.glob("*.py")},
            "controller_sha256":{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in (ROOT/"controller").glob("*.py")}}
    if plan["controller_sha256"] != json.loads((ROOT/"source_baseline.json").read_text())["controller_sha256"]:
        raise RuntimeError("uploaded baseline controller changed unexpectedly")
    dump(output/"PLAN.json",plan)
    dump(RESULTS/"ACTIVE.json",{"status":"RUNNING","output":output.name})
    result = {"status":"RUNNING","trials":[]}
    try:
        calibration, profile = calibrate(base,output,Scenario())
        for spec in specs:
            result["trials"].append(run_case(base,output,spec,calibration,profile))
            dump(output/"SUMMARY.json",result)
        result["status"] = "COMPLETE"
    except BaseException as error:
        result.update(status="STOPPED_FOR_DIAGNOSIS",error=repr(error))
        raise
    finally:
        dump(output/"SUMMARY.json",result)
        dump(RESULTS/"ACTIVE.json",{"status":result["status"],"output":output.name})


if __name__ == "__main__":
    main()
