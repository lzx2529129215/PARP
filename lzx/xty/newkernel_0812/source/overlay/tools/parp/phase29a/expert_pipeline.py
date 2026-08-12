#!/usr/bin/env python3
"""Workload taxonomy and linear pairwise expert specialization experiment."""

import argparse
from collections import Counter, defaultdict
import csv
import gzip
import hashlib
import json
import math
import os
from pathlib import Path
import random
import statistics
import time

TRAIN={"wps_01","files_01"}; VALID={"wps_02"}; TEST={"wps_03","files_02"}
FEATURES=("delta_since_last_access","delta_between_last_two","delta_between_second_third","file_last_delta","file_previous_delta",
 "normalized_position","file_size_log","segment_size_log","segment_ema","file_ema","segment_age","file_age","current_coverage",
 "weighted_coverage","recent_access_count","consecutive_inactive","generation_proxy","damon_hotness")


def atomic_json(path,payload):
    path.parent.mkdir(parents=True,exist_ok=True); tmp=path.with_name(path.name+".tmp")
    with tmp.open("w") as stream: json.dump(payload,stream,indent=2,sort_keys=True); stream.write("\n")
    os.replace(tmp,path)


def atomic_text(path,text):
    path.parent.mkdir(parents=True,exist_ok=True); tmp=path.with_name(path.name+".tmp"); tmp.write_text(text); os.replace(tmp,path)


def stable_hash(value): return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(",",":")).encode()).hexdigest()


def feature_value(name,value):
    if "delta" in name or name in ("segment_age","file_age","recent_access_count","consecutive_inactive"): return math.log1p(min(1_000_000,max(0,float(value))))
    return float(value)


def candidate_vector(candidate): return [feature_value(name,candidate[name]) for name in FEATURES]


def future_descriptor(record):
    candidates=record["candidates"]; ratios=[]
    for horizon in (10,30,60,120): ratios.append(sum(x["future"][str(horizon)] is not None for x in candidates)/len(candidates))
    reused=[x for x in candidates if x["future"]["120"] is not None]; positions=[x["normalized_position"] for x in reused]; times=[x["future"]["120"] for x in reused]
    mean_pos=statistics.fmean(positions) if positions else 0; std_pos=statistics.pstdev(positions) if len(positions)>1 else 0
    entropy=0
    if positions:
        bins=[0]*10
        for value in positions: bins[min(9,int(value*10))]+=1
        total=sum(bins); entropy=-sum((x/total)*math.log(x/total) for x in bins if x)/math.log(10)
    correlation=0
    if len(positions)>2 and statistics.pstdev(positions)>0 and statistics.pstdev(times)>0:
        mp=statistics.fmean(positions); mt=statistics.fmean(times); correlation=sum((p-mp)*(t-mt) for p,t in zip(positions,times))/len(positions)/statistics.pstdev(positions)/statistics.pstdev(times)
    return ratios+[len(reused)/len(candidates),mean_pos,std_pos,(max(positions)-min(positions)) if positions else 0,entropy,correlation,
      statistics.fmean(times)/120 if times else 1.0]


def current_descriptor(record):
    candidates=record["candidates"]
    def mean(name): return statistics.fmean(feature_value(name,x[name]) for x in candidates)
    def std(name): return statistics.pstdev(feature_value(name,x[name]) for x in candidates)
    context=record["window_context"]; kernel=context["kernel_values"]
    kernel_names=("memory_memory_current_delta","memory_pgfault_rate","memory_pgmajfault_rate","memory_workingset_refault_file_rate",
                  "memory_pgscan_rate","memory_pgsteal_rate","memory_file_dirty_delta","memory_file_writeback_delta","cpu_usage_usec_rate",
                  "psi_memory_some_avg10_mean")
    values=[sum(x["current_active"] for x in candidates)/len(candidates),mean("delta_since_last_access"),std("delta_since_last_access"),
      mean("segment_age"),std("segment_age"),mean("segment_ema"),std("segment_ema"),mean("current_coverage"),mean("weighted_coverage"),
      mean("recent_access_count"),mean("generation_proxy"),context["anon_active_ratio"],context["anon_pages_log"],context["anon_age_log"]]
    availability=[]
    for name in kernel_names:
        available=name in kernel; values.append(math.copysign(math.log1p(abs(kernel.get(name,0))),kernel.get(name,0))); availability.append(float(available))
    return values+availability


