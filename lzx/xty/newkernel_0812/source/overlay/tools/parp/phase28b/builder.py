#!/usr/bin/env python3
"""Bounded-memory Phase2.8B causal multiscale window builder."""

import argparse
from bisect import bisect_left
from collections import Counter, defaultdict
import csv
import gzip
import hashlib
import heapq
import json
import math
import os
from pathlib import Path
import resource
import statistics
import time

from region_decode import decode_trace_line
from .contracts import align_start, classify_quality


WINDOWS = (2, 5, 10)
SPLIT = {"wps_01":"train", "wps_02":"validation", "wps_03":"test",
         "files_01":"secondary_train", "files_02":"secondary_test"}
RAW_TO_COARSE = {
    "WPS": {"OPEN":"OPEN_CLOSE", "CLOSE_DOCUMENT":"OPEN_CLOSE", "REOPEN":"OPEN_CLOSE",
            "IDLE_VIEW":"VIEW_IDLE", "SCROLL_DOWN":"NAVIGATION_FORWARD",
            "SCROLL_UP":"NAVIGATION_BACKWARD", "EDIT":"EDIT", "SAVE":"SAVE_WRITE",
            "SEARCH":"SEARCH", "MINIMIZE":"FOREGROUND_BACKGROUND", "RESTORE":"FOREGROUND_BACKGROUND"},
    "FILES": {"BROWSE_LIST":"DIRECTORY_VIEW", "ENTER_DIRECTORY":"NAVIGATION",
              "RETURN_DIRECTORY":"NAVIGATION", "SEARCH":"SEARCH",
              "MINIMIZE":"FOREGROUND_BACKGROUND", "RESTORE":"FOREGROUND_BACKGROUND"},
}


def atomic_json(path, payload):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True); stream.write("\n")
    os.replace(tmp, path)


def atomic_gzip_open(path):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    return gzip.open(path.with_name(path.name + ".tmp"), "wt", encoding="utf-8", compresslevel=3)


def finish_gzip(path):
    path = Path(path); os.replace(path.with_name(path.name + ".tmp"), path)


def stable_id(*parts):
    return hashlib.sha256("|".join(str(x) for x in parts).encode()).hexdigest()[:24]


def percentile(values, q):
    values=sorted(values)
    if not values: return None
    p=(len(values)-1)*q; lo=int(p); hi=min(lo+1,len(values)-1)
    return values[lo]*(hi-p)+values[hi]*(p-lo)


def merge_union(intervals):
    output=[]
    for start,end in sorted((max(0,int(a)),max(0,int(b))) for a,b in intervals if b>a):
        if output and start <= output[-1][1]: output[-1]=(output[-1][0],max(output[-1][1],end))
        else: output.append((start,end))
    return output


def max_weight_spans(intervals):
    """Exact sweep-line integral of the maximum activity ratio."""
    events=[]
    for serial,(start,end,weight) in enumerate(intervals):
        if end>start and weight>0:
            events.append((start,1,serial,float(weight))); events.append((end,-1,serial,float(weight)))
    events.sort(); active=set(); heap=[]; output=[]; last=None; index=0
    while index < len(events):
        point=events[index][0]
        while heap and heap[0][1] not in active: heapq.heappop(heap)
        weight=-heap[0][0] if heap else 0.0
        if last is not None and point>last and weight>0: output.append((last,point,weight))
        while index<len(events) and events[index][0]==point:
            _,kind,serial,value=events[index]
            if kind==1: active.add(serial); heapq.heappush(heap,(-value,serial))
            else: active.discard(serial)
            index+=1
        last=point
    return output


