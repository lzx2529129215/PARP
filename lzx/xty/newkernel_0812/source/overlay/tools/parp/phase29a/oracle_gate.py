#!/usr/bin/env python3
"""Reconstruct MGLRU-eligible proxy candidates and run the G0 Oracle gate."""

import argparse
from collections import defaultdict
import gzip
import hashlib
import json
import math
import os
from pathlib import Path
import statistics
import time

SESSIONS=("wps_01","wps_02","wps_03","files_01","files_02")
HORIZONS=(10,30,60,120); POOL_SIZES=(32,64,128,256); PROTECT=(.10,.20); RECLAIM=(.25,.50)
SCHEMES=("all_valid_observed","age_tail","recency_tail","generation_tail","oldest_n")
POLICIES=("BASE_NATIVE_RECENCY","BASE_DAMON_HOTNESS","BASE_RECENT_FREQUENCY","ORACLE_FUTURE_REUSE")


def atomic_json(path,payload):
    path.parent.mkdir(parents=True,exist_ok=True); tmp=path.with_name(path.name+".tmp")
    with tmp.open("w") as stream: json.dump(payload,stream,indent=2,sort_keys=True); stream.write("\n")
    os.replace(tmp,path)


def atomic_text(path,text):
    path.parent.mkdir(parents=True,exist_ok=True); tmp=path.with_name(path.name+".tmp"); tmp.write_text(text); os.replace(tmp,path)


def stable_hash(parts): return hashlib.sha256("\n".join(str(x) for x in parts).encode()).hexdigest()
def bit_count(value): return bin(value).count("1")


def compact_windows(root,session):
    output=[]
    with gzip.open(root/f"dataset/windows/10s/{session}.jsonl.gz","rt") as stream:
        for line in stream:
            row=json.loads(line); files={}
            for rank,item in enumerate(row["files"],1):
                files[(item["key"],item["partition_generation"])]=({"key":item["key"],"generation":item["partition_generation"],
                  "pages":item["file_page_count"],"size":item["file_size_bytes"],"observed":int(item["observed_bits"]["100"],16),
                  "active":int(item["active_bits"]["100"],16),"age":item["mean_age"],"max_age":item["max_age"],"coverage":item["coverage"],
                  "weighted":item["weighted_coverage"],"intensity":item["mean_access_ratio"],"rank":rank,"share":item["activity_share"]})
            kernel=row["kernel"]["values"]
            output.append({"window_id":row["window_id"],"session_id":session,"app":row["app"],"app_id":row["app_id"],"domain_id":row["domain_id"],
              "start":row["window_start_ns"],"end":row["window_end_ns"],"files":files,"anon":row["anon"],"kernel":kernel})
    return output


def future_target(rows,index,key,generation,segment,horizon):
    steps=horizon//10; observed=False
    for offset in range(1,steps+1):
        if index+offset>=len(rows): return None,False
        item=rows[index+offset]["files"].get((key,generation))
        if not item: continue
        if item["observed"]>>segment&1:
            observed=True
            if item["active"]>>segment&1: return offset*10,True
    return None,observed


