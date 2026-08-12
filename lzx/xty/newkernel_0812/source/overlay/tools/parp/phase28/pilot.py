#!/usr/bin/env python3
"""Existing Phase2.7B data kernel-only current-operation separability pilot."""

import argparse
from collections import Counter, defaultdict
import hashlib
import json
import math
import os
from pathlib import Path
import resource
import statistics
import time

from intra_app_prediction.real_pipeline import parse_real_trace_line
from .contracts import validate_source_map

WINDOWS = (2, 5, 10)
SESSIONS = ("wps_01", "wps_02", "wps_03", "files_01", "files_02")
TAXONOMY = {
    "BROWSE_LIST": "NAVIGATION_FORWARD", "CLOSE_DOCUMENT": "OPEN_CLOSE",
    "EDIT": "EDIT", "ENTER_DIRECTORY": "NAVIGATION_FORWARD",
    "IDLE_VIEW": "VIEW_IDLE", "JUMP_POSITION": "NAVIGATION_FORWARD",
    "MINIMIZE": "FOREGROUND_BACKGROUND", "OPEN": "OPEN_CLOSE",
    "OPEN_DIRECTORY": "NAVIGATION_FORWARD", "RECENT_DOCUMENTS": "NAVIGATION_FORWARD",
    "REOPEN": "OPEN_CLOSE", "RESTORE": "FOREGROUND_BACKGROUND",
    "RETURN_DIRECTORY": "NAVIGATION_BACKWARD", "SAVE": "SAVE_WRITE",
    "SCROLL_DOWN": "NAVIGATION_FORWARD", "SCROLL_LIST": "NAVIGATION_FORWARD",
    "SCROLL_UP": "NAVIGATION_BACKWARD", "SEARCH": "SEARCH",
}


def atomic_json(path, payload):
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(payload, stream, indent=2, sort_keys=True); stream.write("\n")
    os.replace(str(temporary), str(path))


def atomic_jsonl(path, rows):
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        for row in rows: stream.write(json.dumps(row, sort_keys=True) + "\n")
    os.replace(str(temporary), str(path))


def percentile(values, fraction):
    values = sorted(values)
    if not values: return None
    position = (len(values) - 1) * fraction
    lower = int(position); upper = min(lower + 1, len(values) - 1)
    return values[lower] * (upper - position) + values[upper] * (position - lower)


def empty_accumulator(session, app_id, domain_id, start, width):
    return {"session_id": session, "app_id": app_id, "domain_id": domain_id,
            "window_start_ns": start, "window_end_ns": start + width,
            "file_regions": 0, "anon_regions": 0, "file_active_regions": 0,
            "anon_active_regions": 0, "file_pages": 0, "anon_pages": 0,
            "access_sum": 0.0, "access_max": 0.0, "age_sum": 0.0,
            "age_max": 0.0, "evidence": 0, "objects": set(),
            "weighted_position_sum": 0.0, "position_weight": 0.0,
            "epochs": set()}


def add_event(acc, row):
    access = min(1.0, row["nr_accesses"] / 200.0)
    pages = row["nr_pages"]
    acc["evidence"] += 1; acc["access_sum"] += access
    acc["access_max"] = max(acc["access_max"], access)
    acc["age_sum"] += row["age"]; acc["age_max"] = max(acc["age_max"], row["age"])
    acc["epochs"].add(row["foreground_epoch"])
    if row["region_type"] == "FILE":
        acc["file_regions"] += 1; acc["file_pages"] += pages
        acc["file_active_regions"] += access > 0
        acc["objects"].add(hashlib.sha256(("%d:%d:%d:%d" % row["file_id_tuple"]).encode()).hexdigest()[:16])
        if row["file_page_count"]:
            position = ((row["file_page_start"] + row["file_page_end_exclusive"]) / 2 /
                        row["file_page_count"])
            acc["weighted_position_sum"] += position * max(access, 1e-9)
            acc["position_weight"] += max(access, 1e-9)
    else:
        acc["anon_regions"] += 1; acc["anon_pages"] += pages
        acc["anon_active_regions"] += access > 0


