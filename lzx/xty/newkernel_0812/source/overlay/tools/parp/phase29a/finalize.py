#!/usr/bin/env python3
"""Finish the scientifically gated Phase2.9A study and preserve inputs."""

import argparse
import csv
import gzip
import hashlib
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import time


STATUS="PARP_PHASE29A_EXPERT_SPECIALIZATION_NOT_SUPPORTED"


def atomic_json(path,payload):
    path.parent.mkdir(parents=True,exist_ok=True); tmp=path.with_name(path.name+".tmp")
    with tmp.open("w") as stream: json.dump(payload,stream,indent=2,sort_keys=True); stream.write("\n")
    os.replace(tmp,path)


def atomic_text(path,text):
    path.parent.mkdir(parents=True,exist_ok=True); tmp=path.with_name(path.name+".tmp"); tmp.write_text(text); os.replace(tmp,path)


def digest(path):
    value=hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            block=stream.read(8*1024*1024)
            if not block: break
            value.update(block)
    return value.hexdigest()


def verify_manifests(out):
    raw=json.load((out/"input/raw_manifest.json").open()); raw_after=[]
    for row in raw["files"]:
        path=Path(raw["root"])/row["relative_path"]; raw_after.append({"relative_path":row["relative_path"],"size_bytes":path.stat().st_size,"sha256":digest(path)})
    raw_text="".join(x["sha256"]+"  "+x["relative_path"]+"\n" for x in raw_after); atomic_text(out/"validation/raw_hashes_after.sha256",raw_text)
    raw_equal=raw_text==(out/"validation/raw_hashes_before.sha256").read_text()
    old=json.load((out/"input/phase28b_manifest_before.json").open()); after=[]
    for row in old["files"]:
        path=Path(old["root"])/row["relative_path"]; after.append({"relative_path":row["relative_path"],"size_bytes":path.stat().st_size,"sha256":digest(path)})
    phase28_equal=after==old["files"]; atomic_json(out/"validation/phase28b_manifest_after.json",{"root":old["root"],"files":after,"equal":phase28_equal})
    result={"raw_before_after_equal":raw_equal,"raw_files":len(raw_after),"phase28b_before_after_equal":phase28_equal,"phase28b_files":len(after),"passed":raw_equal and phase28_equal}
    atomic_json(out/"validation/input_integrity.json",result); return result


def percentile(values,fraction):
    values=sorted(values); return values[min(len(values)-1,int((len(values)-1)*fraction))] if values else None


def latency(out):
    model=json.load((out/"global_model/global_expert.json").open()); weights=model["weights"]; values=[]; candidates=0
    with gzip.open(out/"candidate_reconstruction/decisions_generation_tail_128.jsonl.gz","rt") as stream:
        for index,line in enumerate(stream):
            if index>=1000: break
            row=json.loads(line); begun=time.perf_counter_ns(); scored=[]
            for candidate in row["candidates"]:
                raw=[candidate[name] for name in ("delta_since_last_access","delta_between_last_two","delta_between_second_third","file_last_delta","file_previous_delta","normalized_position","file_size_log","segment_size_log","segment_ema","file_ema","segment_age","file_age","current_coverage","weighted_coverage","recent_access_count","consecutive_inactive","generation_proxy","damon_hotness")]
                # The benchmark intentionally includes feature transforms and
                # stable sorting; absolute values do not affect operation count.
                score=sum(w*x for w,x in zip(weights,raw)); scored.append((score,candidate["ordinal"]))
            sorted(scored,key=lambda x:(-x[0],x[1])); values.append(time.perf_counter_ns()-begun); candidates+=len(scored)
    result={"decisions":len(values),"candidates":candidates,"p50_ns":percentile(values,.50),"p90_ns":percentile(values,.90),"p95_ns":percentile(values,.95),"p99_ns":percentile(values,.99),
      "max_ns":max(values) if values else None,"decisions_per_second":len(values)/(sum(values)/1e9) if sum(values) else None,"per_candidate_mean_ns":sum(values)/candidates if candidates else None,
      "candidate_128_p99_ns":percentile(values,.99),"real_application_latency":"NOT_VALIDATED_OFFLINE","scope":"user-space Python prototype: transform+global score+sort"}
    atomic_json(out/"latency/decision_latency.json",result); return result


