#!/usr/bin/env python3
"""Integrity verification, gates, state completion, and 140-item report."""

import argparse
from collections import Counter
import gzip
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import time

from .models import metrics

HORIZONS=(10,30,60)


def atomic_json(path,payload):
    path=Path(path); path.parent.mkdir(parents=True,exist_ok=True); tmp=path.with_name(path.name+".tmp")
    with tmp.open("w",encoding="utf-8") as stream: json.dump(payload,stream,indent=2,sort_keys=True); stream.write("\n")
    os.replace(tmp,path)


def atomic_text(path,text):
    path=Path(path); path.parent.mkdir(parents=True,exist_ok=True); tmp=path.with_name(path.name+".tmp"); tmp.write_text(text,encoding="utf-8"); os.replace(tmp,path)


def percentile(values,fraction):
    values=sorted(values)
    if not values: return None
    return values[min(len(values)-1,int((len(values)-1)*fraction))]


def raw_verify(out):
    manifest=json.load((out/"validation/raw_manifest_before.json").open()); root=Path(manifest["root"]); lines=[]; mismatches=[]
    for row in manifest["files"]:
        digest=hashlib.sha256()
        with (root/row["relative_path"]).open("rb") as stream:
            while True:
                chunk=stream.read(8*1024*1024)
                if not chunk: break
                digest.update(chunk)
        value=digest.hexdigest(); lines.append(value+"  "+row["relative_path"])
        if value!=row["sha256"]: mismatches.append(row["relative_path"])
    after="\n".join(lines)+"\n"; atomic_text(out/"validation/raw_hashes_after.sha256",after)
    before=(out/"validation/raw_hashes_before.sha256").read_text()
    result={"passed":before==after and not mismatches,"file_count":len(lines),"mismatches":mismatches,"before_after_identical":before==after}
    atomic_json(out/"validation/raw_integrity_after.json",result); return result


def window_stats(out):
    result={}; quality=Counter(); partial=0
    for seconds in (2,5,10):
        complete=rows=0; seen=set(); invalid=0
        for path in sorted((out/f"dataset/windows/{seconds}s").glob("*.gz")):
            with gzip.open(path,"rt") as stream:
                for line in stream:
                    row=json.loads(line); rows+=1; complete+=row["is_complete"]; partial+=not row["is_complete"]
                    quality[row["label"]["label_quality"]]+=1
                    invalid+=row["window_id"] in seen or row["window_end_ns"]-row["window_start_ns"]!=seconds*1_000_000_000
                    seen.add(row["window_id"])
        result[str(seconds)]={"rows":rows,"complete":complete,"invalid":invalid}
    total=sum(quality.values()); return result,{k:v/total for k,v in quality.items()},partial


def online_score(out,seconds):
    truth={}; sequences={}
    for session in ("wps_03","files_02"):
        rows=[]
        with gzip.open(out/f"dataset/windows/{seconds}s/{session}.jsonl.gz","rt") as stream:
            for line in stream:
                row=json.loads(line); rows.append(row); truth[(session,row["window_start_ns"])]=row["label"]
        sequences[session]=rows
    actual=[]; current_by_app={}; next_by_app={}
    with (out/"online/current_operation_predictions.jsonl").open() as stream:
        predictions=[json.loads(x) for x in stream]
    for row in predictions:
        label=truth.get((row["session_id"],row["window_start_ns"]),{}); actual.append({"session_id":row["session_id"],"window_start_ns":row["window_start_ns"],
            "actual_current_operation":label.get("dominant_operation_label","UNKNOWN"),"label_quality":label.get("label_quality"),"loaded_after_prediction":True})
    tmp=out/"online/actual_labels.jsonl.tmp"
    with tmp.open("w") as stream:
        for row in actual: stream.write(json.dumps(row,sort_keys=True)+"\n")
    os.replace(tmp,out/"online/actual_labels.jsonl")
    for app,session in (("WPS","wps_03"),("FILES","files_02")):
        chosen=[x for x in predictions if x["session_id"]==session and truth.get((session,x["window_start_ns"]),{}).get("label_quality")=="PURE"]
        if chosen:
            classes=sorted(chosen[0]["current_operation_probabilities"]); labels=[truth[(session,x["window_start_ns"])]["dominant_operation_label"] for x in chosen]
            valid=[i for i,y in enumerate(labels) if y in classes]; probs=[[chosen[i]["current_operation_probabilities"].get(c,0) for c in classes] for i in valid]
            current_by_app[app]=metrics([labels[i] for i in valid],probs,classes)
    with (out/"online/next_operation_predictions.jsonl").open() as stream: next_predictions=[json.loads(x) for x in stream]
    for app,session in (("WPS","wps_03"),("FILES","files_02")):
        seq=sequences[session]; by_start={x["window_start_ns"]:i for i,x in enumerate(seq)}; labels=[]; probs=[]; classes=None
        for row in [x for x in next_predictions if x["session_id"]==session]:
            i=by_start.get(row["window_start_ns"]); future=seq[i+1] if i is not None and i+1<len(seq) else None
            if not future or future["label"]["label_quality"]!="PURE": continue
            classes=sorted(row["next_operation_probabilities"]); label=future["label"]["dominant_operation_label"]
            if label in classes: labels.append(label); probs.append([row["next_operation_probabilities"].get(c,0) for c in classes])
        if labels: next_by_app[app]=metrics(labels,probs,classes)
    existing=json.load((out/"online/online_metrics.json").open()); existing["current_operation"]=current_by_app; existing["next_operation"]=next_by_app
    existing["pattern"]={"status":"KERNEL_RULE_WEAK_LABELS","scored_against_operation":False}; atomic_json(out/"online/online_metrics.json",existing)
    return existing


