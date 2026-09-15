#!/usr/bin/env python3
"""R1-R5 real GUI effects with an explicit visit-window -> kernel-bin adapter.

Original M/R runners remain intact. Native means the SAME PARP kernel with its
optimization switches off. No synthetic allocator, MADV_COLD or memory.reclaim.
"""
from __future__ import annotations
import argparse
import contextlib
import copy
import csv
import datetime as dt
import hashlib
import json
import os
import random
import shlex
import signal
import subprocess
import sys
import threading
import time
from collections import defaultdict
from pathlib import Path
from types import SimpleNamespace

HERE=Path(__file__).resolve().parent
sys.path.insert(0,str(HERE))
import visit_window_scenarios as V
from visit_window_bin import VisitWindowBinBridge, COLD_Q15
from core.runtime_scope import load_runtime_app_scope

REAL=V.load_module('visit_real_existing_gui',HERE/'parp-real-pc-experiment-lzx.py')
CONFIG=HERE/'visit-window-real-effects.json'
DATA=V.PREDICTOR/'data/lsapp_expanded/processed/app_visit_window_v1'
SCOPE=V.REPO/'test/configs/lsapp_aligned/runtime_app_scope.json'
MIB=1024*1024
SERVICE='parp-runtime-monitor.service'
DEBUG=Path('/sys/kernel/debug/parp')


def select_trace(config, scenario):
    """Select by source behavior only, never by a prediction or test outcome."""
    meta=json.loads((DATA/'dataset_meta.json').read_text())
    sessions=defaultdict(list)
    for row in csv.DictReader((DATA/'segments.csv').open()):
        if meta['splits']['test']['start'] <= row['start_time'] <= meta['splits']['test']['end']:
            sessions[row['session_id']].append(row)
    active={'Firefox','Thunderbird','VLC'}
    candidates=[]
    for sid,rows in sorted(sessions.items()):
        for i in range(4,len(rows)-1):
            history=rows[i-4:i+1]
            start=dt.datetime.fromisoformat(history[0]['start_time'])
            anchor=dt.datetime.fromisoformat(rows[i]['start_time'])
            end=anchor+dt.timedelta(seconds=config['followup_s'])
            if end>dt.datetime.fromisoformat(meta['splits']['test']['end']):continue
            future=[r for r in rows[i+1:] if dt.datetime.fromisoformat(r['start_time']) <= end]
            if not 12 <= (anchor-start).total_seconds() <= 180 or not future:
                continue
            if any(float(r['dwell_s']) < 3 for r in history[:-1]):
                continue
            if not 8 <= float(rows[i]['dwell_s']) <= 30 or future[0]['app']!='Firefox' or rows[i]['app']=='Firefox':
                continue
            if any(float(r['dwell_s']) < 8 for r in future[:-1]):
                continue
            if dt.datetime.fromisoformat(rows[i]['observed_until']) < end:
                continue
            names={r['app'] for r in history+future}
            if not names <= active or (scenario=='r3' and names != active):
                continue
            events=[{'at_s':(dt.datetime.fromisoformat(r['start_time'])-start).total_seconds(),
                     'app':next(k for k,v in V.ACCEPT.LSAPP_NAME_BY_APP_KEY.items() if v==r['app']),
                     'source_segment_id':r['segment_id']} for r in history+future]
            candidates.append({'session_id':sid,'source_start':start.isoformat(),
                'anchor_s':(anchor-start).total_seconds(),'duration_s':(end-start).total_seconds(),'events':events})
    if not candidates:
        raise ValueError(f'no heldout trace meets predeclared {scenario} criteria; do not synthesize one')
    selected=copy.deepcopy(random.Random(config['seed']+int(scenario[1:])).choice(candidates))
    selected.update(source_split='test',eligible_sequences=len(candidates),
        selection='fixed_seed_behavior_only_no_prediction_filter',
        segments_sha256=hashlib.sha256((DATA/'segments.csv').read_bytes()).hexdigest())
    return selected


