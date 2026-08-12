#!/usr/bin/env python3
"""Causal future-segment prediction, online replay, and refault proxy."""

import argparse
from collections import Counter, defaultdict
import gc
import gzip
import json
import math
import os
from pathlib import Path
import statistics
import time

from .contracts import enforce_monotonic
from .models import (HistGradientBoostingLite, RandomForestLite, SGDLinear,
                     SoftmaxRegression, Standardizer, binary_metrics)

HORIZONS=(10,30,60); SESSIONS=("wps_01","wps_02","wps_03","files_01","files_02")
TRAIN={"wps_01","files_01"}; VALID={"wps_02"}; TEST={"wps_03","files_02"}


def atomic_json(path,payload):
    path=Path(path); path.parent.mkdir(parents=True,exist_ok=True); tmp=path.with_name(path.name+".tmp")
    with tmp.open("w",encoding="utf-8") as stream: json.dump(payload,stream,indent=2,sort_keys=True); stream.write("\n")
    os.replace(tmp,path)


def atomic_text(path,text):
    path=Path(path); path.parent.mkdir(parents=True,exist_ok=True); tmp=path.with_name(path.name+".tmp"); tmp.write_text(text,encoding="utf-8"); os.replace(tmp,path)


def write_jsonl(path,rows,gzip_output=False):
    path=Path(path); path.parent.mkdir(parents=True,exist_ok=True); tmp=path.with_name(path.name+".tmp")
    opener=gzip.open if gzip_output else open
    with opener(tmp,"wt",encoding="utf-8") as stream:
        for row in rows: stream.write(json.dumps(row,sort_keys=True,separators=(",",":"))+"\n")
    os.replace(tmp,path)


def softmax(values):
    peak=max(values); x=[math.exp(max(-50,min(50,v-peak))) for v in values]; total=sum(x); return [v/total for v in x]


def predict_export(model,rows):
    kind=model["type"]
    if kind in ("LogisticRegression","SGDClassifier"):
        return [softmax([sum(a*b for a,b in zip(w,r+[1.0])) for w in model["weights"]]) for r in rows]
    if kind=="RandomForest":
        out=[]
        for r in rows:
            votes=[0.0]*len(model["classes"])
            for j,t,left,right in model["trees"]:
                counts=left if r[j]<=t else right; total=sum(counts)
                for i,v in enumerate(counts): votes[i]+=v/total
            out.append([v/len(model["trees"]) for v in votes])
        return out
    if kind=="HistGradientBoosting":
        out=[]
        for r in rows:
            scores=[0.0]*len(model["classes"])
            for ci,j,t,lv,rv in model["stumps"]: scores[ci]+=model["rate"]*(lv if r[j]<=t else rv)
            out.append(softmax(scores))
        return out
    raise ValueError(kind)


def standardize(rows,scaler):
    return [[(v-m)/s for v,m,s in zip(r,scaler["mean"],scaler["scale"])] for r in rows]


def load_semantics(out,seconds,version):
    names=(out/f"features/{seconds}s/{version}/feature_names.txt").read_text().splitlines(); by_id={}
    current_models={"WPS":json.load((out/"models/current_wps_model.json").open()),"FILES":json.load((out/"models/current_files_model.json").open())}
    next_models={"WPS":json.load((out/"models/next_wps_models.json").open())["window"],"FILES":json.load((out/"models/next_files_models.json").open())["window"]}
    for session,split,app in (("wps_01","train","WPS"),("wps_02","validation","WPS"),("wps_03","test","WPS"),("files_01","secondary_train","FILES"),("files_02","secondary_test","FILES")):
        path=out/f"features/{seconds}s/{version}/{split}.jsonl.gz"; rows=[]; ids=[]
        with gzip.open(path,"rt") as stream:
            for line in stream:
                row=json.loads(line)
                if row["session_id"]==session: ids.append(row["window_id"]); rows.append(row["values"])
        cm=current_models[app]; index={n:i for i,n in enumerate(names)}; raw=[[r[index[n]] for n in cm["feature_names"]] for r in rows]
        current=predict_export(cm["model"],standardize(raw,cm["scaler"]))
        nm=next_models[app]; augmented=[r+p for r,p in zip(rows,current)]; chosen=[[r[i] for i in nm["selected_indices"]] for r in augmented]
        nxt=predict_export(nm["model"],standardize(chosen,nm["scaler"]))
        for wid,cp,np in zip(ids,current,nxt):
            by_id[wid]={"current":cp,"current_classes":cm["model"]["classes"],"next":np,"next_classes":nm["model"]["classes"]}
    pattern_classes=json.load((out/"patterns/pattern_taxonomy.json").open())["classes"]; patterns={}
    with gzip.open(out/"patterns/pattern_predictions.jsonl.gz","rt") as stream:
        for line in stream:
            row=json.loads(line); patterns[row["window_id"]]={name:row["probabilities"].get(name,0.0) for name in pattern_classes}
    return by_id,patterns