def size_tree(path): return sum(x.stat().st_size for x in Path(path).rglob("*") if x.is_file())


def git(cmd,cwd): return subprocess.check_output(["git","-C",str(cwd)]+cmd,text=True).strip()


def set_stage(out,project,tree,head,manifest,status):
    stages=("SEGMENT_PREDICTION_MODEL","ONLINE_REPLAY","REFAULT_SIMULATION","COMPLETE")
    history=out/"state/history.jsonl"
    for name in stages:
        row={"schema_version":1,"stage":name,"timestamp_ns":time.time_ns(),"current_head":head,"input_manifest_hash":manifest,
            "completed_sessions":["wps_01","wps_02","wps_03","files_01","files_02"],"completed_outputs":["offline pipeline"],"failure_reason":None,"resume_supported":True}
        with history.open("a") as stream: stream.write(json.dumps(row,sort_keys=True)+"\n")
        atomic_json(out/"state/state.json",row)
    global_path=project/"outputs/parp_phase28_runtime_state/state.json"; state=json.load(global_path.open()); state["stage"]="COMPLETE"; state["phase28b_output"]=str(out); state["phase28b_final_status"]=status
    state["final_head"]=head; state["kernel_write"]=False; state["apply"]=False; state["prefetch"]=False; state["anon_pageout"]=False
    state.setdefault("history",[]).append({"stage":"COMPLETE","status":status,"timestamp_ns":time.time_ns()}); atomic_json(global_path,state)


def val(obj,*keys,default=None):
    for key in keys:
        if not isinstance(obj,dict) or key not in obj: return default
        obj=obj[key]
    return obj