def make_plan(config, scenario):
    trace=select_trace(config,scenario)
    # Paths are symbolic so Native and PARP execute one identical plan/hash.
    prepare=[]
    for app in config['apps']:
        prepare.append({'app':app,'actions':REAL.app_native_steps(app,'prepare',Path('/__PARP_RUN__'))})
    return {'schema_version':1,'scenario':scenario,'seed':config['seed'],'apps':config['apps'],
        'prepare':prepare,'trace':trace,
        'dirty_refresh':[{'app':a,'actions':REAL.app_native_steps(a,'dirty_refresh',Path('/__PARP_RUN__'))}
                         for a in ['GIMP','LIBREOFFICE']] if scenario in {'r4','r5'} else [],
        'pressure':{'mechanism':'memory.max_after_real_GUI_content_loading',
                    'reclaim_deficit_mib':config['reclaim_deficit_mib'],
                    'extra_images':['image-test-07.png','image-test-08.png'],
                    'memory_max_reused_exactly_within_pair':True},
        'cold_hot_assignment':'model_only','input_clock':'source_time_plus_unscaled_monotonic_elapsed',
        'input_distribution':'heldout_foreground_trace_with_explicit_eight_app_stress_open_set',
        'probe':{'app':'FIREFOX','key':'Page_Down','endpoint':'changed rendered frame stable for three samples'},
        'require_file_dirty':scenario in {'r4','r5'},'require_writepage_promotion':scenario=='r5'}


def privileged_read(path):
    return subprocess.check_output(['sudo','-n','cat',str(path)],text=True,timeout=5).strip()


def write_control(path,value):
    V.ACCEPT.privileged_write(Path(path),value)


@contextlib.contextmanager
def controls(policy,scenario,out):
    changes={DEBUG/'mode':0 if policy=='native' else 2,
        DEBUG/'effective_tier_mode':0,
        Path('/proc/sys/vm/tier2_predict_enabled'):0,
        Path('/proc/sys/vm/tier2_wss_predict_enabled'):0,
        Path('/proc/sys/vm/tier2_wss_strengthen_enabled'):0,
        Path('/proc/sys/vm/parp_reclaim_bin_enabled'):int(policy=='parp'),
        Path('/proc/sys/vm/parp_reclaim_cold_aggressive_enabled'):int(policy=='parp' and scenario in {'r4','r5'}),
        Path('/proc/sys/vm/parp_reclaim_workload_enabled'):int(policy=='parp' and scenario in {'r4','r5'}),
        Path('/proc/sys/vm/parp_reclaim_cold_probability_q15'):COLD_Q15,
        Path('/proc/sys/vm/parp_reclaim_cold_bin_max'):0}
    # Same writeback controls in both halves of R4/R5; restore even on INVALID.
    if scenario in {'r4','r5'}:
        changes.update({Path('/proc/sys/vm/dirty_background_bytes'):4096*MIB,
                        Path('/proc/sys/vm/dirty_bytes'):6144*MIB})
    if scenario=='r5':changes[Path('/proc/sys/vm/laptop_mode')]=600
    originals={p:privileged_read(p) for p in changes}
    # Byte/ratio sysctls implicitly reset each other, so retain both forms.
    ratios={Path('/proc/sys/vm/'+n):privileged_read('/proc/sys/vm/'+n)
            for n in ('dirty_background_ratio','dirty_ratio')} if scenario in {'r4','r5'} else {}
    V.write_json(out/'controls-before.json',{str(p):v for p,v in {**originals,**ratios}.items()})
    try:
        for p,v in changes.items():write_control(p,v)
        actual={str(p):privileged_read(p) for p in changes}
        if any(actual[str(p)]!=str(v) for p,v in changes.items()):raise RuntimeError('kernel control readback mismatch')
        V.write_json(out/'controls-active.json',actual)
        yield
    finally:
        restore_errors=[]
        for p,v in reversed(list(originals.items())):
            try:write_control(p,v)
            except Exception as exc:restore_errors.append(str(exc))
        for p,v in ratios.items():
            if int(v):
                try:write_control(p,v)
                except Exception as exc:restore_errors.append(str(exc))
        V.write_json(out/'controls-restored.json',{'errors':restore_errors})
        if restore_errors:raise RuntimeError('control restoration failed: '+'; '.join(restore_errors))