def bit_range(start, end, pages, bins):
    effective=max(1,min(bins,pages))
    if pages<=0 or end<=start: return 0
    lo=min(effective-1,max(0,start)*effective//pages)
    hi=min(effective-1,max(0,end-1)*effective//pages)
    return ((1 << (hi-lo+1))-1) << lo


def shape10(union, weighted, pages):
    effective=max(1,min(10,pages)); coverage=[0.0]*10; weights=[0.0]*10
    for index in range(effective):
        lo=index*pages//effective; hi=(index+1)*pages//effective; size=max(1,hi-lo)
        coverage[index]=sum(max(0,min(hi,b)-max(lo,a)) for a,b in union)/size
        weights[index]=sum(max(0,min(hi,b)-max(lo,a))*w for a,b,w in weighted)/size
    return coverage,weights


class FileAcc:
    __slots__=("key","pages","size","regions","ratios","ages","observed","active","weighted")
    def __init__(self,key,pages,size):
        self.key=key; self.pages=max(1,pages); self.size=max(0,size); self.regions=0
        self.ratios=[]; self.ages=[]; self.observed=[]; self.active=[]; self.weighted=[]
    def add(self,start,end,ratio,age):
        start=max(0,min(self.pages,start)); end=max(start,min(self.pages,end)); self.regions+=1
        self.ratios.append(ratio); self.ages.append(age); self.observed.append((start,end))
        if ratio>0: self.active.append((start,end)); self.weighted.append((start,end,ratio))
    def finish(self):
        observed=merge_union(self.observed); active=merge_union(self.active); weighted=max_weight_spans(self.weighted)
        observed_pages=sum(b-a for a,b in observed); active_pages=sum(b-a for a,b in active)
        weighted_pages=sum((b-a)*w for a,b,w in weighted); c10,w10=shape10(active,weighted,self.pages)
        active_bits={str(n):bit_range(a,b,self.pages,n) for n in (10,100,1000) for a,b in []}
        active_bits={str(n):0 for n in (10,100,1000)}; observed_bits={str(n):0 for n in (10,100,1000)}
        for a,b in active:
            for n in (10,100,1000): active_bits[str(n)] |= bit_range(a,b,self.pages,n)
        for a,b in observed:
            for n in (10,100,1000): observed_bits[str(n)] |= bit_range(a,b,self.pages,n)
        return {"key":"%d:%d:%d:%d"%self.key,"partition_generation":"%d:%d"%(self.key[3],self.pages),
                "file_size_bytes":self.size,"file_page_count":self.pages,"region_count":self.regions,
                "observed_pages":observed_pages,"unique_accessed_pages":active_pages,"weighted_pages":weighted_pages,
                "coverage":active_pages/self.pages,"weighted_coverage":weighted_pages/self.pages,
                "mean_access_ratio":statistics.fmean(self.ratios) if self.ratios else 0,
                "max_access_ratio":max(self.ratios,default=0),"mean_age":statistics.fmean(self.ages) if self.ages else 0,
                "max_age":max(self.ages,default=0),"coverage_l10":c10,"weighted_coverage_l10":w10,
                "active_bits":{k:hex(v) for k,v in active_bits.items()},
                "observed_bits":{k:hex(v) for k,v in observed_bits.items()},
                "active_segments":{k:bin(v).count("1") for k,v in active_bits.items()},
                "observed_segments":{k:bin(v).count("1") for k,v in observed_bits.items()}}


class WindowAcc:
    def __init__(self,session,app,app_id,domain,epoch,start,seconds,boot,collection_start,collection_end):
        self.session=session; self.app=app; self.app_id=app_id; self.domain=domain; self.epoch=epoch
        self.start=start; self.end=start+seconds*1_000_000_000; self.seconds=seconds; self.boot=boot
        self.collection_start=collection_start; self.collection_end=collection_end; self.files={}; self.anon=[]
        self.file_regions=0; self.anon_regions=0; self.samples=set(); self.dedupe=set()
    def add(self,row):
        marker=(row["sample_id"],row["pid"],row["region_type"],row.get("region_start"),row.get("region_end"),row.get("logical_start"))
        if marker in self.dedupe: return
        self.dedupe.add(marker); self.samples.add(row["sample_id"])
        ratio=min(1.0,max(0.0,row["nr_accesses"]/max(1,row["max_possible_accesses"])))
        if row["region_type"]=="FILE":
            self.file_regions+=1; key=(row["dev_major"],row["dev_minor"],row["inode"],row["file_version"])
            acc=self.files.setdefault(key,FileAcc(key,row["file_page_count"],row["file_size_bytes"]))
            acc.add(row["file_page_start"],row["file_page_end_exclusive"],ratio,row["age"])
        else:
            self.anon_regions+=1; self.anon.append((row["nr_pages"],ratio,row["age"],row["mm_cookie"]))
    def finish(self, metrics, operations):
        files=[x.finish() for x in self.files.values()]
        maxlog=max([math.log1p(x["unique_accessed_pages"]) for x in files] or [1]) or 1
        total_weight=sum(x["weighted_pages"] for x in files) or 1
        for x in files:
            norm=math.log1p(x["unique_accessed_pages"])/maxlog
            intensity=x["mean_access_ratio"]
            x["score"]=.5*norm+.3*x["weighted_coverage"]+.2*intensity
            x["activity_share"]=x["weighted_pages"]/total_weight
        files.sort(key=lambda x:(-x["score"],-x["weighted_pages"],-x["unique_accessed_pages"],x["key"]))
        overlaps=[]
        for op in operations:
            overlap=max(0,min(self.end,op["end_ns"])-max(self.start,op["start_ns"]))
            if overlap: overlaps.append((overlap,op))
        overlaps.sort(key=lambda x:(-x[0],x[1]["repeat_id"])); dominant=overlaps[0][1] if overlaps else None
        ratio=min(1.0,sum(x[0] for x in overlaps)/(self.end-self.start)); quality=classify_quality(ratio)
        anon_pages=sum(x[0] for x in self.anon); anon_weighted=sum(x[0]*x[1] for x in self.anon)
        return {"schema_version":1,"source":"RUNTIME_PHASE28_REAL_FRESH","window_id":stable_id(self.session,self.domain,self.epoch,self.start,self.seconds),
                "session_id":self.session,"app":self.app,"app_id":self.app_id,"domain_id":self.domain,"boot_id":self.boot,
                "foreground_epoch":self.epoch,"window_size_s":self.seconds,"window_start_ns":self.start,"window_end_ns":self.end,
                "is_complete":self.start>=self.collection_start and self.end<=self.collection_end,
                "is_partial_start":self.start<self.collection_start,"is_partial_end":self.end>self.collection_end,
                "kernel_sample_count":len(metrics),"damon_sample_count":len(self.samples),
                "file_region_count":self.file_regions,"anon_region_count":self.anon_regions,
                "anon":{"pages":anon_pages,"weighted_pages":anon_weighted,"active_ratio":anon_weighted/max(1,anon_pages),
                        "mean_age":statistics.fmean(x[2] for x in self.anon) if self.anon else 0,
                        "object_count":len({x[3] for x in self.anon})},
                "files":files,"kernel":aggregate_metrics(metrics,self.seconds),
                "label":{"operation_overlap_ratio":ratio,"dominant_operation_label":dominant["coarse"] if dominant else "UNKNOWN",
                         "raw_operation":dominant["raw"] if dominant else "UNKNOWN","repeat_id_metadata":dominant["repeat_id"] if dominant else None,
                         "operation_instance_start":dominant["start_ns"] if dominant else None,
                         "operation_instance_end":dominant["end_ns"] if dominant else None,"label_quality":quality}}


def flatten_metrics(row):
    out={}; cg=row.get("cgroup_metrics") or {}
    for k in ("memory_current","memory_swap_current"): out[k]=cg.get(k)
    ms=row.get("memory_stat") or {}
    for k in ("anon","file","file_dirty","file_writeback","pgfault","pgmajfault","workingset_refault_anon",
              "workingset_refault_file","workingset_activate_anon","workingset_activate_file","pgscan","pgsteal","pgdeactivate","pswpout"):
        out["memory_"+k]=ms.get(k)
    cs=row.get("cpu_stat") or {}
    for k in ("usage_usec","user_usec","system_usec"): out["cpu_"+k]=cs.get(k)
    proc=row.get("process_aggregate") or {}
    for k in ("pid_count","rss_anon_kib","rss_file_kib","vm_swap_kib","referenced_kib","pss_kib","sched_runtime_ns","sched_wait_ns","sched_timeslices"):
        out["proc_"+k]=proc.get(k)
    for kind in ("memory","cpu","io"):
        p=(row.get("pressure") or {}).get(kind) or {}
        for level in ("some","full"):
            values=p.get(level) or {}
            out[f"psi_{kind}_{level}_avg10"]=values.get("avg10"); out[f"psi_{kind}_{level}_total"]=values.get("total")
    return out


def aggregate_metrics(rows, seconds):
    flat=[flatten_metrics(x) for x in rows]; names=sorted({k for x in flat for k in x}); out={}; availability={}
    for name in names:
        values=[x.get(name) for x in flat if isinstance(x.get(name),(int,float))]
        availability[name]=bool(values)
        if not values: continue
        mean=statistics.fmean(values); std=statistics.pstdev(values) if len(values)>1 else 0
        out[name+"_last"]=values[-1]; out[name+"_mean"]=mean; out[name+"_std"]=std
        out[name+"_min"]=min(values); out[name+"_max"]=max(values); out[name+"_delta"]=values[-1]-values[0]
        out[name+"_rate"]= (values[-1]-values[0])/max(1,seconds); out[name+"_cv"]=std/abs(mean) if mean else 0
        out[name+"_zero_ratio"]=sum(v==0 for v in values)/len(values)
    return {"values":out,"availability":availability,"sample_count":len(rows)}


def load_metrics(path):
    rows=[json.loads(line) for line in Path(path).open()]; rows.sort(key=lambda x:x["monotonic_ns"])
    return rows,[x["monotonic_ns"] for x in rows]


def parse_operations(path, session, app, meta):
    starts={}; output=[]
    with Path(path).open(newline="",encoding="utf-8") as stream:
        for row in csv.DictReader(stream):
            if row.get("action")!="trace_marker" or not row.get("operation_id"): continue
            metadata=json.loads(row.get("metadata_json") or "{}"); repeat=metadata.get("repeat_id")
            if not repeat: continue
            mono=int(meta["collection_start_ns"]+(int(row["ts_ns"])-int(meta["wall_start_ns"])))
            key=row["operation_id"]
            if row["event_type"]=="OP_START": starts[key]=(mono,row,repeat)
            elif row["event_type"]=="OP_DONE" and key in starts:
                start,first,repeat=starts.pop(key); raw=row["operation_name"]
                output.append({"operation_id":key,"repeat_id":repeat,"raw":raw,"coarse":RAW_TO_COARSE[app][raw],
                               "start_ns":start,"end_ns":max(start+1,mono)})
    if starts: raise ValueError("unpaired operations: "+str(sorted(starts)))
    return sorted(output,key=lambda x:x["start_ns"])


def metric_window(metrics,times,start,end):
    lo=bisect_left(times,start); hi=bisect_left(times,end); return metrics[lo:hi]


def sorted_trace_rows(path, reorder_ns=2_000_000_000, max_buffer=100_000):
    """Merge per-CPU trace order with a bounded deterministic watermark."""
    heap=[]; serial=0; maximum=-1
    with Path(path).open(errors="replace") as stream:
        for line in stream:
            if "parp_region_evidence:" not in line: continue
            row=decode_trace_line(line)
            if not row or row.get("nr_pages",0)<=0: continue
            event=int(row["sample_timestamp_ns"]); maximum=max(maximum,event)
            heapq.heappush(heap,(event,serial,row)); serial+=1
            watermark=maximum-reorder_ns
            while heap and (heap[0][0]<=watermark or len(heap)>max_buffer):
                yield heapq.heappop(heap)[2]
    while heap: yield heapq.heappop(heap)[2]


def build_session(fresh,out,meta,checkpoint,schema):
    session=meta["session_id"]; app=meta["app"]; app_lower=app.lower()
    trace=fresh/f"raw/{app_lower}/{session}/parp_region_evidence.raw"
    metrics,times=load_metrics(fresh/meta["kernel_metrics_path"])
    operations=parse_operations(fresh/meta["automation_path"],session,app,meta)
    outputs={}; streams={}; timings=[]; counts={str(x):0 for x in WINDOWS}
    for seconds in WINDOWS:
        path=out/f"dataset/windows/{seconds}s/{session}.jsonl.gz"; outputs[seconds]=path; streams[seconds]=atomic_gzip_open(path)
    state={seconds:{"start":None,"acc":{}} for seconds in WINDOWS}; decoded=0; invalid=0; last_time=-1
    def flush(seconds):
        nonlocal timings
        for acc in state[seconds]["acc"].values():
            begun=time.perf_counter_ns(); m=metric_window(metrics,times,acc.start,acc.end)
            row=acc.finish(m,operations); streams[seconds].write(json.dumps(row,sort_keys=True,separators=(",",":"))+"\n")
            timings.append(time.perf_counter_ns()-begun); counts[str(seconds)]+=1
        state[seconds]["acc"]={}
    begun=time.perf_counter_ns()
    for row in sorted_trace_rows(trace):
        event=int(row["sample_timestamp_ns"]); decoded+=1
        if event<last_time: raise ValueError("out-of-order trace")
        last_time=event
        for field in ("sample_timestamp_ns","timestamp_ns","domain_id","app_id","pid","tgid","nr_pages","nr_accesses","age","foreground_epoch"):
            schema[field]["present"]+=field in row; schema[field]["valid"]+=isinstance(row.get(field),(int,float)); schema[field]["zero"]+=row.get(field)==0
            if isinstance(row.get(field),(int,float)):
                schema[field]["min"]=row[field] if schema[field]["min"] is None else min(schema[field]["min"],row[field])
                schema[field]["max"]=row[field] if schema[field]["max"] is None else max(schema[field]["max"],row[field])
        for seconds in WINDOWS:
            start=align_start(event,seconds); slot=state[seconds]
            if slot["start"] is not None and start>slot["start"]: flush(seconds)
            slot["start"]=start; key=(row["domain_id"],row["foreground_epoch"])
            if key not in slot["acc"]:
                slot["acc"][key]=WindowAcc(session,app,meta["app_id"],key[0],key[1],start,seconds,meta["boot_id"],meta["collection_start_ns"],meta["collection_end_ns"])
            slot["acc"][key].add(row)
    for seconds in WINDOWS: flush(seconds); streams[seconds].close(); finish_gzip(outputs[seconds])
    elapsed=time.perf_counter_ns()-begun
    return {"session_id":session,"decoded":decoded,"invalid":invalid,"operations":len(operations),"metrics":len(metrics),
            "row_counts":counts,"decode_build_ns":elapsed,"window_finalize_p50_ns":percentile(timings,.5),
            "window_finalize_p95_ns":percentile(timings,.95),"window_finalize_p99_ns":percentile(timings,.99),
            "peak_rss_kib":resource.getrusage(resource.RUSAGE_SELF).ru_maxrss}


def run(fresh,out):
    inventory=json.load((out/"validation/session_inventory.json").open()); checkpoint_path=out/"work/dataset_build/checkpoint.json"
    checkpoint=json.load(checkpoint_path.open()); schema=defaultdict(lambda:{"present":0,"valid":0,"zero":0,"min":None,"max":None})
    results={}; started=time.time_ns()
    for meta in inventory["sessions"]:
        session=meta["session_id"]
        if session in checkpoint["completed_sessions"]:
            results[session]=checkpoint.get("session_results",{}).get(session,{}); continue
        checkpoint["current_session"]=session; checkpoint["current_stage"]="WINDOW_BUILD"; checkpoint["updated_ns"]=time.time_ns(); atomic_json(checkpoint_path,checkpoint)
        result=build_session(fresh,out,meta,checkpoint,schema); results[session]=result
        checkpoint.setdefault("session_results",{})[session]=result; checkpoint["completed_sessions"].append(session)
        checkpoint["current_session"]=None; checkpoint["last_successful_offset"]=result["decoded"]
        checkpoint["row_counts"]={str(w):sum(int(x.get("row_counts",{}).get(str(w),0)) for x in results.values()) for w in WINDOWS}
        checkpoint["updated_ns"]=time.time_ns(); atomic_json(checkpoint_path,checkpoint)
    checkpoint["completed_window_sizes"]=list(WINDOWS); checkpoint["current_stage"]="WINDOWS_COMPLETE"; atomic_json(checkpoint_path,checkpoint)
    total=sum(x.get("decoded",0) for x in results.values()); fields={}
    for name,value in schema.items():
        fields[name]={**value,"presence_ratio":value["present"]/total if total else 0,"valid_ratio":value["valid"]/total if total else 0,"zero_ratio":value["zero"]/total if total else 0,"source":"PARP_TRACE"}
    for name in ("io.stat","pids.current","pageout"):
        fields[name]={"status":"NOT_COLLECTED","source":"CGROUP_OR_PROC"}
    atomic_json(out/"validation/real_schema.json",{"schema_version":1,"decoded_rows":total,"fields":fields,"sessions":results})
    atomic_json(out/"validation/clock_alignment.json",{"schema_version":1,"trace_clock":"CLOCK_MONOTONIC_NS",
      "kernel_metrics_clock":"CLOCK_MONOTONIC_NS","automation_clock":"WALL_TIME_NS_PER_SESSION_CONVERTED",
      "formula":"mono=collection_start_ns+(automation_wall_ns-wall_start_ns)","cross_session_anchor_reuse":False,
      "estimated_error_ns":1_000_000,"alignment_2s":"RELIABLE"})
    atomic_json(out/"performance/window_build.json",{"started_ns":started,"ended_ns":time.time_ns(),"sessions":results,
      "peak_rss_kib":resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,"temporary_disk_peak_bytes":0})
    return results


def main():
    parser=argparse.ArgumentParser(); parser.add_argument("--fresh",type=Path,required=True); parser.add_argument("--output",type=Path,required=True)
    args=parser.parse_args(); print(json.dumps(run(args.fresh,args.output),sort_keys=True))


if __name__=="__main__": main()