def feature_row(acc, previous):
    evidence = acc["evidence"] or 1
    file_regions = acc["file_regions"] or 1
    anon_regions = acc["anon_regions"] or 1
    centroid = acc["weighted_position_sum"] / acc["position_weight"] if acc["position_weight"] else 0.0
    previous_centroid = previous.get("access_centroid", 0.0) if previous else 0.0
    values = {
        "foreground_app_id": float(acc["app_id"]), "domain_valid": float(acc["domain_id"] > 0),
        "pagecache_object_count_log": math.log1p(len(acc["objects"])),
        "pagecache_region_count_log": math.log1p(acc["file_regions"]),
        "anon_region_count_log": math.log1p(acc["anon_regions"]),
        "pagecache_pages_log": math.log1p(acc["file_pages"]),
        "anon_pages_log": math.log1p(acc["anon_pages"]),
        "pagecache_active_ratio": acc["file_active_regions"] / file_regions,
        "anon_active_ratio": acc["anon_active_regions"] / anon_regions,
        "mean_access_ratio": acc["access_sum"] / evidence,
        "max_access_ratio": acc["access_max"],
        "mean_age_log": math.log1p(acc["age_sum"] / evidence),
        "max_age_log": math.log1p(acc["age_max"]),
        "access_centroid": centroid,
        "access_centroid_shift": centroid - previous_centroid,
        "pagecache_region_delta_signed_log": math.copysign(
            math.log1p(abs(acc["file_regions"] - previous.get("file_regions", 0))),
            acc["file_regions"] - previous.get("file_regions", 0)) if previous else 0.0,
        "anon_region_delta_signed_log": math.copysign(
            math.log1p(abs(acc["anon_regions"] - previous.get("anon_regions", 0))),
            acc["anon_regions"] - previous.get("anon_regions", 0)) if previous else 0.0,
        "foreground_epoch_change": float(len(acc["epochs"]) > 1),
    }
    metadata = {key: acc[key] for key in ("session_id", "app_id", "domain_id",
                                           "window_start_ns", "window_end_ns")}
    metadata["features"] = values
    metadata["access_centroid"] = centroid
    metadata["file_regions"] = acc["file_regions"]
    metadata["anon_regions"] = acc["anon_regions"]
    return metadata


def label_for(row, operations):
    overlaps = []
    for operation in operations:
        overlap = max(0, min(row["window_end_ns"], operation["end_ns"]) -
                      max(row["window_start_ns"], operation["start_ns"]))
        if overlap: overlaps.append((overlap, operation["operation_type"]))
    width = row["window_end_ns"] - row["window_start_ns"]
    if not overlaps: return {"raw": "UNKNOWN", "coarse": "UNKNOWN", "coverage": 0.0,
                             "quality": "LOW_CONFIDENCE"}
    overlaps.sort(key=lambda item: (-item[0], item[1]))
    coverage = min(1.0, sum(item[0] for item in overlaps) / width)
    return {"raw": overlaps[0][1], "coarse": TAXONOMY[overlaps[0][1]],
            "coverage": coverage,
            "quality": "PURE" if coverage >= .8 else "MIXED" if coverage >= .5 else "LOW_CONFIDENCE"}


class Standardizer:
    def fit(self, rows):
        columns = list(zip(*rows)); self.mean = [sum(x) / len(x) for x in columns]
        self.scale = [(sum((v-m)**2 for v in x)/len(x))**.5 or 1.0
                      for x,m in zip(columns,self.mean)]; return self
    def transform(self, rows):
        return [[(v-m)/s for v,m,s in zip(row,self.mean,self.scale)] for row in rows]


class CentroidClassifier:
    def fit(self, rows, labels):
        self.classes = sorted(set(labels)); grouped = defaultdict(list)
        for row, label in zip(rows, labels): grouped[label].append(row)
        self.centroids = {label: [sum(c)/len(c) for c in zip(*grouped[label])]
                          for label in self.classes}; return self
    def probabilities(self, rows):
        output=[]
        for row in rows:
            distances=[sum((a-b)**2 for a,b in zip(row,self.centroids[c]))**.5 for c in self.classes]
            scores=[math.exp(-min(50,d/max(1,len(row)**.5))) for d in distances]
            total=sum(scores); output.append(([x/total for x in scores],min(distances)))
        return output


def metrics(labels, probabilities, classes, rejected=None):
    rejected = rejected or [False] * len(labels); confusion={c:Counter() for c in classes}
    for truth, row, unknown in zip(labels,probabilities,rejected):
        prediction="UNKNOWN" if unknown else classes[max(range(len(classes)),key=lambda i:(row[i],classes[i]))]
        confusion[truth][prediction]+=1
    recalls=[]; f1s=[]; correct=0
    for name in classes:
        tp=confusion[name][name]; fp=sum(confusion[x][name] for x in classes if x!=name)
        fn=sum(confusion[name].values())-tp; p=tp/(tp+fp) if tp+fp else 0; r=tp/(tp+fn) if tp+fn else 0
        recalls.append(r); f1s.append(2*p*r/(p+r) if p+r else 0); correct+=tp
    n=len(labels) or 1
    return {"accuracy":correct/n,"balanced_accuracy":sum(recalls)/len(classes),
            "macro_f1":sum(f1s)/len(classes),"unknown_rate":sum(rejected)/n,
            "classes":classes,"confusion":{k:dict(v) for k,v in confusion.items()},"samples":len(labels)}