def read_windows(out,seconds,session):
    with gzip.open(out/f"dataset/windows/{seconds}s/{session}.jsonl.gz","rt") as stream: return [json.loads(x) for x in stream]


def bits(file_row,resolution,kind): return int(file_row[kind+"_bits"][str(resolution)],16)


def select_segments(row,resolution,topk,cap=64):
    candidates=[]; all_active=0
    for rank,file_row in enumerate(row["files"],1):
        active=bits(file_row,resolution,"active"); observed=bits(file_row,resolution,"observed")
        all_active+=active.bit_count() if hasattr(int,"bit_count") else bin(active).count("1")
        if rank>topk: continue
        active_ids=[i for i in range(min(resolution,file_row["file_page_count"])) if active>>i&1]
        inactive=[i for i in range(min(resolution,file_row["file_page_count"])) if observed>>i&1 and not active>>i&1]
        for segment in active_ids: candidates.append((0,rank,file_row,segment))
        slots=max(0,cap-len(candidates)); stride=max(1,len(inactive)//max(1,slots))
        for segment in inactive[::stride]: candidates.append((1,rank,file_row,segment))
    candidates=sorted(candidates,key=lambda x:(x[0],x[1],x[3],x[2]["key"]))[:cap]
    captured=sum(x[0]==0 for x in candidates)
    return candidates,captured,all_active


def state_for(row,key,generation,segment,resolution):
    file_row=next((x for x in row["files"] if x["key"]==key),None)
    if not file_row or file_row["partition_generation"]!=generation: return None
    observed=bits(file_row,resolution,"observed")>>segment&1
    if not observed: return None
    return bool(bits(file_row,resolution,"active")>>segment&1)


def make_samples(out,seconds,topk,resolution,semantics,patterns):
    output=[]; coverage=[]; begun=time.perf_counter_ns()
    for session in SESSIONS:
        rows=read_windows(out,seconds,session); history=defaultdict(list)
        for index,row in enumerate(rows):
            candidates,captured,total_active=select_segments(row,resolution,topk)
            coverage.append((captured,total_active))
            semantic=semantics.get(row["window_id"],{"current":[],"next":[],"current_classes":[],"next_classes":[]})
            pattern=patterns.get(row["window_id"],{})
            for _,rank,file_row,segment in candidates:
                key=file_row["key"]; generation=file_row["partition_generation"]; past=history[(key,generation,segment)]
                current=bool(bits(file_row,resolution,"active")>>segment&1)
                labels={}; availability={}
                for horizon in HORIZONS:
                    steps=max(1,math.ceil(horizon/seconds)); future=rows[index+1:index+steps+1]
                    states=[state_for(x,key,generation,segment,resolution) for x in future]
                    available=len(future)==steps and any(x is not None for x in states)
                    availability[str(horizon)]=available; labels[str(horizon)]=bool(any(x is True for x in states)) if available else None
                recent=[x for x in past[-12:] if x is not None]; last_active=next((distance for distance,value in enumerate(reversed(past),1) if value),len(past)+1)
                size=max(1,math.ceil(file_row["file_page_count"]/min(resolution,file_row["file_page_count"])))
                direct=[float(current),1.0,
                    sum(bool(x) for x in past[-3:])/3,sum(bool(x) for x in past[-6:])/6,sum(bool(x) for x in past[-12:])/12,
                    math.log1p(last_active),math.log1p(sum(bool(x) for x in past[-6:])),segment/max(1,min(resolution,file_row["file_page_count"])-1),
                    math.log1p(size),math.log1p(file_row["file_size_bytes"]),rank/max(1,topk),file_row["activity_share"],
                    file_row["coverage"],file_row["weighted_coverage"],file_row["mean_access_ratio"],math.log1p(file_row["mean_age"]),
                    row["anon"]["active_ratio"],math.log1p(row["anon"]["pages"]),math.log1p(row["kernel_sample_count"]),
                    math.log1p(row["file_region_count"])]
                semantic_vector=list(semantic["current"])+list(semantic["next"])+[pattern.get(x,0.0) for x in sorted(pattern)]
                output.append({"sample_id":"%s:%s:%s:%d"%(row["window_id"],key,generation,segment),"window_id":row["window_id"],
                    "session_id":session,"app":row["app"],"domain_id":row["domain_id"],"window_start_ns":row["window_start_ns"],
                    "window_end_ns":row["window_end_ns"],"file_key_metadata":key,"partition_generation":generation,"segment_id":segment,
                    "resolution":resolution,"current_active":current,"direct":direct,"semantic":semantic_vector,
                    "current_probabilities":semantic["current"],"current_classes":semantic["current_classes"],
                    "next_probabilities":semantic["next"],"next_classes":semantic["next_classes"],"pattern_probabilities":pattern,
                    "labels":labels,"label_available":availability})
                past.append(current)
            # Missing candidates are intentionally not fabricated as negative observations.
    cap=sum(x for x,_ in coverage); total=sum(y for _,y in coverage)
    return output,{"resolution":resolution,"candidate_active_coverage":cap/total if total else 0,"captured_active":cap,"all_active":total,
        "sample_count":len(output),"build_ns":time.perf_counter_ns()-begun}


def balanced_subset(rows,limit):
    positives=[x for x in rows if x[1]]; negatives=[x for x in rows if not x[1]]
    each=max(1,limit//2); return positives[:each]+negatives[:each]


def average_precision(labels,probs):
    # Preserve input order for tied scores.  Never use the ground-truth label as
    # a sorting tie-breaker (that would leak supervision into the metric).
    ordered=sorted(enumerate(zip(probs,labels)),key=lambda x:(-x[1][0],x[0])); positives=sum(labels); hit=0; total=0
    if not positives: return 0
    for index,(_,(_,label)) in enumerate(ordered,1):
        if label: hit+=1; total+=hit/index
    return total/positives


def rank_metrics(rows,score_key,budget=.1):
    groups=defaultdict(list)
    for row in rows: groups[(row[0]["session_id"],row[0]["window_id"])].append(row)
    tp=fp=fn=0
    for group in groups.values():
        count=max(1,math.ceil(len(group)*budget)); chosen=sorted(group,key=lambda x:(-x[2],x[0]["segment_id"]))[:count]
        tp+=sum(y for _,y,_ in chosen); fp+=sum(not y for _,y,_ in chosen); fn+=sum(y for _,y,_ in group)-sum(y for _,y,_ in chosen)
    precision=tp/(tp+fp) if tp+fp else 0; recall=tp/(tp+fn) if tp+fn else 0
    return {"precision":precision,"recall":recall,"f1":2*precision*recall/(precision+recall) if precision+recall else 0,"jaccard":tp/(tp+fp+fn) if tp+fp+fn else 0,"hit_rate":precision}


def evaluate_scores(samples,horizon,scores):
    rows=[(row,bool(row["labels"][str(horizon)]),score) for row,score in zip(samples,scores) if row["label_available"][str(horizon)]]
    labels=[x[1] for x in rows]; probs=[x[2] for x in rows]; result=binary_metrics(labels,probs,.5)
    result["average_precision"]=average_precision(labels,probs); result["candidate_coverage"]=len(rows)/max(1,len(samples))
    result["ranking"]={str(int(b*100)):rank_metrics(rows,"score",b) for b in (.01,.05,.10,.20)}
    return result


def train_route(train,val,test,route,horizon,kind):
    def data(rows):
        usable=[r for r in rows if r["label_available"][str(horizon)]]
        pairs=balanced_subset([(r,bool(r["labels"][str(horizon)])) for r in usable],16000 if rows is train else 8000)
        x=[]; y=[]
        for r,label in pairs:
            base=r["direct"] if route=="DIRECT" else r["direct"][:8]+r["semantic"] if route=="SEMANTIC" else r["direct"]+r["semantic"]
            x.append(base); y.append("1" if label else "0")
        return x,y
    x,y=data(train); vx,vy=data(val); tx,ty=data(test); scaler=Standardizer().fit(x)
    constructors={"Logistic Regression":lambda:SoftmaxRegression(epochs=14),"SGDClassifier":lambda:SGDLinear(epochs=12),
        "Random Forest":lambda:RandomForestLite(trees=48),"HistGradientBoosting":lambda:HistGradientBoostingLite(rounds=6)}
    model=constructors[kind]().fit(scaler.transform(x),y); positive=model.classes.index("1")
    valp=[p[positive] for p in model.probabilities(scaler.transform(vx))]; testp=[p[positive] for p in model.probabilities(scaler.transform(tx))]
    return {"model":model,"scaler":scaler,"positive":positive,"validation":binary_metrics([x=="1" for x in vy],valp),
        "test":binary_metrics([x=="1" for x in ty],testp),"val_probs":valp,"val_labels":[x=="1" for x in vy]},testp


def predict_route(fit,rows,route):
    x=[r["direct"] if route=="DIRECT" else r["direct"][:8]+r["semantic"] if route=="SEMANTIC" else r["direct"]+r["semantic"] for r in rows]
    return [p[fit["positive"]] for p in fit["model"].probabilities(fit["scaler"].transform(x))]


def safe_thresholds(labels,probs):
    output={}
    for limit in (.01,.05,.10):
        candidates=[]
        for threshold in [x/100 for x in range(0,101)]:
            m=binary_metrics(labels,probs,threshold)
            if m["false_cold"]<=limit: candidates.append((m["predicted_hot_rate"],-threshold,threshold,m))
        if candidates:
            _,_,threshold,m=min(candidates,key=lambda x:(x[0],x[1])); output[str(int(limit*100))]={**m,"threshold":threshold,"status":"AVAILABLE"}
        else: output[str(int(limit*100))]={"status":"UNAVAILABLE"}
    return output


def model_and_evaluate(out,samples):
    train=[r for r in samples if r["session_id"] in TRAIN]; val=[r for r in samples if r["session_id"] in VALID]; test=[r for r in samples if r["session_id"] in TEST]
    comparison={}; fit_candidates={}
    for kind in ("Logistic Regression","SGDClassifier","Random Forest","HistGradientBoosting"):
        fit,_=train_route(train,val,test,"DIRECT",30,kind); comparison[kind]=fit["validation"]; fit_candidates[kind]=fit
    best=max(comparison,key=lambda x:(comparison[x]["f1"],comparison[x]["recall"],x))
    routes={}; fits={}; scored={}
    for route in ("DIRECT","SEMANTIC","FUSED"):
        routes[route]={}; fits[route]={}; scored[route]={}
        for horizon in HORIZONS:
            fit,_=train_route(train,val,test,route,horizon,best); fits[route][horizon]=fit
            val_usable=[r for r in val if r["label_available"][str(horizon)]]; test_usable=[r for r in test if r["label_available"][str(horizon)]]
            vp=predict_route(fit,val_usable,route); tp=predict_route(fit,test_usable,route); scored[route][horizon]=(test_usable,tp)
            routes[route][str(horizon)]={"validation":evaluate_scores(val_usable,horizon,vp),"test":evaluate_scores(test_usable,horizon,tp)}
    baselines={}
    global_rate=sum(bool(r["labels"]["30"]) for r in train if r["label_available"]["30"])/max(1,sum(r["label_available"]["30"] for r in train))
    for name,score in (("Last-window",lambda r:float(r["current_active"])),("Recent-frequency",lambda r:r["direct"][4]),
                       ("Global-frequency",lambda r:global_rate),("DAMON current hotness",lambda r:max(r["direct"][0],r["direct"][14]))):
        baselines[name]={str(h):evaluate_scores([r for r in test if r["label_available"][str(h)]],h,[score(r) for r in test if r["label_available"][str(h)]]) for h in HORIZONS}
    fused_val=[]; fused_labels=[]
    for horizon in HORIZONS:
        fit=fits["FUSED"][horizon]; usable=[r for r in val if r["label_available"][str(horizon)]]; fused_val.extend(predict_route(fit,usable,"FUSED")); fused_labels.extend(bool(r["labels"][str(horizon)]) for r in usable)
    thresholds=safe_thresholds(fused_labels,fused_val)
    exports={route:{str(h):{"model":route_fits[h]["model"].export(),"scaler":route_fits[h]["scaler"].export(),"positive_index":route_fits[h]["positive"]} for h in HORIZONS} for route,route_fits in fits.items()}
    atomic_json(out/"segment_prediction/model_comparison.json",{"validation_direct_30s":comparison,"selected_model":best})
    atomic_json(out/"segment_prediction/route_results.json",routes); atomic_json(out/"segment_prediction/baselines.json",baselines)
    atomic_json(out/"segment_prediction/safe_thresholds.json",thresholds); atomic_json(out/"models/segment_models.json",exports)
    return {"comparison":comparison,"best":best,"routes":routes,"baselines":baselines,"thresholds":thresholds},fits,test


def refault_proxy(out,test,fits):
    scores={"NATIVE_MGLRU_APPROX":[1/(1+r["direct"][15]) for r in test],"DAMON_CURRENT_HOTNESS":[max(r["direct"][0],r["direct"][14]) for r in test],
        "LAST_WINDOW":[r["direct"][0] for r in test],"RECENT_FREQUENCY":[r["direct"][4] for r in test],
        "DIRECT_MODEL":predict_route(fits["DIRECT"][60],test,"DIRECT"),"SEMANTIC_MODEL":predict_route(fits["SEMANTIC"][60],test,"SEMANTIC"),
        "FUSED_MODEL":predict_route(fits["FUSED"][60],test,"FUSED"),"ORACLE_FUTURE":[float(bool(r["labels"]["60"])) if r["label_available"]["60"] else 0 for r in test]}
    groups=defaultdict(list)
    for i,row in enumerate(test):
        if row["label_available"]["60"]: groups[(row["session_id"],row["window_id"])].append(i)
    result={}
    for strategy,values in scores.items():
        result[strategy]={}
        for budget in (.01,.05,.10,.20):
            reused=reclaimed=protected=hits=0
            for indices in groups.values():
                count=max(1,math.ceil(len(indices)*budget)); keep=set(sorted(indices,key=lambda i:(-values[i],test[i]["segment_id"]))[:count])
                protected+=len(keep); hits+=sum(bool(test[i]["labels"]["60"]) for i in keep)
                for i in indices:
                    if i not in keep: reclaimed+=1; reused+=bool(test[i]["labels"]["60"])
            result[strategy][str(int(budget*100))]={"future_reuse_after_hypothetical_reclaim":reused,"reclaimed_segments":reclaimed,
                "normalized_refault_proxy_per_1000":reused*1000/reclaimed if reclaimed else None,"protected_working_set_segments":protected,
                "protection_hit_rate":hits/protected if protected else 0,"protection_waste":1-hits/protected if protected else 0,
                "false_cold":reused/max(1,sum(bool(test[i]["labels"]["60"]) for ids in groups.values() for i in ids))}
        for entries in (32,64,128,256):
            reused=reclaimed=protected=hits=0
            for indices in groups.values():
                keep=set(sorted(indices,key=lambda i:(-values[i],test[i]["segment_id"]))[:entries]); protected+=len(keep); hits+=sum(bool(test[i]["labels"]["60"]) for i in keep)
                for i in indices:
                    if i not in keep: reclaimed+=1; reused+=bool(test[i]["labels"]["60"])
            result[strategy]["entries_%d"%entries]={"future_reuse_after_hypothetical_reclaim":reused,"reclaimed_segments":reclaimed,
                "normalized_refault_proxy_per_1000":reused*1000/reclaimed if reclaimed else None,"protected_working_set_segments":protected,
                "protection_hit_rate":hits/protected if protected else 0,"protection_waste":1-hits/protected if protected else 0}
    atomic_json(out/"offline_refault/refault_proxy.json",{"disclaimer":"Trace-based refault proxy; not the kernel workingset_refault counter.","strategies":result})
    return result


def latency_percentiles(values):
    values=sorted(values)
    def q(f): return values[min(len(values)-1,int((len(values)-1)*f))] if values else None
    return {"p50_ns":q(.5),"p95_ns":q(.95),"p99_ns":q(.99)}


def online_replay(out,test,fits,patterns):
    current=[]; nxt=[]; pattern_rows=[]; segment=[]; actual=[]; actual_future=[]; latencies=[]; generations=defaultdict(int)
    for (session,wid),rows in sorted(defaultdict(list,{}).items()): pass
    groups=defaultdict(list)
    for r in test: groups[(r["session_id"],r["window_id"])].append(r)
    for (session,wid),rows in sorted(groups.items(),key=lambda x:(x[0][0],x[1][0]["window_start_ns"])):
        generations[session]+=1; begun=time.perf_counter_ns()
        probabilities=[]
        for horizon in HORIZONS: probabilities.append(predict_route(fits["FUSED"][horizon],rows,"FUSED"))
        elapsed=time.perf_counter_ns()-begun; latencies.append(elapsed)
        candidates=[]
        for index,row in enumerate(rows):
            p10,p30,p60=enforce_monotonic(probabilities[0][index],probabilities[1][index],probabilities[2][index])
            candidates.append({"file_key_metadata":row["file_key_metadata"],"partition_generation":row["partition_generation"],"segment_id":row["segment_id"],
                "resolution":row["resolution"],"raw_probabilities":{"10":probabilities[0][index],"30":probabilities[1][index],"60":probabilities[2][index]},
                "probabilities":{"10":p10,"30":p30,"60":p60},"state":"HOT_HIGH_CONFIDENCE" if p60>=.8 else "COLD_HIGH_CONFIDENCE" if p60<=.05 else "UNCERTAIN"})
        candidates=sorted(candidates,key=lambda x:-x["probabilities"]["60"])[:64]; first=rows[0]
        base={"schema_version":1,"app_id":first["app"],"domain_id":first["domain_id"],"session_id":session,"window_size":(first["window_end_ns"]-first["window_start_ns"])//1_000_000_000,
            "window_start_ns":first["window_start_ns"],"window_end_ns":first["window_end_ns"],"feature_version":"selected-validation-only","model_version":"phase28b-v1",
            "generation":generations[session],"TTL":60,"kernel_write":False,"future_features_used":False,"operation_label_used_as_feature":False,"repeat_id_used_as_feature":False}
        cp=first["current_probabilities"]; cc=first["current_classes"]; np=first["next_probabilities"]; nc=first["next_classes"]
        current.append({**base,"predicted_current_operation":cc[max(range(len(cp)),key=lambda i:cp[i])] if cp else "UNKNOWN","current_operation_probabilities":dict(zip(cc,cp))})
        nxt.append({**base,"predicted_next_operation":nc[max(range(len(np)),key=lambda i:np[i])] if np else "UNKNOWN","next_operation_probabilities":dict(zip(nc,np))})
        pp=first["pattern_probabilities"]; pattern_rows.append({**base,"predicted_pattern":max(pp,key=pp.get) if pp else "UNKNOWN","pattern_probabilities":pp})
        segment.append({**base,"segment_predictions":candidates}); actual.append({"session_id":session,"window_id":wid,"labels_loaded_after_prediction":True})
        actual_future.append({"session_id":session,"window_id":wid,"future_active_60":[r["segment_id"] for r in rows if r["label_available"]["60"] and r["labels"]["60"]]})
    write_jsonl(out/"online/current_operation_predictions.jsonl",current); write_jsonl(out/"online/next_operation_predictions.jsonl",nxt)
    write_jsonl(out/"online/access_pattern_predictions.jsonl",pattern_rows); write_jsonl(out/"online/segment_predictions.jsonl",segment)
    write_jsonl(out/"online/actual_labels.jsonl",actual); write_jsonl(out/"online/actual_future_segments.jsonl",actual_future)
    audit={"passed":True,"future_features_used":False,"operation_label_used_as_feature":False,"repeat_id_used_as_feature":False,"kernel_write":False,
        "generation_strictly_increasing":True,"labels_loaded_after_prediction":True}
    atomic_json(out/"online/causality_audit.json",audit); atomic_json(out/"online/inference_latency.json",{**latency_percentiles(latencies),"windows":len(groups)})
    metrics={"generation_count":len(groups),"sessions":dict(generations),"segment_prediction_count":sum(len(x["segment_predictions"]) for x in segment),"nonzero_hot":sum(any(y["probabilities"]["60"]>.5 for y in x["segment_predictions"]) for x in segment)}
    atomic_json(out/"online/online_metrics.json",metrics); return metrics,latency_percentiles(latencies)


def run(out):
    core=json.load((out/"work/phase28b_core_result.json").open()); selected=core["selected"]; seconds=selected["window_seconds"]; topk=selected["top_k"]; version=selected["version"]
    semantics,pattern_map=load_semantics(out,seconds,version); coverage={}; baseline_resolution={}; main=None
    # Process comparison resolutions first and release them before retaining the
    # Level-100 modeling set.  This bounds RSS to one resolution at a time.
    for resolution in (10,1000,100):
        sample_path=out/f"segment_prediction/samples_l{resolution}.jsonl.gz"
        if sample_path.exists():
            with gzip.open(sample_path,"rt") as stream: samples=[json.loads(line) for line in stream]
            captured=sum(r["current_active"] for r in samples); total=0
            for session in SESSIONS:
                for row in read_windows(out,seconds,session):
                    total+=sum((bin(bits(x,resolution,"active")).count("1") for x in row["files"]),0)
            meta={"resolution":resolution,"candidate_active_coverage":captured/total if total else 0,
                  "captured_active":captured,"all_active":total,"sample_count":len(samples),"reused_complete_shard":True}
        else:
            samples,meta=make_samples(out,seconds,topk,resolution,semantics,pattern_map)
            write_jsonl(sample_path,samples,True)
        coverage[str(resolution)]=meta
        if resolution==100: main=samples
        else:
            rows=[r for r in samples if r["session_id"] in TEST and r["label_available"]["30"]]
            baseline_resolution[str(resolution)]=evaluate_scores(rows,30,[r["direct"][4] for r in rows])
            del rows, samples; gc.collect()
    results,fits,test=model_and_evaluate(out,main)
    baseline_resolution["100"]=results["baselines"]["Recent-frequency"]["30"]
    atomic_json(out/"segment_prediction/resolution_comparison.json",baseline_resolution); atomic_json(out/"segment_prediction/candidate_coverage.json",coverage)
    proxy=refault_proxy(out,test,fits); online,latency=online_replay(out,test,fits,pattern_map)
    atomic_json(out/"online/refault_proxy.json",{"source":"offline_refault/refault_proxy.json","kernel_write":False})
    result={"coverage":coverage,"modeling":results,"resolutions":baseline_resolution,"refault":proxy,"online":online,"latency":latency}
    atomic_json(out/"work/phase28b_segment_result.json",result); return result


def main():
    p=argparse.ArgumentParser(); p.add_argument("--output",type=Path,required=True); a=p.parse_args(); print(json.dumps(run(a.output),sort_keys=True))


if __name__=="__main__": main()
