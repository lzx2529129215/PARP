"""Run isolated AgentNet spreadsheet tasks with fixed-window Referenced sampling."""
import argparse
import csv
import io
import json
import os
from pathlib import Path
import sys
import time

ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(ROOT),'/home/lzx/Desktop/wss-ebpf-lightgbm']
from automation.wps_replay import EngineBackend,execute_semantic,guard,workbook_bytes
from automation.scripts.build_agentnet_replay_batch import TASKS,PROFILE
from operation_runtime import Session,Adapter
from operation_scenarios import UI
from wps_user_sessions import WindowSampler


def run(relative,selection,window=3):
    if not 0 < window < float('inf'):raise ValueError('Window must be positive and finite')
    pre_idle=max(9,window+5);post_idle=max(12,2*window+5)
    g=guard();g.check();out=g.path(relative)
    if out.exists():raise FileExistsError(out)
    g.write_json(relative+'/plan.json',dict(tasks=selection,window_s=window,pre_idle_s=pre_idle,post_idle_s=post_idle,
        scope='WPS cgroup; sum process Referenced; no shared-page deduplication',
        origin='synthetic inputs preserving AgentNet task semantics; not original files'))
    results=[]
    for original in TASKS:
        prefix=original['task_id'].split('_')[0]
        if selection and prefix not in selection:continue
        taskrel=relative+'/'+prefix;folder=g.path(taskrel);spec=dict(original)
        if spec['operation']=='sort_ascending':
            spec.update(headers=['row_id','payload','key_c','key_d'],rows=[['r1','alpha',30,200],['r2','bravo',10,400],['r3','charlie',40,100],['r4','delta',20,300]])
        path=folder/(prefix+'.xlsx')
        with g.open(taskrel+'/'+path.name) as f:f.write(workbook_bytes(spec))
        s=a=sampler=None;events=[];error='';audit={};started=time.monotonic_ns()
        try:
            print(prefix,'launch',flush=True)
            s=Session(folder/'runtime',command=['/opt/kingsoft/wps-office/office6/et',str(path)])
            a=Adapter(s);ui=UI(s,a,{})
            s.wait_window(path.name,45);s.wait_editor(path.name);s.focus(path.name);s.dismiss();s.focus_editor(path.name)
            original_call=s.call
            def protected_call(command,**kwargs):
                if command=='begin':g.check(64*1024*1024)
                return original_call(command,**kwargs)
            s.call=protected_call
            sampler=WindowSampler(s,folder,window);sampler.start()
            def record(event,task_id=''):
                events.append(dict(event=event,monotonic_ns=time.monotonic_ns(),unix_s=time.time()))
                print(prefix,event,flush=True)
            class Backend(EngineBackend):
                def ready(self):
                    # Delayed default-app reminders can appear during long idle.
                    s.dismiss()
                    self.wid=str(s.wait_editor(path.name));s.focus(path.name);s.focus_editor(path.name)
                def key(self,key):ui.editor_key(key)
                def select(self,ref):ui.cell(ref)
                def event(self,event,task_id):record(event,task_id)
                def save(self):
                    record('AUDIT_SAVE_START');super().save();record('AUDIT_SAVE_END')
            backend=Backend(a.engine,dict(path=str(path),profile=PROFILE,name='wps',app_key='WPS'),a.ctx)
            record('PRE_IDLE_START');time.sleep(pre_idle);record('TASK_PREPARATION_START')
            audit=execute_semantic(spec,backend,path)
            record('POST_IDLE_START');time.sleep(post_idle);record('POST_IDLE_END')
        except Exception as exc:
            error=repr(exc);print(prefix,'FAILED',error,flush=True)
            events.append(dict(event='FAILED',monotonic_ns=time.monotonic_ns(),error=error))
            if s:
                try:
                    s.capture('failed.png')
                    (folder/'visible-text.txt').write_text(ui.text())
                    (folder/'windows.txt').write_text(s.windows())
                except Exception as capture_error:print('diagnostic error',repr(capture_error),flush=True)
        finally:
            if sampler:
                try:sampler.finish()
                except Exception as exc:error+='; sampler '+repr(exc)
            if a:a.close()
            if s:s.close()
        g.write_json(taskrel+'/events.json',events)
        rows=sampler.rows if sampler else []
        flat=[]
        if rows:
            zero=rows[0]['begin_ns']
            for r in rows:
                flat.append(dict(start_s=(r['begin_ns']-zero)/1e9,end_s=(r['end_ns']-zero)/1e9,duration_s=r['duration_s'],
                    wss_mib=None if r['referenced_bytes'] is None else r['referenced_bytes']/2**20,
                    rss_mib=None if r['rss_bytes'] is None else r['rss_bytes']/2**20,
                    coverage_complete=r['coverage_complete'],quality_flags=';'.join(r['quality_flags']),
                    final_short_window=r['final_short_window'],collection_gap_s=r['collection_gap_s']))
            buf=io.StringIO();writer=csv.DictWriter(buf,fieldnames=list(flat[0]));writer.writeheader();writer.writerows(flat)
            with g.open(taskrel+'/timeline.csv') as f:f.write(buf.getvalue().encode())
        result=dict(task_id=original['task_id'],status='FAILED' if error else 'GUI_AUDIT_PASS',error=error,audit=audit,
            window_count=len(rows),complete_windows=sum(r['coverage_complete'] for r in rows),
            peak_wss_mib=max((r['wss_mib'] for r in flat if r['wss_mib'] is not None),default=None),
            elapsed_s=(time.monotonic_ns()-started)/1e9)
        g.write_json(taskrel+'/result.json',result);results.append(result);print(json.dumps(result),flush=True)
    g.write_json(relative+'/results.json',results)
    return results


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',required=True);p.add_argument('--tasks',nargs='*',default=[]);p.add_argument('--window',type=float,default=3)
    args=p.parse_args();run(args.output,args.tasks,args.window)