def cgroup_for_unit(unit):
    path=subprocess.check_output(['systemctl','--user','show',unit,'-p','ControlGroup','--value'],text=True).strip()
    if not path:raise RuntimeError(f'no cgroup for {unit}')
    return Path('/sys/fs/cgroup')/path.lstrip('/')


def snapshot(paths,root):
    def one(path):
        return {'memory_current':int((path/'memory.current').read_text()),
            'memory_swap':int((path/'memory.swap.current').read_text()),
            'memory_stat':REAL.read_kv(path/'memory.stat'),
            'memory_events':REAL.read_kv(path/'memory.events'),
            'psi':REAL.read_psi(path/'memory.pressure')}
    return {'monotonic_s':time.monotonic(),'root':one(root),'apps':{k:one(p) for k,p in paths.items()}}


def counter_delta(before,after):
    result={}
    for app,b in before['apps'].items():
        a=after['apps'][app]
        result[app]={k:a['memory_stat'].get(k,0)-b['memory_stat'].get(k,0)
                     for k in ('workingset_refault_anon','workingset_refault_file','pgmajfault','pgscan','pgsteal')}
        result[app].update(memory_drop_bytes=b['memory_current']-a['memory_current'],
            swap_change_bytes=a['memory_swap']-b['memory_swap'],
            psi_some_us=a['psi'].get('some_total',0)-b['psi'].get('some_total',0),
            psi_full_us=a['psi'].get('full_total',0)-b['psi'].get('full_total',0))
        # Do not silently label a system-wide vmstat count as Firefox swap-in.
        result[app]['pswpin']=a['memory_stat']['pswpin']-b['memory_stat'].get('pswpin',0) if 'pswpin' in a['memory_stat'] else None
    return result


def live_samples(windows):
    _,found,_=windows.snapshot()
    samples=[]
    for key,items in found.items():
        for item in items:
            cg=Path(f'/proc/{item.pid}/cgroup').read_text()
            path=next(line.split(':',2)[2] for line in cg.splitlines() if line.startswith('0::'))
            samples.append(SimpleNamespace(app_id=key,identity=SimpleNamespace(cgroup_path=path)))
    return samples


class ClockAndSink:
    def __init__(self,runner,source_start,mono_start,sink,windows):
        self.runner=runner; self.source_start=dt.datetime.fromisoformat(source_start)
        self.mono_start=mono_start
        self.wall_start=dt.datetime.now()-dt.timedelta(seconds=time.monotonic()-mono_start)
        self.sink=sink;self.windows=windows
    def __getattr__(self,name):return getattr(self.runner,name)
    def call(self,method,row,*args):
        feature=dict(row);feature['timestamp']=(self.source_start+dt.timedelta(seconds=time.monotonic()-self.mono_start)).isoformat()
        result=getattr(self.runner,method)(feature,*args)
        if result.get('inference_executed'):
            transport=copy.deepcopy(result)
            offset=self.wall_start-self.source_start
            for r in transport['all_probabilities']:
                for key in ('predicted_at','expires_at'):
                    r[key]=(dt.datetime.fromisoformat(r[key])+offset).isoformat()
            self.sink.submit_prediction(row,transport,process_samples=live_samples(self.windows))
            if self.sink.mode=='apply' and self.sink._stats['ioctl_success']<self.sink._stats['successful_inferences']:
                raise RuntimeError('kernel prediction submission failed')
        return result
    def process_event(self,row,event):return self.call('process_event',row,event)
    def process_sample(self,row):return self.call('process_sample',row)