def load_decisions(path):
    rows=[]
    with gzip.open(path,"rt") as stream:
        for line in stream:
            source=json.loads(line); candidates=[]
            for item in source["candidates"]:
                candidates.append({"identity":item["identity"],"ordinal":item["ordinal"],"features":candidate_vector(item),
                  "native_recency":item["native_recency"],"damon_hotness":item["damon_hotness"],"recent_frequency":item["recent_frequency"],
                  "future":item["future"],"normalized_position":item["normalized_position"]})
            compact={"decision_id":source["decision_id"],"session_id":source["session_id"],"app":source["app"],"app_id":source["app_id"],
              "domain_id":source["domain_id"],"window_start_ns":source["window_start_ns"],"window_end_ns":source["window_end_ns"],"candidate_hash":source["candidate_hash"],
              "candidates":candidates,"future_descriptor":future_descriptor(source),"current_descriptor":current_descriptor(source)}; rows.append(compact)
    return rows


class Standardizer:
    def fit(self,rows):
        columns=list(zip(*rows)); self.mean=[statistics.fmean(x) for x in columns]; self.scale=[statistics.pstdev(x) or 1 for x in columns]; return self
    def transform_one(self,row): return [(x-m)/s for x,m,s in zip(row,self.mean,self.scale)]
    def export(self): return {"mean":self.mean,"scale":self.scale,"fit_scope":"TRAIN_ONLY"}


def distance(left,right): return sum((a-b)**2 for a,b in zip(left,right))


def kmeans_fit(rows,k,iterations=30):
    centers=[list(rows[0])]
    while len(centers)<k:
        index=max(range(len(rows)),key=lambda i:(min(distance(rows[i],c) for c in centers),-i)); centers.append(list(rows[index]))
    for _ in range(iterations):
        labels=[min(range(k),key=lambda j:(distance(row,centers[j]),j)) for row in rows]; new=[]
        for ci in range(k):
            group=[row for row,label in zip(rows,labels) if label==ci]
            new.append([statistics.fmean(x) for x in zip(*group)] if group else centers[ci])
        if new==centers: break
        centers=new
    return centers


def assign(rows,centers): return [min(range(len(centers)),key=lambda j:(distance(row,centers[j]),j)) for row in rows]


def semantic_rule(descriptor):
    r10,r30,r60,r120,_,_,spread,width,entropy,corr,_=descriptor
    if r120==0: return "IDLE_COOLING"
    if abs(corr)>=.60 and r120>=.01: return "SEQUENTIAL_STREAM"
    if r30>=.01 and spread<=.18: return "LOCAL_REUSE"
    if entropy>=.65 and r120>=.015: return "RANDOM_WORKING_SET"
    if r120>=max(.01,r30*2): return "WORKING_SET_EXPANSION"
    if r10>0 and r120<=r10*1.2: return "WORKING_SET_CONTRACTION"
    return "MIXED"