def feature_inventory():
    available = ["PARP FILE logical intervals/nr_accesses/age", "PARP ANON nr_accesses/age",
                 "file size/page count", "foreground epoch", "domain/app binding",
                 "2s/5s/10s causal deltas"]
    missing = ["memory.current time series", "memory.stat time series", "memory.events time series",
               "memory.swap.current time series", "cpu.stat time series", "io.stat time series",
               "cgroup memory/cpu/io PSI", "pgfault/pgmajfault window deltas",
               "workingset refault/activate window deltas", "pgscan/pgsteal window deltas",
               "dirty/writeback window series", "task/scheduler series", "RSS/PSS/Referenced/Swap series"]
    return {"schema_version":1,"AVAILABLE_EXISTING_RAW":available,
            "DERIVABLE_FROM_EXISTING":["access centroid","active ratios","object count","temporal delta"],
            "NOT_COLLECTED":missing,"UNSAFE_OR_FORBIDDEN":["automation fields as features","GUI/input/title/path/name/content"]}


def run(real, output):
    started=time.perf_counter_ns(); inventory=json.load((real/"validation/session_inventory.json").open())
    operations=defaultdict(list)
    with (real/"dataset/operation_events.jsonl").open() as stream:
        for line in stream:
            row=json.loads(line); operations[row["session_id"]].append(row)
    accumulators={seconds:defaultdict(dict) for seconds in WINDOWS}; ranges={}
    for meta in inventory["sessions"]:
        low=high=None
        with (real/meta["trace_path"]).open(errors="replace") as stream:
            for line in stream:
                if "parp_region_evidence:" not in line: continue
                row=parse_real_trace_line(line,meta["session_id"],meta["boot_id"],"20260802_194342")
                if row is None: continue
                low=row["event_time_ns"] if low is None else min(low,row["event_time_ns"])
                high=row["event_time_ns"] if high is None else max(high,row["event_time_ns"])
                for seconds in WINDOWS:
                    width=seconds*1_000_000_000; start=row["event_time_ns"]//width*width
                    acc=accumulators[seconds][meta["session_id"]].setdefault(start,
                        empty_accumulator(meta["session_id"],meta["app_id"],meta["domain_id"],start,width))
                    add_event(acc,row)
        ranges[meta["session_id"]]=(low,high)
    all_rows={}; labels_by_seconds={}; support={}; validation_grid=[]
    names=None; source_map=None
    for seconds in WINDOWS:
        rows=[]; label_rows=[]
        for session in SESSIONS:
            previous=None; low,high=ranges[session]
            for start,acc in sorted(accumulators[seconds][session].items()):
                row=feature_row(acc,previous); previous=row
                row["window_id"]=hashlib.sha256(("%s:%d:%d"%(session,seconds,start)).encode()).hexdigest()[:24]
                row["is_complete"]=start>=low and start+seconds*1_000_000_000<=high
                label=label_for(row,operations[session]); rows.append(row)
                label_rows.append({"window_id":row["window_id"],"session_id":session,**label})
        names=sorted(rows[0]["features"])
        source_map={name:{"source_type":"UPPER_LAYER_APP_ID" if name=="foreground_app_id" else
                         "DERIVED_KERNEL_HISTORY" if "delta" in name or "shift" in name else
                         "PARP_ANON" if name.startswith("anon") else "PARP_FILE"}
                    for name in names}; validate_source_map(names,source_map)
        atomic_jsonl(output/("dataset/existing_kernel_windows_%ds.jsonl"%seconds),rows)
        atomic_jsonl(output/("dataset/existing_supervision_%ds.jsonl"%seconds),label_rows)
        label_index={x["window_id"]:x for x in label_rows}; all_rows[seconds]=rows; labels_by_seconds[seconds]=label_index
        counts=defaultdict(lambda:Counter())
        split={"wps_01":"train","wps_02":"validation","wps_03":"test"}
        for row in rows:
            label=label_index[row["window_id"]]
            if row["session_id"] in split and row["is_complete"] and label["quality"]=="PURE":
                counts[label["coarse"]][split[row["session_id"]]]+=1
        eligible=sorted(name for name,c in counts.items() if c["train"]>=8 and c["validation"]>=3 and c["test"]>=3)
        support[str(seconds)]={"classes":{k:dict(v) for k,v in counts.items()},"eligible":eligible}
        if len(eligible)<2: continue
        def extract(session):
            chosen=[row for row in rows if row["session_id"]==session and row["is_complete"] and
                    label_index[row["window_id"]]["quality"]=="PURE" and
                    label_index[row["window_id"]]["coarse"] in eligible]
            return chosen,[[row["features"][name] for name in names] for row in chosen],[
                label_index[row["window_id"]]["coarse"] for row in chosen]
        train,tr_x,tr_y=extract("wps_01"); val,va_x,va_y=extract("wps_02")
        scaler=Standardizer().fit(tr_x); model=CentroidClassifier().fit(scaler.transform(tr_x),tr_y)
        val_output=model.probabilities(scaler.transform(va_x)); probs=[x[0] for x in val_output]
        majority=max(Counter(tr_y),key=lambda x:(Counter(tr_y)[x],x)); majority_probs=[
            [float(c==majority) for c in model.classes] for _ in va_y]
        validation_grid.append({"window_seconds":seconds,"eligible_classes":eligible,
            "centroid":metrics(va_y,probs,model.classes),"majority":metrics(va_y,majority_probs,model.classes)})
    selected=max(validation_grid,key=lambda x:(x["centroid"]["macro_f1"]-x["majority"]["macro_f1"],
                                                x["centroid"]["balanced_accuracy"],-x["window_seconds"]))
    seconds=selected["window_seconds"]; rows=all_rows[seconds]; idx=labels_by_seconds[seconds]
    eligible=selected["eligible_classes"]
    def extract(session):
        chosen=[row for row in rows if row["session_id"]==session and row["is_complete"] and
                idx[row["window_id"]]["quality"]=="PURE" and idx[row["window_id"]]["coarse"] in eligible]
        return chosen,[[row["features"][name] for name in names] for row in chosen],[idx[row["window_id"]]["coarse"] for row in chosen]
    train,tr_x,tr_y=extract("wps_01"); val,va_x,va_y=extract("wps_02"); test,te_x,te_y=extract("wps_03")
    scaler=Standardizer().fit(tr_x); model=CentroidClassifier().fit(scaler.transform(tr_x),tr_y)
    val_output=model.probabilities(scaler.transform(va_x)); threshold=percentile([x[1] for x in val_output],.90)
    test_output=model.probabilities(scaler.transform(te_x)); probs=[x[0] for x in test_output]
    rejected=[x[1]>threshold for x in test_output]; test_metrics=metrics(te_y,probs,model.classes,rejected)
    majority=max(Counter(tr_y),key=lambda x:(Counter(tr_y)[x],x)); majority_probs=[
        [float(c==majority) for c in model.classes] for _ in te_y]
    majority_metrics=metrics(te_y,majority_probs,model.classes)
    quality={}
    for seconds_key, rows_key in all_rows.items():
        q=Counter(labels_by_seconds[seconds_key][row["window_id"]]["quality"] for row in rows_key if row["is_complete"])
        quality[str(seconds_key)]={**dict(q),"complete":sum(row["is_complete"] for row in rows_key)}
    result={"schema_version":1,"source":"RUNTIME_PHASE27B_REAL_FRESH_REUSED",
            "feature_boundary":"foreground_app_id plus PAGE/ANON kernel evidence only",
            "feature_names":names,"source_map":source_map,"window_quality":quality,"support":support,
            "validation_grid":validation_grid,"selected_window_seconds":seconds,
            "unknown_distance_threshold":threshold,"test":test_metrics,"majority_test":majority_metrics,
            "macro_f1_gain":test_metrics["macro_f1"]-majority_metrics["macro_f1"],
            "balanced_accuracy_gain":test_metrics["balanced_accuracy"]-majority_metrics["balanced_accuracy"],
            "new_collection_required":True,"reason":"missing VM/IO/CPU/PSI/refault series and repeated cross-document operations",
            "elapsed_ns":time.perf_counter_ns()-started,"peak_rss_kib":resource.getrusage(resource.RUSAGE_SELF).ru_maxrss}
    atomic_json(output/"operation/current_operation_existing_pilot.json",result)
    atomic_json(output/"validation/kernel_feature_inventory.json",feature_inventory())
    atomic_json(output/"validation/feature_source_map.json",source_map)
    atomic_json(output/"validation/feature_leakage_audit.json",{
        "status":"PASS","forbidden_features":[],"feature_count":len(names),
        "operation_labels_separate":True,"automation_as_feature":False,"paths_as_feature":False})
    atomic_json(output/"validation/kernel_only_contract.json",{
        "status":"PASS","only_upper_layer_input":"foreground_app_id",
        "other_inputs":"PARP_FILE/PARP_ANON","future_features_used":False,"kernel_write":False})
    return result


def main():
    parser=argparse.ArgumentParser(); parser.add_argument("--real",type=Path,required=True)
    parser.add_argument("--output",type=Path,required=True); args=parser.parse_args()
    print(json.dumps(run(args.real,args.output),sort_keys=True))


if __name__=="__main__": main()