def execute_steps(group,windows,ctx,run_dir):
    V.owned_switch(group['app'],windows,ctx)
    for action in group['actions']:
        action=json.loads(json.dumps(action).replace('/__PARP_RUN__',str(run_dir)).replace('{RUN}',str(run_dir)))
        V.AUTO.ACTION_HANDLERS[action['type']](action,ctx)


def dirty_gate(plan,config,prediction,snap):
    cold={r['app_key'] for r in prediction['all_probabilities'] if r['thermal_state']=='cold'}
    hot={r['app_key'] for r in prediction['all_probabilities'] if r['thermal_state'] in {'hot','foreground'}}
    dirty=sum(snap['apps'][a]['memory_stat'].get('file_dirty',0) for a in cold)
    cold_clean=sum(max(0,snap['apps'][a]['memory_stat'].get('file',0)-snap['apps'][a]['memory_stat'].get('shmem',0)-snap['apps'][a]['memory_stat'].get('file_dirty',0)-snap['apps'][a]['memory_stat'].get('file_writeback',0)) for a in cold)
    protected_clean=sum(max(0,snap['apps'][a]['memory_stat'].get('file',0)-snap['apps'][a]['memory_stat'].get('shmem',0)-snap['apps'][a]['memory_stat'].get('file_dirty',0)-snap['apps'][a]['memory_stat'].get('file_writeback',0)) for a in hot)
    deficit=config['reclaim_deficit_mib']*MIB
    valid=not plan['require_file_dirty'] or (dirty>=config['minimum_cold_file_dirty_mib']*MIB and cold_clean<deficit<=cold_clean+dirty and protected_clean>0)
    return {'valid':valid,'cold_apps':sorted(cold),'protected_apps':sorted(hot),'cold_file_dirty_bytes':dirty,
        'cold_clean_estimate_bytes':cold_clean,'protected_clean_estimate_bytes':protected_clean,
        'deficit_bytes':deficit,'capacity_contract':'cold_clean < deficit <= cold_clean + cold_file_dirty; protected_clean > 0',
        'limitation':'cgroup counters are aggregate estimates, not proof of per-page reclaimability; anon does not substitute for file_dirty'}