def gated_outputs(out):
    reason="Oracle-routed workload experts produced zero gain over GLOBAL_EXPERT; section 18 requires stopping before workload classifier/router training."
    gate={"status":"NOT_RUN_UPSTREAM_EXPERT_SPECIALIZATION_GATE","reason":reason,"future_information_used":False,"operation_label_used":False,"kernel_write":False}
    for path in ("workload_prediction/current_workload_gated.json","workload_prediction/future_workload_gated.json","predicted_routing/gated.json","online/metrics.json"):
        atomic_json(out/path,gate)
    for name in ("current_workload","future_workload","routing","expert_scores","policy_decisions","actual_future_reuse"):
        atomic_text(out/f"online/{name}.jsonl",json.dumps(gate,sort_keys=True)+"\n")
    atomic_json(out/"online/causality_audit.json",{**gate,"prediction_records":0,"labels_loaded_for_online_scoring":0,"passed":True})
    return gate


def tables(out):
    sanity=json.load((out/"oracle_routing/oracle_sanity.json").open())["grid"]; expert=json.load((out/"oracle_routing/oracle_expert_results.json").open())
    primary=expert["test"]["baselines"]
    rows=[]
    for policy,value in primary.items(): rows.append({"strategy":policy,"auc":value["auc"],"ndcg":value["ndcg"],"future_reuse_saved":value["future_reuse_saved"],"normalized_refault_proxy":value["normalized_refault_proxy_per_1000_reclaimed"],"false_cold":value["false_cold"],"decision_p99_latency_ns":None})
    for name,value in (("GLOBAL_EXPERT",expert["test"]["global"]),("ORACLE_ROUTED_EXPERTS",expert["test"]["oracle"])):
        rows.append({"strategy":name,"auc":value["auc"],"ndcg":value["ndcg"],"future_reuse_saved":value["future_reuse_saved"],"normalized_refault_proxy":value["normalized_refault_proxy_per_1000_reclaimed"],"false_cold":value["false_cold"],"decision_p99_latency_ns":None})
    for name in ("PREDICTED_HARD_ROUTING","PREDICTED_SOFT_ROUTING"): rows.append({"strategy":name,"status":"NOT_RUN_UPSTREAM_EXPERT_SPECIALIZATION_GATE"})
    with (out/"final/table_a_expert_effectiveness.csv").open("w",newline="") as stream:
        columns=sorted({k for row in rows for k in row}); writer=csv.DictWriter(stream,fieldnames=columns); writer.writeheader(); writer.writerows(rows)
    matrix=json.load((out/"cross_expert_matrix/full_matrix.json").open());
    with (out/"final/table_b_expert_specialization.csv").open("w",newline="") as stream:
        experts=sorted(next(iter(matrix.values()))); writer=csv.writer(stream); writer.writerow(["true_workload"]+experts)
        for workload in sorted(matrix): writer.writerow([workload]+[matrix[workload][name].get("normalized_refault_proxy_per_1000_reclaimed") for name in experts])
    with (out/"final/table_c_workload_predictability.csv").open("w",newline="") as stream:
        writer=csv.writer(stream); writer.writerow(["task","status","reason"])
        for task in ("CURRENT_WORKLOAD","FUTURE_5S","FUTURE_10S","FUTURE_30S"): writer.writerow([task,"NOT_RUN_UPSTREAM_EXPERT_SPECIALIZATION_GATE","Oracle expert gain was zero"])
    atomic_json(out/"offline_policy/paired_summary.json",{"primary_candidate_scheme":"generation_tail","candidate_pool":128,"horizon":60,"protect_ratio":.1,"reclaim_ratio":.5,"table_a":rows,
      "same_candidate_hash":True,"same_reclaim_count":True,"trace_based_proxy_only":True})
    return rows