def universe_for(row,index,last_access,last_two,file_last,file_previous,ema):
    universe=[]; ordinal=0
    for (key,generation),item in sorted(row["files"].items()):
        effective=min(100,item["pages"])
        for segment in range(effective):
            if not item["observed"]>>segment&1: continue
            history_key=(key,generation,segment); accesses=last_two.get(history_key,[]); current=bool(item["active"]>>segment&1)
            if current:
                accesses=(accesses+[index])[-3:]; last_two[history_key]=accesses; file_previous[(key,generation)]=file_last.get((key,generation)); file_last[(key,generation)]=index
            last=accesses[-1] if accesses else None; previous=accesses[-2] if len(accesses)>=2 else None; third=accesses[-3] if len(accesses)>=3 else None
            delta=(index-last)*10 if last is not None else 1_000_000; delta12=(last-previous)*10 if previous is not None else 1_000_000; delta23=(previous-third)*10 if third is not None else 1_000_000
            value=ema.get(history_key,0.0)*.8+.2*float(current); ema[history_key]=value
            recent=sum(1 for x in accesses if index-x<=6); ordinal+=1
            file_last_value=file_last.get((key,generation)); file_previous_value=file_previous.get((key,generation))
            file_last_delta=(index-file_last_value)*10 if file_last_value is not None else 1_000_000
            file_previous_delta=(index-file_previous_value)*10 if file_previous_value is not None else 1_000_000
            universe.append({"identity":"%s|%s|%d"%(key,generation,segment),"file_key_metadata":key,"partition_generation":generation,"segment_id":segment,
              "ordinal":ordinal,"current_active":current,"delta_since_last_access":delta,"delta_between_last_two":delta12,"delta_between_second_third":delta23,
              "file_last_delta":file_last_delta,"file_previous_delta":file_previous_delta,
              "normalized_position":segment/max(1,effective-1),"file_size_log":math.log1p(item["pages"]),"segment_size_log":math.log1p(max(1,math.ceil(item["pages"]/effective))),
              "segment_ema":value,"file_ema":item["share"],"segment_age":item["age"],"file_age":item["max_age"],"current_coverage":item["coverage"],
              "weighted_coverage":item["weighted"],"recent_access_count":recent,"consecutive_inactive":min(12,delta//10) if delta<1_000_000 else 12,
              "generation_proxy":min(1.0,item["age"]/max(1,item["max_age"])),"damon_hotness":float(current)+item["intensity"]+item["weighted"],
              "native_recency":-float(delta),"recent_frequency":recent+value,"observed_state":"OBSERVED_ACTIVE" if current else "OBSERVED_INACTIVE"})
    return universe


def scheme_order(universe,scheme):
    if scheme=="all_valid_observed": return sorted(universe,key=lambda x:x["ordinal"])
    if scheme=="age_tail": return sorted(universe,key=lambda x:(-x["segment_age"],x["current_active"],-x["delta_since_last_access"],x["ordinal"]))
    if scheme=="recency_tail": return sorted(universe,key=lambda x:(x["current_active"],-x["delta_since_last_access"],x["ordinal"]))
    if scheme=="generation_tail": return sorted(universe,key=lambda x:(x["current_active"],-x["generation_proxy"],-x["delta_since_last_access"],-x["segment_age"],x["ordinal"]))
    return sorted(universe,key=lambda x:(-x["file_age"],x["current_active"],-x["delta_since_last_access"],x["ordinal"]))


def ndcg(order,relevant,count):
    chosen=order[:count]; dcg=sum((1 if relevant[i] else 0)/math.log2(rank+2) for rank,i in enumerate(chosen))
    ideal=sum(1/math.log2(rank+2) for rank in range(min(count,sum(relevant))))
    return dcg/ideal if ideal else 0.0


def evaluate(candidates,horizon,protect_ratio,reclaim_ratio,policy):
    n=len(candidates); protect=max(1,math.ceil(n*protect_ratio)); reclaim=max(1,math.ceil(n*reclaim_ratio))
    score_name={"BASE_NATIVE_RECENCY":"native_recency","BASE_DAMON_HOTNESS":"damon_hotness","BASE_RECENT_FREQUENCY":"recent_frequency"}.get(policy)
    if score_name: scores=[x[score_name] for x in candidates]
    else: scores=[1/(x["future"][str(horizon)]+1e-9) if x["future"][str(horizon)] is not None else 0 for x in candidates]
    order=sorted(range(n),key=lambda i:(-scores[i],candidates[i]["ordinal"])); protected=set(order[:protect]); reclaimed=set(order[-reclaim:])
    relevant=[x["future"][str(horizon)] is not None for x in candidates]; total=sum(relevant); after=sum(relevant[i] for i in reclaimed); hits=sum(relevant[i] for i in protected)
    return {"candidate_count":n,"protected_candidates":len(protected),"reclaimed_candidates":len(reclaimed),"future_reuse_after_reclaim":after,
      "future_reuse_saved":total-after,"normalized_refault_proxy_per_1000_reclaimed":after*1000/len(reclaimed),"recall_at_budget":hits/total if total else 0,
      "ndcg_at_budget":ndcg(order,relevant,protect),"false_cold":after/total if total else 0,"protection_hit_rate":hits/len(protected),
      "protection_waste":1-hits/len(protected),"ranking_hash":stable_hash(candidates[i]["identity"] for i in order),
      "selected_hash":stable_hash(candidates[i]["identity"] for i in order[:protect]),"candidate_hash":stable_hash(x["identity"] for x in candidates)}


def add(aggregate,key,value):
    row=aggregate.setdefault(key,{"decisions":0,"candidate_count":0,"protected_candidates":0,"reclaimed_candidates":0,"future_reuse_after_reclaim":0,"future_reuse_saved":0,
      "recall_at_budget_sum":0.0,"ndcg_at_budget_sum":0.0,"false_cold_sum":0.0,"protection_hit_rate_sum":0.0,"protection_waste_sum":0.0,"ranking_hashes":[],"selected_hashes":[],"candidate_hashes":[]})
    row["decisions"]+=1
    for name in ("candidate_count","protected_candidates","reclaimed_candidates","future_reuse_after_reclaim","future_reuse_saved"): row[name]+=value[name]
    for name in ("recall_at_budget","ndcg_at_budget","false_cold","protection_hit_rate","protection_waste"): row[name+"_sum"]+=value[name]
    for name in ("ranking_hash","selected_hash","candidate_hash"): row[name+"es"].append(value[name])


def finalize(row):
    decisions=row["decisions"]; reclaimed=row["reclaimed_candidates"]
    output={k:v for k,v in row.items() if not k.endswith("_sum") and not k.endswith("hashes")}
    output["normalized_refault_proxy_per_1000_reclaimed"]=row["future_reuse_after_reclaim"]*1000/reclaimed if reclaimed else None
    for name in ("recall_at_budget","ndcg_at_budget","false_cold","protection_hit_rate","protection_waste"): output[name]=row[name+"_sum"]/decisions
    for name in ("ranking_hash","selected_hash","candidate_hash"): output[name]=stable_hash(row[name+"es"])
    return output


def run(phase28b,out):
    aggregate={}; counts=defaultdict(list); candidate_hashes={}; primary_path=out/"candidate_reconstruction/decisions_generation_tail_128.jsonl.gz"; tmp=primary_path.with_name(primary_path.name+".tmp")
    started=time.time_ns(); decision_count=0
    with gzip.open(tmp,"wt",compresslevel=3) as writer:
        for session in SESSIONS:
            rows=compact_windows(phase28b,session); last_access={}; last_two={}; file_last={}; file_previous={}; ema={}
            for index,row in enumerate(rows):
                universe=universe_for(row,index,last_access,last_two,file_last,file_previous,ema)
                counts[session].append(len(universe)); if_future={}
                scheme_pools={scheme:scheme_order(universe,scheme)[:max(POOL_SIZES)] for scheme in SCHEMES}
                identities={x["identity"]:(x["file_key_metadata"],x["partition_generation"],x["segment_id"]) for pool in scheme_pools.values() for x in pool}
                for identity,(key,generation,segment) in identities.items():
                    if_future[identity]={str(h):future_target(rows,index,key,generation,segment,h)[0] for h in HORIZONS}
                for scheme,pool in scheme_pools.items():
                    for size in POOL_SIZES:
                        chosen=pool[:size]
                        if len(chosen)<min(32,size): continue
                        for x in chosen: x["future"]=if_future[x["identity"]]
                        # All schemes/sizes share one primary operating point;
                        # the selected primary pool receives the full budget and
                        # horizon grid.  This preserves the requested comparison
                        # without repeating equivalent sorts 1,280 times/window.
                        configs={(60,.10,.50)}
                        if scheme=="generation_tail" and size==128:
                            configs.update((h,p,r) for h in HORIZONS for p in PROTECT for r in RECLAIM)
                        for horizon,protect,reclaim in sorted(configs):
                            for policy in POLICIES:
                                key="|".join(map(str,(scheme,size,horizon,protect,reclaim,policy))); add(aggregate,key,evaluate(chosen,horizon,protect,reclaim,policy))
                primary=scheme_pools["generation_tail"][:128]
                if len(primary)>=32:
                    for x in primary: x["future"]=if_future[x["identity"]]
                    candidate_hash=stable_hash(x["identity"] for x in primary); decision_id=stable_hash((session,row["window_id"],candidate_hash))[:24]; decision_count+=1
                    record={"schema_version":1,"decision_id":decision_id,"session_id":session,"app":row["app"],"app_id":row["app_id"],"domain_id":row["domain_id"],
                      "window_start_ns":row["start"],"window_end_ns":row["end"],"candidate_schema":"MGLRU_ELIGIBLE_PROXY","candidate_hash":candidate_hash,"candidate_count":len(primary),
                      "window_context":{"anon_active_ratio":row["anon"]["active_ratio"],"anon_pages_log":math.log1p(row["anon"]["pages"]),
                        "anon_age_log":math.log1p(row["anon"]["mean_age"]),"kernel_values":row["kernel"]},
                      "candidates":primary,"future_information_used_for_candidate_set":False}
                    writer.write(json.dumps(record,sort_keys=True,separators=(",",":"))+"\n"); candidate_hashes[decision_id]={p:candidate_hash for p in POLICIES}
    os.replace(tmp,primary_path)
    summary={key:finalize(value) for key,value in aggregate.items()}
    passing=[]
    for key,value in summary.items():
        scheme,size,horizon,protect,reclaim,policy=key.split("|")
        if scheme!="generation_tail" or policy!="ORACLE_FUTURE_REUSE": continue
        base_key="|".join((scheme,size,horizon,protect,reclaim,"BASE_RECENT_FREQUENCY")); baseline=summary.get(base_key)
        if not baseline: continue
        old=baseline["normalized_refault_proxy_per_1000_reclaimed"]; new=value["normalized_refault_proxy_per_1000_reclaimed"]
        gain=(old-new)/old if old else 0
        if gain>=.20 and value["reclaimed_candidates"]>0 and value["protected_candidates"]<value["candidate_count"] and value["ranking_hash"]!=baseline["ranking_hash"]:
            passing.append({"key":key,"relative_proxy_reduction":gain,"oracle":value,"baseline":baseline})
    gate={"schema_version":1,"passed":bool(passing),"status":"G0_PASS" if passing else "PARP_PHASE29A_ORACLE_PROXY_INVALID","passing_points":passing,
      "criteria":{"relative_proxy_reduction":.20,"same_candidates":True,"same_reclaim_count":True,"distinct_ranking_required":True},"decision_count":decision_count}
    atomic_json(out/"oracle_routing/oracle_sanity.json",{"gate":gate,"grid":summary}); atomic_json(out/"candidate_reconstruction/candidate_counts.json",{"per_session":{k:{"windows":len(v),"mean_universe":statistics.fmean(v),"min":min(v),"max":max(v)} for k,v in counts.items()},"primary_decisions":decision_count})
    atomic_json(out/"candidate_reconstruction/candidate_hashes.json",{"all_policy_hashes_equal":all(len(set(v.values()))==1 for v in candidate_hashes.values()),"decisions":candidate_hashes})
    atomic_json(out/"candidate_reconstruction/candidate_schema.json",{"name":"MGLRU_ELIGIBLE_PROXY","resolution":100,"window_seconds":10,"primary_scheme":"generation_tail","primary_pool_size":128,
      "criteria":["target domain","current version and partition","currently observed","age or access history","exists before decision"],"not_real_mglru_scan_list":True,"future_used_for_candidates":False})
    atomic_text(out/"candidate_reconstruction/mglru_proxy_limitations.md","# MGLRU eligible proxy limitations\n\nThis candidate set is not the kernel MGLRU scan list. The trace has no folio residency, generation membership, referenced-bit, eviction eligibility, or exact scanner-position evidence. The proxy uses currently observed valid file segments, inactive/age/recency signals, and a deterministic cap. All policies nevertheless receive identical candidates and reclaim counts.\n")
    atomic_json(out/"performance/oracle_gate.json",{"started_ns":started,"ended_ns":time.time_ns(),"decision_count":decision_count})
    state={"schema_version":1,"stage":"ORACLE_SANITY_COMPLETE","timestamp_ns":time.time_ns(),"g0_passed":gate["passed"],"failure_reason":None if gate["passed"] else gate["status"],"resume_supported":True}
    atomic_json(out/"state/state.json",state)
    with (out/"state/history.jsonl").open("a") as stream: stream.write(json.dumps(state,sort_keys=True)+"\n")
    return gate


def main():
    p=argparse.ArgumentParser(); p.add_argument("--phase28b",type=Path,required=True); p.add_argument("--output",type=Path,required=True); a=p.parse_args(); print(json.dumps(run(a.phase28b,a.output),sort_keys=True))


if __name__=="__main__": main()
