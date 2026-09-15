"""Run built long scenarios on an isolated desktop; default is offline dry-run."""
import argparse
import json
import os
from pathlib import Path
import select
import shutil
import subprocess
import sys
import tempfile
import uuid

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from automation import app_automation as engine
from automation.wps_long_scenarios import guard


def run(batch,selected=(),actual=False):
    g=guard();batch=g.path(str(Path(batch).absolute().relative_to(g.root)))
    entries=json.loads((batch/'batch.json').read_text())['tasks']
    if set(selected)-{e['kind'] for e in entries}:raise ValueError('Unknown task selection')
    results=[]
    for entry in entries:
        if selected and entry['kind'] not in selected:continue
        scenario=engine.load_scenario(Path(entry['scenario']))
        if not actual:
            ctx=engine.Context(dry_run=True)
            for action in scenario.actions:engine.ACTION_HANDLERS[action['type']](action,ctx)
            results.append(dict(kind=entry['kind'],status='DRY_RUN_PASS'));continue
        for tool in ('Xvfb','openbox','xdotool','wmctrl','xclip','tesseract','import','firefox','pdftotext'):
            if not shutil.which(tool):raise RuntimeError('Missing runtime tool: '+tool)
        folder=Path(entry['scenario']).parent
        if (folder/'replay_result.json').exists():raise FileExistsError('Use a fresh build')
        previous=os.environ.copy();processes=[];logs=[];alias=None;ctx=None
        try:
            g.check(64*1024*1024)
            config=g.path('.tmp/long-'+uuid.uuid4().hex[:10]);config.mkdir(parents=True)
            # Short paths for WPS IPC; symlink itself stays outside DiskGuard root.
            alias=Path(tempfile.mkdtemp(prefix='wps-long-'));(alias/'xdg').symlink_to(config,target_is_directory=True)
            for key,sub in [('XDG_CONFIG_HOME','config'),('XDG_CACHE_HOME','cache'),('XDG_DATA_HOME','data')]:
                (config/sub).mkdir();os.environ[key]=str(alias/'xdg'/sub)
            os.environ.update(LANG='en_US.UTF-8',LC_ALL='en_US.UTF-8',QT_AUTO_SCREEN_SCALE_FACTOR='0',QT_SCALE_FACTOR='1',OMP_THREAD_LIMIT='1')
            g.check();log=(folder/'desktop.log').open('xb');logs.append(log)
            xvfb=subprocess.Popen(['Xvfb','-displayfd','1','-screen','0','1920x1080x24','-nolisten','tcp'],stdout=subprocess.PIPE,stderr=log)
            processes.append(xvfb)
            if not select.select([xvfb.stdout],[],[],10)[0]:raise RuntimeError('Xvfb startup timeout')
            display=xvfb.stdout.readline().decode().strip()
            if not display.isdecimal():raise RuntimeError('Xvfb failed')
            os.environ['DISPLAY']=':'+display
            processes.append(subprocess.Popen(['openbox'],stdout=log,stderr=log))
            trace=engine.TraceWriter(str(folder/'trace.csv'),uuid.uuid4().hex,entry['kind'])
            ctx=engine.Context(dry_run=False,trace=trace,test_slice='huawei-test.slice')
            for action in scenario.actions:engine.ACTION_HANDLERS[action['type']](action,ctx)
            results.append(dict(kind=entry['kind'],status='GUI_AUDIT_PASS'))
        except Exception as exc:
            results.append(dict(kind=entry['kind'],status='FAILED',error=repr(exc)))
        finally:
            if ctx:
                engine.cleanup_tracked_processes(ctx);engine._stop_clipboard_process(ctx)
                if ctx.trace:ctx.trace.close()
            for proc in reversed(processes):
                if proc.poll() is None:
                    proc.terminate()
                    try:proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:proc.kill();proc.wait()
            for log in logs:log.close()
            if alias:
                (alias/'xdg').unlink(missing_ok=True);alias.rmdir()
            os.environ.clear();os.environ.update(previous)
    print(json.dumps(results,ensure_ascii=False,indent=2))
    return results


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--batch',required=True)
    p.add_argument('--tasks',nargs='*',default=[]);p.add_argument('--run',action='store_true')
    a=p.parse_args();result=run(a.batch,a.tasks,a.run)
    raise SystemExit(1 if any(r['status']=='FAILED' for r in result) else 0)
