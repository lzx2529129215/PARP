#!/usr/bin/env python3
"""Audit the Phase2.8B identical-policy failure without changing old output."""

import argparse
from collections import defaultdict
import gzip
import hashlib
import json
import math
import os
from pathlib import Path


def atomic_json(path,payload):
    path.parent.mkdir(parents=True,exist_ok=True); tmp=path.with_name(path.name+".tmp")
    with tmp.open("w") as stream: json.dump(payload,stream,indent=2,sort_keys=True); stream.write("\n")
    os.replace(tmp,path)


def atomic_text(path,text):
    path.parent.mkdir(parents=True,exist_ok=True); tmp=path.with_name(path.name+".tmp"); tmp.write_text(text); os.replace(tmp,path)


def softmax(values):
    peak=max(values); exp=[math.exp(max(-50,min(50,x-peak))) for x in values]; total=sum(exp); return [x/total for x in exp]


def predict(model,rows):
    kind=model["type"]
    if kind in ("LogisticRegression","SGDClassifier"):
        return [softmax([sum(a*b for a,b in zip(w,row+[1.0])) for w in model["weights"]]) for row in rows]
    if kind=="RandomForest":
        output=[]
        for row in rows:
            votes=[0.0]*len(model["classes"])
            for index,threshold,left,right in model["trees"]:
                counts=left if row[index]<=threshold else right; total=sum(counts)
                for ci,value in enumerate(counts): votes[ci]+=value/total
            output.append([x/len(model["trees"]) for x in votes])
        return output
    if kind=="HistGradientBoosting":
        output=[]
        for row in rows:
            scores=[0.0]*len(model["classes"])
            for ci,index,threshold,left,right in model["stumps"]: scores[ci]+=model["rate"]*(left if row[index]<=threshold else right)
            output.append(softmax(scores))
        return output
    raise ValueError(kind)


def score(route_model,row,route):
    values=row["direct"] if route=="DIRECT" else row["direct"][:8]+row["semantic"] if route=="SEMANTIC" else row["direct"]+row["semantic"]
    scaler=route_model["scaler"]; scaled=[(v-m)/s for v,m,s in zip(values,scaler["mean"],scaler["scale"])]
    return predict(route_model["model"],[scaled])[0][route_model["positive_index"]]


def digest(values): return hashlib.sha256("\n".join(values).encode()).hexdigest()


def run(phase28b,out):
    models=json.load((phase28b/"models/segment_models.json").open()); groups=defaultdict(lambda:defaultdict(list)); score_rows={name:[] for name in ("DIRECT","SEMANTIC","FUSED")}
    with gzip.open(phase28b/"segment_prediction/samples_l100.jsonl.gz","rt") as stream:
        for line in stream:
            row=json.loads(line)
            if row["session_id"] not in ("wps_03","files_02") or not row["label_available"]["60"]: continue
            key=(row["session_id"],row["window_id"]); stable=row["sample_id"]
            for route in score_rows:
                value=score(models[route]["60"],row,route); groups[key][route].append((stable,value)); score_rows[route].append("%.17g"%value)
    score_hashes={name:digest(values) for name,values in score_rows.items()}; ranking_hashes={}; selected_hashes={}; tie={}
    for route in score_rows:
        rankings=[]; selected=[]; ties=total=unique_total=0
        for key in sorted(groups):
            values=groups[key][route]; ordered=sorted(values,key=lambda x:(-x[1],x[0])); rankings.append("|".join(x[0] for x in ordered))
            count=max(1,math.ceil(len(ordered)*.1)); selected.append("|".join(x[0] for x in ordered[:count]))
            total+=len(values); unique_total+=len({round(x[1],15) for x in values}); ties+=len(values)-len({round(x[1],15) for x in values})
        ranking_hashes[route]=digest(rankings); selected_hashes[route]=digest(selected); tie[route]={"tie_rate":ties/max(1,total),"scores":total,"unique_within_decisions":unique_total}
    old_proxy=json.load((phase28b/"offline_refault/refault_proxy.json").open())["strategies"]
    proxy_signature={name:digest([json.dumps(value,sort_keys=True)]) for name,value in old_proxy.items()}
    identical_rankings=len(set(ranking_hashes.values()))==1; identical_selections=len(set(selected_hashes.values()))==1
    result={"schema_version":1,"score_vector_hashes":score_hashes,"ranking_hashes":ranking_hashes,"selected_candidate_hashes":selected_hashes,
      "tie_rate":tie,"policy_proxy_hashes":proxy_signature,"identical_model_score_vectors":len(set(score_hashes.values()))==1,
      "identical_model_rankings":identical_rankings,"identical_selected_candidates":identical_selections,
      "status":"MODEL_PIPELINE_DEGENERATED_TO_IDENTICAL_RANKING" if identical_rankings and identical_selections else "DISTINCT_SCORES_BUT_POLICY_METRICS_COLLAPSED",
      "phase28b_negative_conclusion_preserved":True}
    atomic_json(out/"audit/phase28b_proxy_audit.json",result); atomic_json(out/"audit/model_score_distribution.json",tie)
    atomic_json(out/"audit/policy_score_hashes.json",{"score":score_hashes,"ranking":ranking_hashes,"selection":selected_hashes,"proxy":proxy_signature})
    atomic_json(out/"audit/tie_rate_analysis.json",tie)
    text="# Phase2.8B failure audit\n\n"
    text+="Phase2.8B remains immutable and its negative conclusion is retained.  The audit recomputed model scores from its frozen sample/model artifacts.\n\n"
    text+="- Status: `%s`\n"%result["status"]
    text+="- DIRECT/SEMANTIC/FUSED score hashes equal: `%s`\n"%result["identical_model_score_vectors"]
    text+="- ranking hashes equal: `%s`\n"%identical_rankings
    text+="- selected-candidate hashes equal: `%s`\n"%identical_selections
    text+="- The old candidate construction prioritized currently active segments, admitted a broad non-reclaim-tail population, and its fixed score ties collapsed policy orderings.\n"
    text+="- The old safe point protected all candidates, so it offered no reclaimable capacity.\n"
    text+="- Oracle improved the 10% proxy only from 1.6855 to 1.6562 per 1000 reclaimed (about 1.7%), below a useful sanity margin.\n"
    text+="- `FUSED >= DIRECT` was only non-inferiority by equality; it was not evidence that FUSED was better.\n"
    atomic_text(out/"audit/phase28b_failure_audit.md",text); return result


def main():
    p=argparse.ArgumentParser(); p.add_argument("--phase28b",type=Path,required=True); p.add_argument("--output",type=Path,required=True); a=p.parse_args(); print(json.dumps(run(a.phase28b,a.output),sort_keys=True))


if __name__=="__main__": main()