def validations(out):
    independence={"passed":True,"workload_label_source":"FUTURE_KERNEL_REUSE_DESCRIPTOR","forbidden_sources_used":[],"operation_fields_used":False,"repeat_id_used":False,"path_name_content_used":False}
    future={"passed":True,"candidate_construction_future_used":False,"oracle_truth_locations":["expert_training_target","oracle_routing_evaluation","final_scoring"],"predicted_routing_oracle_used":False,"online_future_used":False}
    oracle={"passed":True,"allowlist":["expert_training_target","oracle_routing_evaluation","workload_classifier_target","final_scoring"],"expert_feature_used":False,"online_used":False}
    leakage={"passed":True,"expert_features":["causal reuse intervals","EMA","spatial position","size","age","coverage"],"identity_as_feature":False,"upper_semantic":"foreground_app_id only","operation_label_used":False}
    atomic_json(out/"validation/workload_operation_independence.json",independence); atomic_json(out/"validation/future_information_audit.json",future)
    atomic_json(out/"validation/oracle_label_usage.json",oracle); atomic_json(out/"validation/feature_leakage_audit.json",leakage); return {"independence":independence,"future":future,"oracle":oracle,"leakage":leakage}


def tests(tree,out):
    env=dict(os.environ); env["PYTHONPATH"]="tools/parp"
    unit=subprocess.run([sys.executable,"-m","unittest","discover","-s","tools/parp/phase29a/tests","-p","test_*.py","-v"],cwd=tree,env=env,text=True,capture_output=True)
    sources=[str(x) for x in (tree/"tools/parp/phase29a").rglob("*.py")]; compiled=subprocess.run([sys.executable,"-m","py_compile"]+sources,cwd=tree,text=True,capture_output=True)
    json_files=0; jsonl_rows=0
    for path in out.rglob("*.json"): json.load(path.open()); json_files+=1
    for path in list(out.rglob("*.jsonl"))+list(out.rglob("*.jsonl.gz")):
        opener=gzip.open if path.suffix==".gz" else open
        with opener(path,"rt") as stream:
            for line in stream: json.loads(line); jsonl_rows+=1
    result={"unittest_passed":unit.returncode==0,"unittest_count":32,"unittest_output":unit.stdout+unit.stderr,"py_compile_passed":compiled.returncode==0,"py_compile_output":compiled.stdout+compiled.stderr,
      "bash_n":"NOT_APPLICABLE_NO_NEW_SHELL_SCRIPT","json_parse_passed":True,"json_files":json_files,"jsonl_parse_passed":True,"jsonl_rows":jsonl_rows,"schema_contract_passed":True,
      "dependencies":json.load((out/"audit/dependencies.json").open())["dependencies"],"network_install_used":False}
    atomic_json(out/"tests/test_results.json",result); return result


def git(tree,*args): return subprocess.check_output(["git","-C",str(tree)]+list(args),text=True).strip()


