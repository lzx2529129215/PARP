#!/usr/bin/env python3
"""Create Phase2.9A output and freeze both Phase2.8 input roots."""

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import time

DIRECTORIES=("state","config","input","audit","dataset","workload_taxonomy","workload_features",
 "workload_prediction","candidate_reconstruction","experts","global_model","oracle_routing",
 "predicted_routing","cross_expert_matrix","offline_policy","statistics","latency","online",
 "validation","tests","performance","final","analysis","work")


def atomic_json(path,payload):
    path.parent.mkdir(parents=True,exist_ok=True); tmp=path.with_name(path.name+".tmp")
    with tmp.open("w",encoding="utf-8") as stream: json.dump(payload,stream,indent=2,sort_keys=True); stream.write("\n")
    os.replace(tmp,path)


def atomic_text(path,text):
    path.parent.mkdir(parents=True,exist_ok=True); tmp=path.with_name(path.name+".tmp"); tmp.write_text(text,encoding="utf-8"); os.replace(tmp,path)


def digest(path):
    value=hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            block=stream.read(8*1024*1024)
            if not block: break
            value.update(block)
    return value.hexdigest()


def hash_manifest(root,relative_paths):
    rows=[]
    for relative in sorted(relative_paths):
        path=root/relative; rows.append({"relative_path":relative,"size_bytes":path.stat().st_size,"sha256":digest(path)})
    return rows


def git(tree,*args): return subprocess.check_output(["git","-C",str(tree)]+list(args),text=True).strip()


def run(project,tree,phase28real,phase28b,out):
    out.mkdir(parents=True,exist_ok=False)
    for name in DIRECTORIES: (out/name).mkdir()
    original=json.load((phase28b/"validation/raw_manifest_before.json").open()); raw_paths=[x["relative_path"] for x in original["files"]]
    raw_rows=hash_manifest(phase28real,raw_paths); raw_text="".join(x["sha256"]+"  "+x["relative_path"]+"\n" for x in raw_rows)
    atomic_text(out/"validation/raw_hashes_before.sha256",raw_text); atomic_json(out/"input/raw_manifest.json",{"root":str(phase28real),"files":raw_rows,"file_count":len(raw_rows),"size_bytes":sum(x["size_bytes"] for x in raw_rows)})
    phase28b_paths=[str(x.relative_to(phase28b)) for x in phase28b.rglob("*") if x.is_file()]
    phase28b_rows=hash_manifest(phase28b,phase28b_paths); atomic_json(out/"input/phase28b_manifest_before.json",{"root":str(phase28b),"files":phase28b_rows,"file_count":len(phase28b_rows),"size_bytes":sum(x["size_bytes"] for x in phase28b_rows)})
    manifest_hash=hashlib.sha256(raw_text.encode()).hexdigest(); head=git(tree,"rev-parse","HEAD")
    provenance={"schema_version":1,"project_root":str(project),"phase28_real_root":str(phase28real),"phase28b_root":str(phase28b),"output_root":str(out),
      "worktree":str(tree),"branch":git(tree,"branch","--show-current"),"baseline_head":"e4e7f2fd0abadf938cf6ac4a8a1b016f6e64e5ab","start_head":head,
      "raw_file_count":len(raw_rows),"raw_total_size":sum(x["size_bytes"] for x in raw_rows),"raw_manifest_hash":manifest_hash,"phase28b_file_count":len(phase28b_rows),
      "phase28b_manifest_hash":hashlib.sha256(json.dumps(phase28b_rows,sort_keys=True).encode()).hexdigest(),"started_ns":time.time_ns(),"source":"RUNTIME_PHASE28_REAL_FRESH"}
    atomic_json(out/"input/provenance.json",provenance)
    deps={name:("AVAILABLE" if importlib.util.find_spec(name) else "NOT_AVAILABLE") for name in ("numpy","sklearn","scipy","pandas","pyarrow","jsonschema","pytest")}
    atomic_json(out/"audit/dependencies.json",{"dependencies":deps,"network_install_used":False,"local_learnedcache_material":"NOT_FOUND"})
    state={"schema_version":1,"stage":"INPUT_FROZEN","timestamp_ns":time.time_ns(),"head":head,"raw_manifest_hash":manifest_hash,"resume_supported":True,"failure_reason":None}
    atomic_json(out/"state/state.json",state); atomic_text(out/"state/history.jsonl",json.dumps(state,sort_keys=True)+"\n")
    runtime=project/"outputs/parp_phase29a_runtime_state"; runtime.mkdir(exist_ok=True); atomic_text(runtime/"output_path.txt",str(out)+"\n")
    return provenance


def main():
    p=argparse.ArgumentParser(); p.add_argument("--project",type=Path,required=True); p.add_argument("--tree",type=Path,required=True); p.add_argument("--phase28-real",type=Path,required=True); p.add_argument("--phase28b",type=Path,required=True); p.add_argument("--output",type=Path,required=True)
    a=p.parse_args(); print(json.dumps(run(a.project,a.tree,a.phase28_real,a.phase28b,a.output),sort_keys=True))


if __name__=="__main__": main()
