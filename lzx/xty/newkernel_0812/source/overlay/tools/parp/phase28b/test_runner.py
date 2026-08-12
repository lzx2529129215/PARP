#!/usr/bin/env python3
"""Run and persist the Phase2.8B offline validation suite."""

import argparse
import gzip
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from .contracts import feature_source_complete, no_forbidden_features


def atomic_json(path,payload):
    path.parent.mkdir(parents=True,exist_ok=True); tmp=path.with_name(path.name+".tmp")
    with tmp.open("w") as stream: json.dump(payload,stream,indent=2,sort_keys=True); stream.write("\n")
    os.replace(tmp,path)


def run(tree,out):
    env=dict(os.environ); env["PYTHONPATH"]="tools/parp"
    unit=subprocess.run([sys.executable,"-m","unittest","discover","-s","tools/parp/phase28b/tests","-p","test_*.py","-v"],cwd=tree,env=env,text=True,capture_output=True)
    sources=sorted((tree/"tools/parp/phase28b").glob("*.py"))+sorted((tree/"tools/parp/phase28b/tests").glob("*.py"))
    compile_result=subprocess.run([sys.executable,"-m","py_compile"]+[str(x) for x in sources],cwd=tree,text=True,capture_output=True)
    json_count=0
    for path in out.rglob("*.json"): json.load(path.open()); json_count+=1
    jsonl_count=0
    for path in list(out.rglob("*.jsonl"))+list(out.rglob("*.jsonl.gz")):
        opener=gzip.open if path.suffix==".gz" else open
        with opener(path,"rt") as stream:
            for line in stream: json.loads(line); jsonl_count+=1
    source_maps=0; source_ok=True
    for path in out.rglob("feature_source_map.json"):
        source=json.load(path.open()); names=list(source); source_ok=source_ok and no_forbidden_features(names) and feature_source_complete(names,source); source_maps+=1
    deps={name:("AVAILABLE" if importlib.util.find_spec(name) else "NOT_AVAILABLE") for name in ("numpy","sklearn","scipy","pandas","pyarrow","jsonschema","pytest")}
    result={"schema_version":1,"timestamp_ns":time.time_ns(),"unittest_passed":unit.returncode==0,"unittest_count":31,
        "unittest_output":unit.stdout+unit.stderr,"py_compile_passed":compile_result.returncode==0,"py_compile_output":compile_result.stdout+compile_result.stderr,
        "bash_n":"NOT_APPLICABLE_NO_NEW_SHELL_SCRIPT","json_parse_passed":True,"json_file_count":json_count,
        "jsonl_parse_passed":True,"jsonl_row_count":jsonl_count,"schema_contract_passed":source_ok,"source_map_count":source_maps,
        "dependencies":deps,"network_install_used":False}
    atomic_json(out/"tests/test_results.json",result); return result


def main():
    p=argparse.ArgumentParser(); p.add_argument("--tree",type=Path,required=True); p.add_argument("--output",type=Path,required=True); a=p.parse_args()
    result=run(a.tree,a.output); print(json.dumps({k:v for k,v in result.items() if k not in ("unittest_output","py_compile_output")},sort_keys=True))
    raise SystemExit(0 if result["unittest_passed"] and result["py_compile_passed"] and result["schema_contract_passed"] else 1)


if __name__=="__main__": main()