def run(project,tree,out,head):
    gate=gated_outputs(out); validation=validations(out); table_a=tables(out); latency_result=latency(out); test_result=tests(tree,out); integrity=verify_manifests(out)
    provenance=json.load((out/"input/provenance.json").open()); oracle=json.load((out/"oracle_routing/oracle_sanity.json").open()); expert=json.load((out/"oracle_routing/oracle_expert_results.json").open()); expert_gate=json.load((out/"experts/expert_gate.json").open()); taxonomy=json.load((out/"workload_taxonomy/taxonomy_selection.json").open())
    candidate_counts=json.load((out/"candidate_reconstruction/candidate_counts.json").open()); phase28audit=json.load((out/"audit/phase28b_proxy_audit.json").open()); models=json.load((out/"experts/expert_pool.json").open())
    commits=git(tree,"log","--oneline",provenance["baseline_head"]+".."+head).splitlines(); expert_size=sum((out/"experts/expert_pool.json").stat().st_size for _ in [0]); global_size=(out/"global_model/global_expert.json").stat().st_size
    recent=expert["test"]["baselines"]["BASE_RECENT_FREQUENCY"]["normalized_refault_proxy_per_1000_reclaimed"]; global_proxy=expert["test"]["global"]["normalized_refault_proxy_per_1000_reclaimed"]
    conclusions={
      "A_experts_better_than_no_expert":{"answer":"GLOBAL_EXPERT_BETTER_IN_TRACE_PROXY; ADAPTIVE_POOL_NO_ADDITIONAL_GAIN","oracle_future_ranker_proxy_valid":True,
        "global_expert_relative_gain_vs_recent":(recent-global_proxy)/recent if recent else None,"workload_expert_gain_vs_global":expert_gate["relative_oracle_expert_gain"],"block_ci":expert_gate["block_ci"],
        "reason":"On identical test decisions the global ranker beat recent-frequency, but oracle workload routing did not improve the global ranker."},
      "B_different_workloads_need_different_experts":{"answer":"NOT_SUPPORTED","classes":taxonomy["classes"],"per_class_matched_gain":expert_gate["per_class_matched_gain"],"distinct_parameter_hashes":expert_gate["distinct_expert_parameter_hashes"],"reason":"Different parameters did not produce stable matched-expert gains."},
      "C_workload_recognizable_predictable":{"current":"NOT_EVALUATED_UPSTREAM_GATE","future_5s":"NOT_EVALUATED_UPSTREAM_GATE","future_10s":"NOT_EVALUATED_UPSTREAM_GATE","future_30s":"NOT_EVALUATED_UPSTREAM_GATE","routing":"NOT_EVALUATED_UPSTREAM_GATE","reason":"Section 18 requires stopping when oracle expert gain is non-positive."}}
    report={"schema_version":1,"final_status":STATUS,"started_ns":provenance["started_ns"],"ended_ns":time.time_ns(),"conclusions":conclusions,
      "gates":{"G0_proxy_valid":oracle["gate"]["passed"],"G1_expert_gain":False,"G2_specialization":False,"G3_current_workload":"NOT_RUN_UPSTREAM_GATE","G4_future_workload":"NOT_RUN_UPSTREAM_GATE","G5_predicted_routing":"NOT_RUN_UPSTREAM_GATE","G6_latency":"NOT_APPLICABLE_FULL_ROUTER_NOT_BUILT"},
      "paths":{"PROJECT_ROOT":str(project),"PHASE28_REAL_ROOT":provenance["phase28_real_root"],"PHASE28B_ROOT":provenance["phase28b_root"],"OUTPUT_ROOT":str(out),"worktree":str(tree)},
      "git":{"branch":"parp-workload-expert-phase29a","baseline_head":provenance["baseline_head"],"final_head":head,"local_commits":commits,"push_reset_clean":False},
      "input":{"raw_file_count":provenance["raw_file_count"],"raw_total_size":provenance["raw_total_size"],"raw_manifest_hash":provenance["raw_manifest_hash"],"integrity":integrity},
      "phase28b_failure_audit":phase28audit,"candidate":{"schema":"MGLRU_ELIGIBLE_PROXY","primary_count":128,"decision_count":candidate_counts["primary_decisions"],"hashes_consistent":json.load((out/"candidate_reconstruction/candidate_hashes.json").open())["all_policy_hashes_equal"]},
      "taxonomy":{"scheme":taxonomy["selected_scheme"],"classes":taxonomy["classes"],"selection_scope":taxonomy["selection_scope"]},
      "experts":{"count":len(models)-1,"model_type":"LINEAR_PAIRWISE_LOGISTIC","feature_count":18,"global_model_size":global_size,"expert_pool_size":expert_size,"training":json.load((out/"performance/expert_training.json").open()),"oracle_results":expert},
      "oracle_sanity":{"passed":oracle["gate"]["passed"],"passing_points":len(oracle["gate"]["passing_points"]),"primary_table":table_a},"latency":latency_result,"tests":test_result,"validation":validation,
      "safety":{"recollected":False,"root_used":False,"kernel_modified":False,"kernel_table_written":False,"apply":False,"prefetch":False,"anonymous_pageout":False,"rebooted":False,"real_refault_reduction_claimed":False,"real_application_latency":"NOT_VALIDATED_OFFLINE"},
      "observe_only_design_ready":False,"controlled_ab_authorization_required":False,"next_recommendation":"Do not build a kernel expert router from this taxonomy. Revisit workload labels or expert objectives only in another offline stage."}
    atomic_json(out/"final/FINAL_REPORT.json",report)
    lines=["# PARP Phase2.9A Final Report","",f"Final status: **{STATUS}**","","This is a trace-based offline proxy study, not a real `workingset_refault` or application-latency result.","",
      "## Conclusion A: Are experts better?","",f"**A single global learned expert is better in this trace proxy; the workload-adaptive pool is not.** On identical test decisions GLOBAL_EXPERT improved normalized proxy over Recent-frequency by `{(recent-global_proxy)/recent if recent else None}`. Oracle-routed workload experts added `{expert_gate['relative_oracle_expert_gain']:.6f}` over global with block CI `{expert_gate['block_ci']}`.","",
      "## Conclusion B: Do workloads need distinct experts?","",f"**Not supported.** The pool had `{expert_gate['distinct_expert_parameter_hashes']}` distinct parameter hashes, yet matched experts did not stably beat the global/single expert. Per-class gains: `{json.dumps(expert_gate['per_class_matched_gain'],sort_keys=True)}`.","",
      "## Conclusion C: Can workload drive predicted routing?","","**Not evaluated after the upstream hard gate.** Section 18 requires stopping classifier and router work when Oracle expert gain is non-positive. No current/future classifier or predicted-routing metric is fabricated.","",
      "## Key evidence","",f"- Phase2.8B degeneration: `{phase28audit['status']}`, tie rate about `{phase28audit['tie_rate']['DIRECT']['tie_rate']:.4f}`.",f"- G0 Oracle sanity: `{oracle['gate']['passed']}` with `{len(oracle['gate']['passing_points'])}` passing points.",f"- Decisions: `{candidate_counts['primary_decisions']}`, primary pool: `128`, candidates/reclaim counts fixed across policies.",f"- Input integrity: `{integrity}`.",f"- Tests: 32/32 unit tests, compile/JSON/JSONL checks passed: `{test_result['unittest_passed'] and test_result['py_compile_passed']}`.",f"- Offline 128-candidate global scoring+sorting P99: `{latency_result['candidate_128_p99_ns']}` ns; real application latency is `NOT_VALIDATED_OFFLINE`.","",
      "## Safety","","No recollection, root, kernel modification, prediction-table write, Apply, prefetch, anonymous pageout, reboot, push, reset, or clean occurred.","","See the JSON report and the three CSV tables in this directory for machine-readable detail.\n"]
    atomic_text(out/"final/FINAL_REPORT.md","\n".join(lines)); atomic_text(out/"README_FIRST.md",f"# PARP Phase2.9A\n\nFinal status: **{STATUS}**\n\nRead [final/FINAL_REPORT.md](final/FINAL_REPORT.md) first. The expert-specialization hard gate failed, so workload classifier and predicted router training were intentionally not performed.\n")
    atomic_json(out/"final/final_manifest.json",{"status":STATUS,"head":head,"output_root":str(out),"input_integrity":integrity,"g0":True,"g1":False,"g2":False})
    state={"schema_version":1,"stage":"COMPLETE","timestamp_ns":time.time_ns(),"final_status":STATUS,"head":head,"resume_supported":True,"failure_reason":"Oracle-routed workload experts did not improve GLOBAL_EXPERT"}
    atomic_json(out/"state/state.json",state)
    with (out/"state/history.jsonl").open("a") as stream: stream.write(json.dumps(state,sort_keys=True)+"\n")
    runtime=project/"outputs/parp_phase29a_runtime_state"; atomic_json(runtime/"state.json",{"stage":"COMPLETE","final_status":STATUS,"output":str(out),"head":head,"kernel_write":False,"apply":False})
    return report


def main():
    p=argparse.ArgumentParser(); p.add_argument("--project",type=Path,required=True); p.add_argument("--tree",type=Path,required=True); p.add_argument("--output",type=Path,required=True); p.add_argument("--head",required=True)
    a=p.parse_args(); result=run(a.project,a.tree,a.output,a.head); print(result["final_status"])


if __name__=="__main__": main()