def run_half(config,plan,policy,out,pair_reference=None,gui_check=False):
    out.mkdir(parents=True,exist_ok=False);V.write_json(out/'action-plan.json',plan)
    cfg={**config,'vocab_names':{a:V.ACCEPT.LSAPP_NAME_BY_APP_KEY[a] for a in config['apps']}}
    V.prepare(out,cfg)
    ctx=V.AUTO.Context(dry_run=False,test_slice='parp-visit-effects.slice')
    units={};observer=None;windows=None;sink=None; sampler_stop=threading.Event();sampler=None;root=None
    result={'scenario':plan['scenario'],'policy':policy,'status':'INVALID','path':str(out)}
    telemetry=[];errors=[];probes=[]
    signal.alarm(config['maximum_round_seconds'])
    try:
        with contextlib.nullcontext() if gui_check else controls(policy,plan['scenario'],out):
            for app in config['apps']:
                command=V.ACCEPT.app_specs(out)[app].command
                if app=='GIMP':command=f'env GIMP2_DIRECTORY={shlex.quote(str(out/"fixtures/gimp-profile"))} '+command.replace('gimp ','gimp --new-instance ',1)
                if app in {'EVINCE','IMAGE_VIEWER','SOLITAIRE'}:command='dbus-run-session -- '+command
                if app=='VLC':command+=' --no-audio'
                name='visit-effects-'+hashlib.sha256(str(out).encode()).hexdigest()[:12]+'-'+app.lower()
                V.AUTO.launch({'name':name,'scope_name':name,'command':command},ctx);units[app]=ctx.processes[name]
            V.write_json(out/'owned-scopes.json',units)
            windows=V.Windows(units,out)
            deadline=time.monotonic()+180
            while time.monotonic()<deadline:
                _,found,_=windows.snapshot()
                if all(V.content_windows(a,found,out) for a in config['apps']):break
                time.sleep(.5)
            else:raise RuntimeError('owned application content window startup failed')
            paths={a:cgroup_for_unit(u) for a,u in units.items()};root=cgroup_for_unit(ctx.test_slice)
            if any(not p.is_relative_to(root) for p in paths.values()):raise RuntimeError('application outside experiment subtree')
            if not gui_check:
                write_control(root/'memory.max','max')
                write_control(root/'memory.tier2_enabled',int(policy=='parp'))
                write_control(root/'memory.swap.max',config['memory_swap_max_mib']*MIB)
            for group in plan['prepare']:execute_steps(group,windows,ctx,out)
            # Additional decoded image working set comes from ordinary Open UI.
            execute_steps({'app':'GIMP','actions':[{'type':'open_file','path':'{RUN}/fixtures/'+n,'wait_after':3.0} for n in plan['pressure']['extra_images']]},windows,ctx,out)
            for group in plan['dirty_refresh']:execute_steps(group,windows,ctx,out)
            scope=load_runtime_app_scope(SCOPE)
            sink=VisitWindowBinBridge(policy_root=root,mode='apply' if policy=='parp' else 'dry-run',device='/dev/myfs',
                                     runtime_scope=scope,output_dir=out,session_id=out.name)
            if policy=='parp' and sink.kernel_abi_version<3:raise RuntimeError('requires myfs ABI v3')
            first=plan['trace']['events'][0];V.owned_switch(first['app'],windows,ctx)
            observer=V.Observer(out,units,cfg,V.DEFAULT_CHECKPOINT)
            start=time.monotonic()
            observer.runner=ClockAndSink(observer.runner,plan['trace']['source_start'],start,sink,observer.windows)
            observer.thread.start()
            if not observer.startup_ready.wait(10) or observer.error:raise RuntimeError(observer.error or 'observer timeout')
            def sample():
                try:
                    with (out/'memory-timeline.jsonl').open('w') as f:
                        while not sampler_stop.is_set():
                            value=snapshot(paths,root);telemetry.append(value);f.write(json.dumps(value)+'\n');f.flush()
                            if value['root']['memory_events'].get('oom_kill',0):raise RuntimeError('OOM in experiment subtree')
                            sampler_stop.wait(config['sample_interval_s'])
                except Exception as exc:errors.append(str(exc))
            sampler=threading.Thread(target=sample,daemon=True);sampler.start()
            anchored=False;before=None;boundary=None;pressure_thread=None;pressure_errors=[];executed=[]
            for event in plan['trace']['events']:
                observer.wait_until(start+event['at_s'])
                probe=anchored and event['app']=='FIREFOX'
                visual={'app_key':'FIREFOX','class':'epiphany|Epiphany','title':'PARP local page',
                        'baseline_key':'return','sample_width':32,'sample_height':32}
                if probe:V.AUTO.capture_visual_baseline(visual,ctx)
                tick=time.monotonic();actual=V.owned_switch(event['app'],windows,ctx)
                V.AUTO.key({'key':'Page_Down' if event['app']!='VLC' else 'space'},ctx)
                elapsed=time.monotonic()-tick
                if probe:
                    V.AUTO.wait_visual_stable({**visual,'require_visual_change':True,'timeout':5,
                        'poll_seconds':.05,'stable_samples':3,'mean_abs_delta':.75,'minimum_change_delta':1.0},ctx)
                    probes.append({'at_s':event['at_s'],'command_completion_s':elapsed,
                        'rendered_ready_s':time.monotonic()-tick,'endpoint':'changed rendered window stable for three samples'})
                executed.append({**event,**actual,'lateness_s':actual['verified_at']-start-event['at_s']})
                V.write_json(out/'verified-actions.json',executed)
                if executed[-1]['lateness_s']>config['maximum_action_lateness_s']:raise RuntimeError('missed original event deadline')
                if event['at_s']==plan['trace']['anchor_s']:
                    observer.wait_until(time.monotonic()+.3)
                    if not observer.calls or observer.calls[-1]['result']['mapped_foreground_app']!=cfg['vocab_names'][event['app']]:raise RuntimeError('anchor foreground not observed')
                    before=snapshot(paths,root);V.write_json(out/'before-pressure.json',before)
                    kernel_before={} if gui_check else {name:privileged_read(DEBUG/name) for name in ('reclaim_bin_stats','reclaim_cold_stats')}
                    V.write_json(out/'kernel-before.json',kernel_before)
                    gate=dirty_gate(plan,config,observer.calls[-1]['result'],before);V.write_json(out/'dirty-capacity-gate.json',gate)
                    if not gate['valid']:raise RuntimeError('file_dirty/capacity gate failed; not substituting anonymous memory')
                    usage=before['root']['memory_current']
                    if usage<config['minimum_working_set_mib']*MIB:raise RuntimeError('insufficient real GUI working set')
                    boundary=(usage-config['reclaim_deficit_mib']*MIB)//MIB*MIB if pair_reference is None else pair_reference['memory_max_bytes']
                    if pair_reference and abs(usage-pair_reference['pre_pressure_bytes'])/pair_reference['pre_pressure_bytes']>config['maximum_pair_working_set_difference_ratio']:raise RuntimeError('Native/PARP initial working sets differ beyond tolerance')
                    if boundary<=0:raise RuntimeError('invalid memory boundary')
                    result.update(memory_max_bytes=boundary,pre_pressure_bytes=usage)
                    V.write_json(out/'pressure-boundary.json',{'memory_max_bytes':boundary,'pre_pressure_bytes':usage})
                    def limit():
                        try:
                            if not gui_check:write_control(root/'memory.max',boundary)
                        except Exception as exc:pressure_errors.append(str(exc))
                    pressure_thread=threading.Thread(target=limit,daemon=True);pressure_thread.start();anchored=True
                if errors or pressure_errors:raise RuntimeError('; '.join(errors+pressure_errors))
            observer.wait_until(start+plan['trace']['duration_s'])
            if pressure_thread:pressure_thread.join(20)
            if errors or pressure_errors:raise RuntimeError('; '.join(errors+pressure_errors))
            if observer.error:raise RuntimeError(observer.error)
            if observer.maximum_sample_gap>1 or observer.unknown_seconds>2:raise RuntimeError('foreground observation quality gate failed')
            after=snapshot(paths,root);V.write_json(out/'after-pressure.json',after)
            kernel_after={name:privileged_read(DEBUG/name) for name in kernel_before}
            V.write_json(out/'kernel-after.json',kernel_after)
            kernel_delta={name:REAL.policy_stat_delta(kernel_before[name],kernel_after[name]) for name in kernel_before}
            V.write_json(out/'kernel-delta.json',kernel_delta)
            V.write_json(out/'foreground_entries.json',observer.entries)
            V.write_json(out/'return-probes.json',probes)
            prediction_metrics,arrays=V.evaluate_calls(observer.calls,observer.entries,observer.observed_until,
                start+plan['trace']['anchor_s'],start+plan['trace']['duration_s'],observer.runner.vocab)
            V.write_json(out/'prediction-evaluation.json',prediction_metrics)
            V.np.savez_compressed(out/'scored-predictions.npz',**arrays)
            minimum=min(x['root']['memory_current'] for x in telemetry if x['monotonic_s']>=before['monotonic_s'])
            achieved=before['root']['memory_current']-minimum
            if not gui_check and achieved<config['minimum_reclaim_mib']*MIB:raise RuntimeError('insufficient observed memory drop under boundary')
            if not gui_check and after['root']['memory_stat'].get('pgsteal',0)<=before['root']['memory_stat'].get('pgsteal',0):raise RuntimeError('no reclaimed pages observed')
            if policy=='parp' and kernel_delta['reclaim_bin_stats'].get('context_hits',0)<=0:raise RuntimeError('no prediction-backed kernel bin action')
            if policy=='parp' and plan['require_writepage_promotion'] and kernel_delta['reclaim_cold_stats'].get('writepage_promotions',0)<=0:raise RuntimeError('required writepage promotion did not occur')
            result.update(status='GUI_CHECK_PASS' if gui_check else 'PASS',observed_memory_drop_bytes=achieved,app_deltas=counter_delta(before,after),
                          probes=probes,prediction_calls=len(observer.calls),
                          hot_prediction_calls=sum(bool(c['result']['hot_apps']) for c in observer.calls),
                          maximum_sample_gap_s=observer.maximum_sample_gap,kernel_delta=kernel_delta,
                          prediction_metrics=prediction_metrics)
    except Exception as exc:result.update(status='INVALID',error=str(exc))
    finally:
        signal.alarm(0)
        sampler_stop.set()
        if sampler:sampler.join(3)
        if observer:observer.stop.set();observer.thread.join(5)
        if sink:sink.close()
        if windows:windows.reader.close()
        cleanup_errors=[]
        if root and root.exists() and not gui_check:
            for name,value in [('memory.max','max'),('memory.tier2_enabled',0),('memory.swap.max','max')]:
                try:write_control(root/name,value)
                except Exception as exc:cleanup_errors.append(str(exc))
        try:V.cleanup_owned(out)
        except Exception as exc:cleanup_errors.append(str(exc))
        try:V.AUTO.cleanup_tracked_processes(ctx)
        except Exception as exc:cleanup_errors.append(str(exc))
        if cleanup_errors:result.update(status='INVALID',cleanup_errors=cleanup_errors)
        V.write_json(out/'result.json',result)
    return result