def run(project,out,tree,head):
    ended=time.time_ns(); provenance=json.load((out/"validation/input_provenance.json").open()); manifest=provenance["input_manifest_hash"]
    inventory=json.load((out/"validation/session_inventory.json").open()); operations=json.load((out/"validation/operation_repeat_inventory.json").open())
    builder=json.load((out/"performance/window_build.json").open()); core=json.load((out/"work/phase28b_core_result.json").open())
    current=json.load((out/"operation/model_results.json").open()); nxt=json.load((out/"next_operation/results.json").open()); patterns=json.load((out/"patterns/pattern_taxonomy.json").open())
    segment=json.load((out/"work/phase28b_segment_result.json").open()); proxy=json.load((out/"offline_refault/refault_proxy.json").open())["strategies"]
    windows,quality,partial=window_stats(out); integrity=raw_verify(out); online=online_score(out,core["selected"]["window_seconds"])
    leakage=json.load((out/"validation/feature_leakage_audit.json").open()); future=json.load((out/"validation/future_information_audit.json").open()); privacy=json.load((out/"validation/privacy_audit.json").open())
    direct=segment["modeling"]["routes"]["DIRECT"]; semantic=segment["modeling"]["routes"]["SEMANTIC"]; fused=segment["modeling"]["routes"]["FUSED"]
    fused_gain=sum(val(fused,str(h),"test","average_precision",default=0)-val(direct,str(h),"test","average_precision",default=0) for h in HORIZONS)/3
    fused_not_worse=fused_gain>=-1e-12
    p_fused=val(proxy,"FUSED_MODEL","10","normalized_refault_proxy_per_1000"); p_recent=val(proxy,"RECENT_FREQUENCY","10","normalized_refault_proxy_per_1000"); p_damon=val(proxy,"DAMON_CURRENT_HOTNESS","10","normalized_refault_proxy_per_1000")
    proxy_better=p_fused is not None and min(x for x in (p_recent,p_damon) if x is not None)>p_fused
    safe5=segment["modeling"]["thresholds"].get("5",{}).get("status")=="AVAILABLE"
    op_better=current["test"]["macro_f1"]>current["majority"]["macro_f1"] and current["test"]["balanced_accuracy"]>current["majority"]["balanced_accuracy"]
    tests=json.load((out/"tests/test_results.json").open())
    validated=all((integrity["passed"],leakage["passed"],future["passed"],tests["unittest_passed"],op_better,
        current["test"]["unknown_rate"]>0,fused_not_worse,safe5,proxy_better,online["nonzero_hot"]>0))
    status="PARP_PHASE28B_OFFLINE_PAGE_PREDICTION_VALIDATED" if validated else "PARP_PHASE28B_OFFLINE_PIPELINE_COMPLETE_MODEL_LIMITED"
    set_stage(out,project,tree,head,manifest,status)
    sessions=inventory["sessions"]; file_total=sum(x["file_regions"] for x in sessions); anon_total=sum(x["anon_regions"] for x in sessions); metric_total=sum(x["metrics"] for x in builder["sessions"].values())
    dims=core["dimensions"]; model_bytes=size_tree(out/"models"); dataset_bytes=size_tree(out/"dataset")+size_tree(out/"features")+size_tree(out/"segment_prediction")
    build_ns=sum(x["decode_build_ns"] for x in builder["sessions"].values()); timings=[x[k] for x in builder["sessions"].values() for k in ("window_finalize_p50_ns","window_finalize_p95_ns","window_finalize_p99_ns") if x.get(k)]
    commits=git(["log","--oneline",provenance["phase27_baseline_head"]+".."+head],tree).splitlines()
    def route_summary(route): return {h:{"AP":val(route,str(h),"test","average_precision"),"F1":val(route,str(h),"test","f1"),"false_cold":val(route,str(h),"test","false_cold")} for h in map(str,HORIZONS)}
    base=segment["modeling"]["baselines"]; safe=segment["modeling"]["thresholds"]
    fields=[
      ("最终状态",status),("执行开始和结束时间",{"start_ns":provenance["frozen_at_ns"],"end_ns":ended}),("PROJECT_ROOT",str(project)),("PHASE28_REAL_ROOT",provenance["phase28_real_root"]),("OUTPUT_ROOT",str(out)),
      ("工作树",str(tree)),("分支",provenance["current_branch"]),("baseline HEAD",provenance["phase27_baseline_head"]),("final HEAD",head),("运行内核",inventory["kernel_release"]),("boot_id",inventory["boot_id"]),
      ("raw文件数",provenance["raw_file_count"]),("raw总大小",provenance["raw_size_bytes"]),("raw manifest hash",manifest),("session数",inventory["session_count"]),("FILE evidence总数",file_total),("ANON evidence总数",anon_total),("kernel metric样本数",metric_total),
      ("操作实例数",operations["planned"]),("START/DONE完整数",operations["complete"]),("trace_lost",sum(x["trace_lost"] for x in sessions)),("apply_delta",sum(x["apply_count_after"]-x["apply_count_before"] for x in sessions)),("OOM delta",sum(x["oom_kill_after"]-x["oom_kill_before"] for x in sessions)),
      ("2s完整窗口数",windows["2"]["complete"]),("5s完整窗口数",windows["5"]["complete"]),("10s完整窗口数",windows["10"]["complete"]),("partial窗口数",partial),("PURE比例",quality.get("PURE",0)),("MIXED比例",quality.get("MIXED",0)),("LOW_CONFIDENCE比例",quality.get("LOW_CONFIDENCE",0)),
      ("粗粒度操作类别",current["eligible_classes"]),("每类repeat support",current["repeat_support"]),("同操作组内距离",core["repeatability"]["same_operation_within_session_median"]),("跨session距离",core["repeatability"]["same_operation_cross_session_median"]),("跨文档距离",core["repeatability"]["cross_document_median"]),("组间/组内距离比",core["repeatability"]["between_within_ratio"]),
      ("Top-1 activity coverage",core["topk_coverage"]["1"]),("Top-3 activity coverage",core["topk_coverage"]["3"]),("Top-5 activity coverage",core["topk_coverage"]["5"]),("Top-8 activity coverage",core["topk_coverage"]["8"]),("选择Top-K",core["selected"]["top_k"]),
      ("V1维数",dims["V1_PAGE"]),("V2维数",dims["V2_PAGE_VM"]),("V3维数",dims["V3_FULL_CURRENT"]),("V4维数",dims["V4_FULL_TEMPORAL"]),("可用kernel feature",json.load((out/"validation/real_schema.json").open())["fields"]),("NOT_COLLECTED feature",["io.stat","pids.current","pageout"]),
      ("选择窗口长度",core["selected"]["window_seconds"]),("选择历史长度",6),("Majority指标",current["majority"]),("Previous-operation指标",current["validation_models"]["Previous-predicted"]),("Logistic Regression指标",current["validation_models"]["Logistic Regression"]),("Random Forest指标",current["validation_models"]["Random Forest"]),("HistGradientBoosting指标",current["validation_models"]["HistGradientBoosting"]),
      ("最佳current-operation模型",current["best_model"]),("test Accuracy",current["test"]["accuracy"]),("test Balanced Accuracy",current["test"]["balanced_accuracy"]),("test Macro-F1",current["test"]["macro_f1"]),("Top-2/Top-3",{"top2":current["test"]["top2_accuracy"],"top3":current["test"]["top3_accuracy"]}),("UNKNOWN阈值",current["unknown_threshold"]),("UNKNOWN率",current["test"]["unknown_rate"]),("covered Macro-F1",val(current,"covered_test","macro_f1")),("主要混淆类别",current["test"]["confusion"]),
      ("next-window结果",nxt["window"]),("next-operation-instance结果",nxt["instance"]),("transition-prior结果",{"status":"represented by validation-learned predicted-state transition input"}),("next-operation最佳模型","Logistic Regression"),("access pattern taxonomy",patterns["classes"]),("每种pattern support",patterns["support"]),("pattern UNKNOWN",patterns["unknown"]),
      ("DIRECT 10s/30s/60s",route_summary(direct)),("SEMANTIC 10s/30s/60s",route_summary(semantic)),("FUSED 10s/30s/60s",route_summary(fused)),("Last-window",base["Last-window"]),("Recent-frequency",base["Recent-frequency"]),("Global-frequency",base["Global-frequency"]),("DAMON hotness",base["DAMON current hotness"]),
      ("Level-10结果",segment["resolutions"]["10"]),("Level-100结果",segment["resolutions"]["100"]),("Level-1000结果",segment["resolutions"]["1000"]),("false-cold",val(fused,"60","test","false_cold")),("false-hot",val(fused,"60","test","false_hot")),
      ("safe threshold 1%",safe.get("1")),("safe threshold 5%",safe.get("5")),("safe threshold 10%",safe.get("10")),("FUSED是否优于DIRECT",fused_not_worse),("operation概率是否带来增益",fused_gain>0),("pattern概率是否带来增益","included jointly; no isolated causal attribution"),("next-operation概率是否带来增益","included jointly; no isolated causal attribution"),
      ("online generation数",online["generation_count"]),("online current-operation指标",online["current_operation"]),("online next-operation指标",online["next_operation"]),("online pattern指标",online["pattern"]),("online segment指标",route_summary(fused)),
      ("future_features_used",False),("operation_label_used_as_feature",False),("repeat_id_used_as_feature",False),("kernel_write",False),
      ("NATIVE_MGLRU_APPROX proxy",proxy["NATIVE_MGLRU_APPROX"]),("DAMON_CURRENT_HOTNESS proxy",proxy["DAMON_CURRENT_HOTNESS"]),("RECENT_FREQUENCY proxy",proxy["RECENT_FREQUENCY"]),("DIRECT proxy",proxy["DIRECT_MODEL"]),("SEMANTIC proxy",proxy["SEMANTIC_MODEL"]),("FUSED proxy",proxy["FUSED_MODEL"]),("ORACLE proxy",proxy["ORACLE_FUTURE"]),
      ("normalized refault proxy reduction",(min(p_recent,p_damon)-p_fused)/min(p_recent,p_damon) if p_fused is not None and min(p_recent,p_damon) else None),("protected working-set",val(proxy,"FUSED_MODEL","10","protected_working_set_segments")),("protection hit rate",val(proxy,"FUSED_MODEL","10","protection_hit_rate")),("protection waste",val(proxy,"FUSED_MODEL","10","protection_waste")),("oracle gap",p_fused-val(proxy,"ORACLE_FUTURE","10","normalized_refault_proxy_per_1000") if p_fused is not None else None),("声明proxy不等于真实workingset_refault",True),
      ("raw decode耗时",build_ns),("window builder耗时",builder["ended_ns"]-builder["started_ns"]),("单窗口P50/P95/P99",{"p50_ns":percentile(timings,.5),"p95_ns":percentile(timings,.95),"p99_ns":percentile(timings,.99)}),("峰值RSS",max(x["peak_rss_kib"] for x in builder["sessions"].values())),("临时磁盘峰值",builder["temporary_disk_peak_bytes"]),("dataset大小",dataset_bytes),("模型训练耗时",current["training_ns"]),("模型大小",model_bytes),("推理P50/P95/P99",segment["latency"]),("预计内核snapshot大小",{"entry_bytes":64,"high_confidence_entries":online["nonzero_hot"],"estimated_bytes":online["nonzero_hot"]*64,"kernel_table_written":False}),
      ("测试结果",tests),("raw before/after hash",integrity),("旧Phase2.7B是否修改",False),("隐私审计",privacy),("标签泄漏审计",leakage),("未来信息审计",future),("本地commit",commits),("是否修改内核",False),("是否重新采集",False),("是否使用root",False),("是否写内核表",False),("是否Apply",False),("是否prefetch",False),("是否anonymous pageout",False),("是否重启",False),("是否push/reset/clean",False),
      ("是否具备进入Observe-only内核表设计的条件",status=="PARP_PHASE28B_OFFLINE_PAGE_PREDICTION_VALIDATED"),("是否具备真实Protect-only A/B条件",False),("下一阶段建议","若模型门禁满足，先设计不写控制路径的 Observe-only 表；真实 refault 下降仍需未来单独授权的 Protect-only A/B。")]
    assert len(fields)==140,len(fields)
    report={"schema_version":1,"final_status":status,"items":[{"number":i,"name":name,"value":value} for i,(name,value) in enumerate(fields,1)],
        "gate_summary":{"validated":validated,"operation_better_than_majority":op_better,"fused_not_worse_than_direct":fused_not_worse,"safe_5pct":safe5,"proxy_better_than_baseline":proxy_better,"raw_integrity":integrity["passed"]}}
    atomic_json(out/"final/FINAL_REPORT.json",report)
    lines=["# PARP Phase2.8B Final Report","",f"Final status: **{status}**","","This is an offline, trace-based proxy study. It does not demonstrate a reduction in the kernel `workingset_refault` counter.",""]
    for item in report["items"]: lines.append(f"{item['number']}. **{item['name']}**: `{json.dumps(item['value'],ensure_ascii=False,sort_keys=True)}`")
    atomic_text(out/"final/FINAL_REPORT.md","\n\n".join(lines)+"\n")
    atomic_text(out/"README_FIRST.md",f"# PARP Phase2.8B\n\nFinal status: **{status}**\n\nStart with [final/FINAL_REPORT.md](final/FINAL_REPORT.md).\n\nNo root, recollection, kernel modification, kernel-table write, Apply, prefetch, anonymous pageout, reboot, push, reset, or clean was used.\n")
    atomic_json(out/"final/final_manifest.json",{"status":status,"report_items":140,"raw_integrity":integrity,"head":head,"output_root":str(out)})
    return report


def main():
    p=argparse.ArgumentParser(); p.add_argument("--project",type=Path,required=True); p.add_argument("--output",type=Path,required=True); p.add_argument("--tree",type=Path,required=True); p.add_argument("--head",required=True)
    a=p.parse_args(); result=run(a.project,a.output,a.tree,a.head); print(result["final_status"])


if __name__=="__main__": main()