class LinearRanker:
    def __init__(self,dimension): self.weights=[0.0]*dimension; self.bias=0.0
    def fit(self,decisions,labels=None,target=None,epochs=18):
        pairs=0
        for epoch in range(epochs):
            rate=.08/(1+epoch*.12)
            for di,decision in enumerate(decisions):
                if labels is not None and labels[di]!=target: continue
                positives=[x for x in decision["candidates"] if x["future"]["60"] is not None]; negatives=[x for x in decision["candidates"] if x["future"]["60"] is None]
                if not positives or not negatives: continue
                for pi,positive in enumerate(positives):
                    for offset in (0,len(negatives)//2,len(negatives)-1):
                        negative=negatives[(pi+offset)%len(negatives)]; diff=[a-b for a,b in zip(positive["scaled"],negative["scaled"])]
                        margin=sum(w*x for w,x in zip(self.weights,diff)); probability=1/(1+math.exp(max(-40,min(40,-margin)))); error=1-probability
                        for j,value in enumerate(diff): self.weights[j]+=rate*(error*value-1e-4*self.weights[j])
                        pairs+=epoch==0
        self.pair_count=pairs; return self
    def score(self,candidate): return sum(w*x for w,x in zip(self.weights,candidate["scaled"]))+self.bias
    def export(self): return {"type":"LINEAR_PAIRWISE_LOGISTIC","weights":self.weights,"bias":self.bias,"pair_count":self.pair_count,"parameter_hash":stable_hash(self.weights)}


def prepare_scaler(decisions):
    scaler=Standardizer().fit([c["features"] for d in decisions if d["session_id"] in TRAIN for c in d["candidates"]])
    for decision in decisions:
        for candidate in decision["candidates"]: candidate["scaled"]=scaler.transform_one(candidate["features"])
    return scaler


def order_metrics(decision,scores,horizon=60,protect_ratio=.1,reclaim_ratio=.5):
    candidates=decision["candidates"]; n=len(candidates); order=sorted(range(n),key=lambda i:(-scores[i],candidates[i]["ordinal"])); protect=max(1,math.ceil(n*protect_ratio)); reclaim=max(1,math.ceil(n*reclaim_ratio))
    relevant=[x["future"][str(horizon)] is not None for x in candidates]; positives=sum(relevant); protected=order[:protect]; reclaimed=order[-reclaim:]
    after=sum(relevant[i] for i in reclaimed); hits=sum(relevant[i] for i in protected); pairs=correct=0
    pos=[i for i,v in enumerate(relevant) if v]; neg=[i for i,v in enumerate(relevant) if not v]
    for i in pos:
        for j in neg:
            if scores[i]>scores[j]+1e-15: correct+=1
            elif abs(scores[i]-scores[j])<=1e-15: correct+=.5
            pairs+=1
    dcg=sum((1 if relevant[i] else 0)/math.log2(rank+2) for rank,i in enumerate(protected)); ideal=sum(1/math.log2(rank+2) for rank in range(min(protect,positives)))
    return {"decision_id":decision["decision_id"],"candidate_hash":decision["candidate_hash"],"reclaimed":reclaim,"future_reuse_after_reclaim":after,
      "future_reuse_saved":positives-after,"normalized_proxy":after*1000/reclaim,"auc":correct/pairs if pairs else .5,"ndcg":dcg/ideal if ideal else 0,
      "false_cold":after/positives if positives else 0,"protection_hit_rate":hits/protect,"protection_waste":1-hits/protect,
      "ranking_hash":stable_hash([candidates[i]["identity"] for i in order])}


def aggregate(rows):
    if not rows: return {"decisions":0}
    return {"decisions":len(rows),"future_reuse_after_reclaim":sum(x["future_reuse_after_reclaim"] for x in rows),"future_reuse_saved":sum(x["future_reuse_saved"] for x in rows),
      "reclaimed":sum(x["reclaimed"] for x in rows),"normalized_refault_proxy_per_1000_reclaimed":sum(x["future_reuse_after_reclaim"] for x in rows)*1000/sum(x["reclaimed"] for x in rows),
      **{name:statistics.fmean(x[name] for x in rows) for name in ("auc","ndcg","false_cold","protection_hit_rate","protection_waste")},
      "ranking_hash":stable_hash([x["ranking_hash"] for x in rows])}


def expert_scores(expert,decision): return [expert.score(x) for x in decision["candidates"]]


def train_pool(decisions,labels,classes):
    global_model=LinearRanker(len(FEATURES)).fit(decisions)
    experts={name:LinearRanker(len(FEATURES)).fit(decisions,labels,name) for name in classes}
    return global_model,experts


def evaluate_pool(decisions,labels,global_model,experts,session_set):
    global_rows=[]; oracle_rows=[]
    for decision,label in zip(decisions,labels):
        if decision["session_id"] not in session_set: continue
        global_rows.append(order_metrics(decision,expert_scores(global_model,decision))); oracle_rows.append(order_metrics(decision,expert_scores(experts[label],decision)))
    return aggregate(global_rows),aggregate(oracle_rows),global_rows,oracle_rows


def block_ci(global_rows,oracle_rows,rounds=1000):
    paired=[(g["decision_id"],g["normalized_proxy"]-o["normalized_proxy"]) for g,o in zip(global_rows,oracle_rows)]; blocks=defaultdict(list)
    for index,(decision,value) in enumerate(paired): blocks["block_%03d"%(index//50)].append(value)
    rng=random.Random(29); names=sorted(blocks); boot=[]
    for _ in range(rounds):
        values=[v for _ in names for v in blocks[rng.choice(names)]]; boot.append(statistics.fmean(values) if values else 0)
    boot.sort(); mean=statistics.fmean(x[1] for x in paired) if paired else 0
    return {"paired_mean_proxy_improvement":mean,"block_bootstrap_95_ci":[boot[int(.025*rounds)],boot[int(.975*rounds)-1]],"paired_win_rate":sum(x[1]>0 for x in paired)/len(paired) if paired else 0,"block_size_decisions":50}


def run(out):
    started=time.time_ns(); decisions=load_decisions(out/"candidate_reconstruction/decisions_generation_tail_128.jsonl.gz"); scaler=prepare_scaler(decisions)
    train_indices=[i for i,x in enumerate(decisions) if x["session_id"] in TRAIN]; taxonomy_grid={}; candidates={}
    train_desc=[decisions[i]["future_descriptor"] for i in train_indices]; desc_scaler=Standardizer().fit(train_desc); scaled_train=[desc_scaler.transform_one(x) for x in train_desc]
    semantic=[semantic_rule(x["future_descriptor"]) for x in decisions]
    taxonomy_grid["RULE"]={"support":dict(Counter(semantic[i] for i in train_indices)),"cross_app":{name:sorted({d["app"] for d,label in zip(decisions,semantic) if label==name}) for name in set(semantic)}}
    for k in range(3,9):
        centers=kmeans_fit(scaled_train,k); labels=["CLUSTER_%d"%x for x in assign([desc_scaler.transform_one(d["future_descriptor"]) for d in decisions],centers)]; candidates[k]=(centers,labels)
        support={split:dict(Counter(label for d,label in zip(decisions,labels) if d["session_id"] in sessions)) for split,sessions in (("train",TRAIN),("validation",VALID),("test",TEST))}
        taxonomy_grid["K%d"%k]={"support":support,"centers":centers,"cross_app":{name:sorted({d["app"] for d,label in zip(decisions,labels) if label==name}) for name in set(labels)}}
    quick={}
    global_model=LinearRanker(len(FEATURES)).fit(decisions)
    for k in range(3,7):
        labels=candidates[k][1]; classes=sorted(set(labels)); experts={name:LinearRanker(len(FEATURES)).fit(decisions,labels,name,epochs=10) for name in classes}
        global_val,oracle_val,_,_=evaluate_pool(decisions,labels,global_model,experts,VALID); old=global_val.get("normalized_refault_proxy_per_1000_reclaimed",0); new=oracle_val.get("normalized_refault_proxy_per_1000_reclaimed",0)
        quick[str(k)]={"global":global_val,"oracle_routed":oracle_val,"relative_proxy_gain":(old-new)/old if old else 0}
    selected_k=max((3,4,5,6),key=lambda k:(quick[str(k)]["relative_proxy_gain"],-k)); labels=candidates[selected_k][1]; classes=sorted(set(labels))
    global_model,experts=train_pool(decisions,labels,classes); global_val,oracle_val,_,_=evaluate_pool(decisions,labels,global_model,experts,VALID)
    global_test,oracle_test,global_rows,oracle_rows=evaluate_pool(decisions,labels,global_model,experts,TEST); stats=block_ci(global_rows,oracle_rows)
    baseline_test={}
    for policy,field in (("BASE_NATIVE_RECENCY","native_recency"),("BASE_DAMON_HOTNESS","damon_hotness"),("BASE_RECENT_FREQUENCY","recent_frequency")):
        rows=[order_metrics(decision,[candidate[field] for candidate in decision["candidates"]]) for decision in decisions if decision["session_id"] in TEST]
        baseline_test[policy]=aggregate(rows)
    rows=[]
    for decision in decisions:
        if decision["session_id"] not in TEST: continue
        scores=[1/(candidate["future"]["60"]+1e-9) if candidate["future"]["60"] is not None else 0 for candidate in decision["candidates"]]
        rows.append(order_metrics(decision,scores))
    baseline_test["ORACLE_FUTURE_REUSE"]=aggregate(rows)
    matrix={}; matrix_details={}
    for truth in classes:
        subset=[(d,l) for d,l in zip(decisions,labels) if d["session_id"] in TEST and l==truth]; matrix[truth]={}; matrix_details[truth]={}
        for name,model in [("GLOBAL_EXPERT",global_model)]+[("EXPERT_"+c,experts[c]) for c in classes]:
            rows=[order_metrics(d,expert_scores(model,d)) for d,_ in subset]; value=aggregate(rows); matrix[truth][name]=value.get("normalized_refault_proxy_per_1000_reclaimed"); matrix_details[truth][name]=value
    weights={"GLOBAL_EXPERT":global_model.export(),**{"EXPERT_"+name:model.export() for name,model in experts.items()}}
    distances={}
    names=sorted(weights)
    for i,left in enumerate(names):
        for right in names[i+1:]:
            a=weights[left]["weights"]; b=weights[right]["weights"]; dot=sum(x*y for x,y in zip(a,b)); norm=(sum(x*x for x in a)*sum(x*x for x in b))**.5
            distances[left+"|"+right]={"cosine_distance":1-dot/norm if norm else None,"parameter_hash_different":weights[left]["parameter_hash"]!=weights[right]["parameter_hash"]}
    old=global_test["normalized_refault_proxy_per_1000_reclaimed"]; new=oracle_test["normalized_refault_proxy_per_1000_reclaimed"]; relative=(old-new)/old if old else 0
    ci=stats["block_bootstrap_95_ci"]; expert_gate=relative>=.10 and ci[0]>0
    per_class_gain={}
    for name in classes:
        base=matrix_details[name]["GLOBAL_EXPERT"].get("normalized_refault_proxy_per_1000_reclaimed")
        matched=matrix_details[name]["EXPERT_"+name].get("normalized_refault_proxy_per_1000_reclaimed")
        per_class_gain[name]=(base-matched)/base if base and matched is not None else None
    specialization=sum(value is not None and value>0 for value in per_class_gain.values())>=3 and stats["paired_mean_proxy_improvement"]>0 and len({weights[x]["parameter_hash"] for x in weights})>1
    taxonomy={"selected_scheme":"DATA_DRIVEN_K%d"%selected_k,"selected_k":selected_k,"classes":classes,"selection_scope":"TRAIN_AND_VALIDATION_ONLY","selection_quick_expert_grid":quick,
      "rule_taxonomy":taxonomy_grid["RULE"],"taxonomy_grid":taxonomy_grid,"hybrid":"rule descriptors audited; data clusters retained because support was sufficient"}
    atomic_json(out/"workload_taxonomy/taxonomy_selection.json",taxonomy); atomic_json(out/"workload_taxonomy/workload_labels.json",{"labels":[{"decision_id":d["decision_id"],"label":l,"source":"FUTURE_KERNEL_REUSE_DESCRIPTOR"} for d,l in zip(decisions,labels)]})
    atomic_json(out/"workload_features/feature_schema.json",{"expert_features":list(FEATURES),"workload_current_dimension":len(decisions[0]["current_descriptor"]),"scaler":scaler.export(),"identity_features":False})
    atomic_json(out/"global_model/global_expert.json",global_model.export()); atomic_json(out/"experts/expert_pool.json",weights)
    atomic_json(out/"oracle_routing/oracle_expert_results.json",{"validation":{"global":global_val,"oracle":oracle_val},"test":{"baselines":baseline_test,"global":global_test,"oracle":oracle_test},"relative_proxy_gain":relative,"statistics":stats,"upper_bound_only":True,
      "comparison_contract":{"sessions":sorted(TEST),"candidate_pool":128,"horizon_seconds":60,"protect_ratio":.1,"reclaim_ratio":.5,"same_candidates":True,"same_reclaim_count":True}})
    atomic_json(out/"cross_expert_matrix/full_matrix.json",matrix_details); atomic_json(out/"cross_expert_matrix/matched_vs_global.json",{"per_class_relative_gain":per_class_gain,"overall":stats})
    atomic_json(out/"cross_expert_matrix/matched_vs_mismatched.json",{"matrix":matrix,"matched_is_best_count":sum(matrix[name]["EXPERT_"+name] is not None and matrix[name]["EXPERT_"+name]==min(v for v in matrix[name].values() if v is not None) for name in classes if any(v is not None for v in matrix[name].values()))})
    atomic_json(out/"cross_expert_matrix/expert_weight_distance.json",distances)
    for metric,filename in (("normalized_refault_proxy_per_1000_reclaimed","refault_proxy_matrix.csv"),("auc","auc_matrix.csv"),("ndcg","ndcg_matrix.csv")):
        with (out/"cross_expert_matrix"/filename).open("w",newline="") as stream:
            writer=csv.writer(stream); columns=["GLOBAL_EXPERT"]+["EXPERT_"+x for x in classes]; writer.writerow(["true_workload"]+columns)
            for truth in classes: writer.writerow([truth]+[matrix_details[truth][name].get(metric) for name in columns])
    audit={"schema_version":1,"g1_expert_gain_passed":expert_gate,"g2_specialization_passed":specialization,"relative_oracle_expert_gain":relative,"block_ci":ci,
      "per_class_matched_gain":per_class_gain,"distinct_expert_parameter_hashes":len({weights[x]["parameter_hash"] for x in weights}),"selected_k":selected_k,
      "continue_workload_prediction":relative>0,"status":"EXPERT_STAGE_PASS" if expert_gate and specialization else "PARP_PHASE29A_EXPERT_SPECIALIZATION_NOT_SUPPORTED"}
    atomic_json(out/"experts/expert_gate.json",audit)
    atomic_text(out/"analysis/expert_specialization.md","# Expert specialization\n\nOracle-routed workload experts are an upper bound. Relative test proxy gain versus global: `%.6f`. Block CI: `%s`. At least three matched classes improve: `%s`. Final expert-stage status: `%s`.\n"%(relative,ci,specialization,audit["status"]))
    atomic_json(out/"performance/expert_training.json",{"started_ns":started,"ended_ns":time.time_ns(),"global_pairs":global_model.pair_count,"expert_pairs":{name:model.pair_count for name,model in experts.items()},"model_type":"LINEAR_PAIRWISE_LOGISTIC"})
    state={"schema_version":1,"stage":"EXPERT_SPECIALIZATION_COMPLETE","timestamp_ns":time.time_ns(),"expert_gate":audit,"resume_supported":True}
    atomic_json(out/"state/state.json",state)
    with (out/"state/history.jsonl").open("a") as stream: stream.write(json.dumps(state,sort_keys=True)+"\n")
    return audit


def main():
    p=argparse.ArgumentParser(); p.add_argument("--output",type=Path,required=True); a=p.parse_args(); print(json.dumps(run(a.output),sort_keys=True))


if __name__=="__main__": main()