def preflight(out):
    expected=['/dev/myfs','/proc/sys/vm/parp_reclaim_bin_enabled','/proc/sys/vm/parp_reclaim_cold_probability_q15',
              '/proc/sys/vm/parp_reclaim_workload_enabled']
    report={'kernel':os.uname().release,'missing':[p for p in expected if not Path(p).exists()],
            'legacy_service':subprocess.run(['systemctl','--user','is-active',SERVICE],capture_output=True,text=True).stdout.strip()}
    report['ready']=not report['missing'];V.write_json(out/'preflight.json',report);return report


def summarize(out,results):
    V.write_json(out/'summary.json',{'rounds':results})
    lines=['# 双窗口预测接入 bin：真实 GUI 效果试验','',
        'Native 为同一 PARP 内核关闭优化；PARP 启用显式 p180 prior 与现有 rank/bin 评分。',
        '动作计划来自独立测试分区；八应用运行集合是明确的系统压力条件，不代表 LSApp 常见输入分布。',
        '每场景初始只做一对，结果是试运行证据；未选出热应用时不能声称验证了预测热保护。','',
        '| 场景 | Native | PARP | Native 可见恢复(s) | PARP 可见恢复(s) |',
        '|---|---|---|---:|---:|']
    for name in dict.fromkeys(r['scenario'] for r in results):
        pair={r['policy']:r for r in results if r['scenario']==name}
        def cell(policy):
            r=pair.get(policy,{})
            probes=r.get('probes',[])
            value=f"{sum(x['rendered_ready_s'] for x in probes)/len(probes):.4f}" if r.get('status')=='PASS' and probes else 'N/A'
            return r.get('status','未运行'),value
        n,nv=cell('native');p,pv=cell('parp');lines.append(f'| {name} | {n} | {p} | {nv} | {pv} |')
    for r in results:
        if r.get('error'):lines.extend(['',f"{r['scenario']} / {r['policy']}: {r['error']}"])
    lines.extend(['','逐应用 refault、major fault、PSI、内存和 swap 变化见各轮 result.json；完整时间序列见 memory-timeline.jsonl。',
        '缺少 cgroup pswpin 计数时记为 null，不使用全系统 swap-in 冒充单应用指标。',
        'R4/R5 必须满足 file_dirty 与容量门禁；R5 PARP 还要求 writepage_promotions 增量大于0。',
        '预测指标使用实际进入事件、严格未来窗口和未知掩码；后半段预测观察不足，不能按全负统计。'])
    (out/'REPORT.md').write_text('\n'.join(lines)+'\n')


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('command',choices=['plan','preflight','gui-check','run'])
    p.add_argument('--config',type=Path,default=CONFIG);p.add_argument('--output-dir',type=Path,required=True)
    p.add_argument('--scenario',choices=['all','r1','r2','r3','r4','r5'],default='all');args=p.parse_args()
    config=json.loads(args.config.read_text())
    if (config['hot_threshold'],config['cold_threshold'],config['kernel_cold_probability_q15'],config['kernel_model_version']) != (.9,.2,COLD_Q15,501):
        p.error('config thresholds/version differ from the explicit kernel projection contract')
    if config['rounds']!=1:p.error('initial paired runner supports one pair per scenario')
    out=args.output_dir.resolve();out.mkdir(parents=True,exist_ok=False)
    config['vocab_names']={a:V.ACCEPT.LSAPP_NAME_BY_APP_KEY[a] for a in config['apps']}
    names=config['scenarios'] if args.scenario=='all' else [args.scenario]
    plans=[make_plan(config,s) for s in names];V.write_json(out/'action-plans.json',plans);V.write_json(out/'config.json',config)
    if args.command=='plan':print(json.dumps({'plans':len(plans),'output':str(out)}));return
    status=preflight(out)
    if args.command=='preflight':print(json.dumps(status));return
    gui_check=args.command=='gui-check'
    if not status['ready'] and not gui_check:raise RuntimeError('PARP kernel interface unavailable; no controls changed')
    V.torch.set_num_threads(1)
    def interrupted(*_):raise KeyboardInterrupt('experiment interrupted')
    def timeout(*_):raise RuntimeError('round exceeded maximum duration')
    signal.signal(signal.SIGTERM,interrupted);signal.signal(signal.SIGINT,interrupted);signal.signal(signal.SIGALRM,timeout)
    running=status['legacy_service']=='active'
    results=[]
    try:
        if running:subprocess.run(['systemctl','--user','stop',SERVICE],check=True)
        with V.desktop('isolated',out):
            for plan in plans:
                reference=None
                for policy in (('native',) if gui_check else ('native','parp')):
                    V.write_json(out/'progress.json',{'state':'running','scenario':plan['scenario'],'policy':policy,'completed':len(results),'total':len(plans)*2})
                    result=run_half(config,plan,policy,out/(plan['scenario']+'-'+policy),reference,gui_check)
                    results.append(result);summarize(out,results)
                    print(json.dumps(result),flush=True)
                    if result['status'] not in {'PASS','GUI_CHECK_PASS'}:
                        break  # Keep INVALID; proceed to the next independent scenario.
                    if policy=='native':reference=result
        invalid=any(r['status']=='INVALID' for r in results)
        V.write_json(out/'progress.json',{'state':'complete_with_invalid' if invalid else 'complete',
            'completed':len(results),'planned_halves':len(plans)*(1 if gui_check else 2),
            'valid_halves':sum(r['status'] in {'PASS','GUI_CHECK_PASS'} for r in results)})
        return 1 if invalid else 0
    except BaseException as exc:
        V.write_json(out/'progress.json',{'state':'interrupted','completed':len(results),'error':str(exc)})
        raise
    finally:
        if running:subprocess.run(['systemctl','--user','start',SERVICE],check=True)


if __name__=='__main__':raise SystemExit(main())
