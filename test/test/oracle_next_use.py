"""Known-sequence ordinal priors. Scores are not model probabilities."""
from bisect import bisect_right
from collections import defaultdict
from datetime import datetime
import ctypes
import fcntl
import hashlib
import json
import math
from pathlib import Path
import sys
import time

from build_user_events_switch_sequence import read_rows
from user_events_plan import build, ROOT
sys.path.insert(0, str(ROOT/'lzx/service/runtime_monitor'))
from core.parp_myfs import (PARPMyfsBridge, PredictStateV3, ABI_VERSION_V3,
    PARP_PREDICT_SET_STATE_V3, Q15_ONE, ENTRY_FOREGROUND, MAX_APPS, MAX_BINDINGS)

FORMAT='oracle_next_use_rank_v1'
MODEL_VERSION=601  # Transport provenance, not a learned model.


def compile_plan(comparison, source):
    original=build(source)
    byrow={e['excel_row']:e for e in original['events']}
    rows=read_rows(comparison)
    if len(rows)!=len(byrow) or {int(r['原 Excel 行号']) for r in rows}!=set(byrow):
        raise ValueError('Comparison does not contain the complete original workbook')
    rows.sort(key=lambda r:(datetime.strptime(r['原始时间'],'%Y-%m-%d %H:%M:%S,%f'),int(r['原 Excel 行号'])))
    segments=[]
    for row in rows:
        event=byrow[int(row['原 Excel 行号'])]
        for key,col in [('event1','原始操作'),('app_name','原始应用'),('operation','当前操作代码'),('timestamp_str','原始时间')]:
            if event[key]!=row[col]:raise ValueError(f'Source/mapping mismatch: {event["excel_row"]} {key}')
        if not row['当前应用 ID'] or row['最终执行结果'] not in {'后置校验通过','仅输入已送达'}:continue
        if int(row['当前应用 ID'])!=event['runtime_app_id']:raise ValueError('Application ID mismatch')
        if not segments or segments[-1]['app_key']!=event['target_app_key']:
            segments.append(dict(index=len(segments),app_key=event['target_app_key'],app_id=event['runtime_app_id'],events=[]))
        segments[-1]['events'].append(event)
    payload=dict(format=FORMAT,source_sha256=original['source_sha256'],
        comparison_sha256=hashlib.sha256(Path(comparison).read_bytes()).hexdigest(),segments=segments)
    payload['plan_sha256']=hashlib.sha256(json.dumps(payload,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    return payload


def bin_for_rank(rank):
    return 7 if rank==1 else 6 if rank==2 else 5 if rank==3 else 4 if rank==4 else 3 if rank<=6 else 2 if rank<=9 else 1 if rank<=12 else 0


class NextUse:
    def __init__(self,segments):
        self.segments=segments;self.positions=defaultdict(list)
        self.ids={}
        for i,s in enumerate(segments):
            self.positions[s['app_key']].append(i);self.ids[s['app_key']]=s['app_id']
        if len(set(self.ids.values()))!=len(self.ids) or any(not isinstance(v,int) or v<=0 for v in self.ids.values()):
            raise ValueError('Invalid or duplicate runtime app IDs')

    def rank(self,index,foreground,opened):
        opened=set(opened)
        if not 0<=index<len(self.segments) or self.segments[index]['app_key']!=foreground:
            raise ValueError('Cursor/foreground mismatch')
        if foreground not in opened or not opened<=self.ids.keys() or len(opened)>MAX_APPS:
            raise ValueError('Invalid live application set')
        future={}
        for app in opened-{foreground}:
            ps=self.positions[app];j=bisect_right(ps,index)
            future[app]=ps[j] if j<len(ps) else None
        early=sorted(future,key=lambda a:(future[a] if future[a] is not None else math.inf,self.ids[a]))
        reclaim=sorted(future,key=lambda a:(-(future[a] if future[a] is not None else math.inf),self.ids[a]))
        ordered=[foreground]+early
        entries=[(self.ids[a],Q15_ONE if a==foreground else 0,i+1,ENTRY_FOREGROUND if a==foreground else 0) for i,a in enumerate(ordered)]
        return entries,dict(format=FORMAT,segment_index=index,foreground=foreground,
            live_apps=sorted(opened),reclaim_order=reclaim,
            rows=[dict(app_key=a,app_id=self.ids[a],next_segment=future.get(a),
                       never_again=(a!=foreground and future[a] is None),rank=i+1,
                       score_q15=entries[i][1],expected_bin=bin_for_rank(i+1)) for i,a in enumerate(ordered)])


def same_state(sent,got):
    fields=['generation','model_version','nr_predictions','nr_bindings','timestamp_ns','ttl_ns']
    if any(getattr(sent,k)!=getattr(got,k) for k in fields):return False
    for i in range(sent.nr_predictions):
        if any(getattr(sent.predictions[i],k)!=getattr(got.predictions[i],k) for k in ['app_id','score_q15','rank','flags']):return False
    # Kernel may reorder bindings; membership and epoch must still agree.
    bind=lambda s:sorted((b.domain_id,b.app_id,b.flags,b.epoch_id) for b in s.bindings[:s.nr_bindings])
    return bind(sent)==bind(got)


class OracleBridge(PARPMyfsBridge):
    def __init__(self,*,policy_root,**kwargs):
        self.policy_root=Path(policy_root).resolve()
        super().__init__(model_version=MODEL_VERSION,prior_ttl_ms=5000,horizon_ms=5000,**kwargs)
        if self.mode=='apply' and self.kernel_abi_version!=3:raise RuntimeError('Oracle requires existing myfs v3 ABI')

    def publish(self,entries,audit,samples):
        samples=list(samples)
        expected_domains={int((self.cgroup_root/s.identity.cgroup_path.lstrip('/')).stat().st_ino) for s in samples}
        if len(expected_domains)>MAX_BINDINGS:raise RuntimeError('Live bindings exceed atomic ABI capacity')
        bindings,_,ambiguous=self._bindings(samples,{e[0] for e in entries})
        # A vanished helper requires a fresh inventory, not a partial batch.
        for s in samples:(self.cgroup_root/s.identity.cgroup_path.lstrip('/')).stat()
        if ambiguous or {a for _,a in bindings}!={e[0] for e in entries} or {d for d,_ in bindings}!=expected_domains:
            raise RuntimeError('Missing/ambiguous live cgroup bindings')
        for _,_,path in self._last_binding_paths.values():
            if not path.resolve().is_relative_to(self.policy_root) or path.resolve()==self.policy_root:
                raise RuntimeError('Binding escaped experiment subtree')
        self.generation+=1
        state=PredictStateV3();state.abi_version=ABI_VERSION_V3;state.struct_size=ctypes.sizeof(state)
        self._fill_state(state,entries,bindings,None,time.monotonic_ns())
        # No working-set probability projection, workload hints, or learned model call.
        if self.mode=='apply':
            previous=getattr(self,'_oracle_previous_state',None)
            if previous is not None and not same_state(previous,self._get_state()):
                raise RuntimeError('Oracle state overwritten by another publisher; lease stopped')
            self._stats['ioctl_attempts']+=1
            fd=self._open()
            try:fcntl.ioctl(fd,PARP_PREDICT_SET_STATE_V3,bytearray(bytes(state)),True)
            finally: __import__('os').close(fd)
            got=self._get_state()
            if not same_state(state,got):raise RuntimeError('Kernel readback differs from submitted oracle state')
            self._oracle_previous_state=PredictStateV3.from_buffer_copy(bytes(state))
            self._stats['ioctl_success']+=1
            self._stats['bindings_submitted']+=len(bindings)
            self._successful_apps={s.app_id for s in samples}
        else:self._stats['dry_runs']+=1
        self._stats['oracle_publications']=self._stats.get('oracle_publications',0)+1
        record=dict(audit,generation=self.generation,ttl_ms=5000,at_ns=state.timestamp_ns,
                    bindings=[dict(domain_id=b.domain_id,app_id=b.app_id,flags=b.flags,
                        epoch_id=b.epoch_id,cgroup_path=str(self._last_binding_paths[b.domain_id][2]))
                        for b in state.bindings[:state.nr_bindings]],
                    mode=self.mode,readback_verified=self.mode=='apply')
        if self.mode=='apply':
            record['kernel_readback']=dict(generation=got.generation,timestamp_ns=got.timestamp_ns,
                ttl_ns=got.ttl_ns,nr_predictions=got.nr_predictions,nr_bindings=got.nr_bindings,
                entries=[dict(app_id=e.app_id,rank=e.rank,score_q15=e.score_q15,flags=e.flags)
                    for e in got.predictions[:got.nr_predictions]])
        with (self.parp_dir/'oracle_updates.jsonl').open('a') as f:f.write(json.dumps(record)+'\n')
        return record
