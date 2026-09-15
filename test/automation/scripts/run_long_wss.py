"""Replay adapted long tasks using owned WPS scope and synchronous window sampler."""
import argparse
import json
import os
from pathlib import Path
import sys
import time

sys.path[:0]=[str(Path(__file__).resolve().parents[2]),'/home/lzx/Desktop/wss-ebpf-lightgbm']
from automation.wps_long_scenarios import build,guard,Desktop
from operation_runtime import Session,Adapter
from operation_scenarios import UI
from wps_user_sessions import WindowSampler


def run(output,tasks,pilot=False,hold=0):
    entries=build(output);g=guard();results=[]
    for entry in entries:
        if tasks and entry['kind'] not in tasks:continue
        folder=Path(entry['scenario']).parent;spec=json.loads((folder/'spec.json').read_text())
        s=a=sampler=backend=None;events=[];error='';audit={};previous=os.environ.copy()
        try:
            os.environ['OMP_THREAD_LIMIT']='1'
            s=Session(folder/'runtime',command=['/opt/kingsoft/wps-office/office6/wpsoffice','/prometheus',spec['document']],isolated_preferences=True)
            a=Adapter(s);ui=UI(s,a,{})
            s.wait_window(Path(spec['document']).name,45);s.wait_editor(Path(spec['document']).name);s.dismiss()
            call=s.call
            def protected(command,**kw):
                if command=='begin':g.check(64*1024*1024)
                return call(command,**kw)
            s.call=protected
            class Backend(Desktop):
                def bind(self):
                    s.dismiss();s.focus(self.path.name);self.wid=str(s.wait_editor(self.path.name))
                def key(self,key):ui.editor_key(key)
                def select(self,ref):ui.cell(ref)
                def perform(self,step):
                    if step['op']=='open_document':self.bind();return
                    if step['op']=='close_apps':return
                    return super().perform(step)
            backend=Backend(a.engine,a.ctx,spec)
            if not pilot:
                sampler=WindowSampler(s,folder,30);sampler.start()
            def phase(name,action):
                row=dict(event=name,start_ns=time.monotonic_ns(),status='RUNNING');events.append(row)
                print(entry['kind'],name,flush=True)
                action();row.update(end_ns=time.monotonic_ns(),status='PASS')
            phase('PRE_IDLE',lambda:time.sleep(0 if pilot else 35))
            for step in spec['steps']:
                if step['op']=='close_apps':continue
                phase(step['id']+':'+step['op'],lambda step=step:backend.perform(step))
                if pilot:g.check(4*1024*1024);s.capture(step['id']+'.png')
            audit=backend.audit
            phase('POST_IDLE',lambda:time.sleep(0 if pilot else 65))
        except Exception as exc:
            error=repr(exc)
            if events:events[-1].update(end_ns=time.monotonic_ns(),status='FAILED',error=error)
            print('FAILED',error,flush=True)
            if s:
                try:s.capture('failed.png')
                except Exception as capture_error:print('capture failed',capture_error,flush=True)
                print('DEBUG DISPLAY',s.env['DISPLAY'],flush=True)
                if hold:time.sleep(hold)
        finally:
            if backend is not None:audit=backend.audit
            if sampler:
                try:sampler.finish()
                except Exception as exc:error+=' sampler:'+repr(exc)
            if a:
                a.engine.cleanup_tracked_processes(a.ctx);a.close()
            if s:s.close()
            os.environ.clear();os.environ.update(previous)
        rel=str(folder.relative_to(g.root));g.write_json(rel+'/events.json',events)
        result=dict(kind=entry['kind'],status='FAILED' if error else 'GUI_AUDIT_PASS',error=error,audits=audit,
            pilot=pilot,window_s=None if pilot else 30,window_count=len(sampler.rows) if sampler else 0,
            wss_collected=bool(sampler and sampler.rows),
            scope='WPS process sum only; browser/file manager excluded; document launch precedes sampling',
            close_outside_sampling=True)
        g.write_json(rel+'/result.json',result);results.append(result)
    g.write_json(output+'/run_results.json',results)
    print(json.dumps(results,ensure_ascii=False),flush=True)
    return results


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',required=True);p.add_argument('--tasks',nargs='*',default=[])
    p.add_argument('--pilot',action='store_true');p.add_argument('--hold',type=int,default=0)
    a=p.parse_args();results=run(a.output,a.tasks,a.pilot,a.hold)
    sys.exit(0 if results and all(r['status']=='GUI_AUDIT_PASS' for r in results) else 1)
