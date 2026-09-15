#!/usr/bin/env python3
"""Private desktop and per-app cgroups for LSApp-30 GUI acceptance.

Snapshots/actions are evidence, not automatic claims of a successful operation.
No reclaim controls or prediction/kernel writes are used.
"""
import argparse
import dataclasses
import hashlib
import json
import os
from pathlib import Path
import signal
import shutil
import subprocess
import sys
import time

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from PIL import ImageGrab


def unit(out, app):
    return 'parp30-' + hashlib.sha256(str(out).encode()).hexdigest()[:8] + '-' + app.lower() + '.service'


def environment(out):
    env = json.loads((out/'desktop-env.json').read_text())
    os.environ.update(env)
    return env


def snapshot(out, label):
    environment(out)
    reader = V._X11PropertyReader()
    values=[]
    for wid in reader.client_window_ids():
        p=reader.window_properties(wid)
        if not p:continue
        r=dataclasses.asdict(p)
        try:r['cgroup']=Path(f'/proc/{p.pid}/cgroup').read_text().strip()
        except OSError:r['cgroup']=''
        values.append(r)
    result={'timestamp':time.time(),'active_window':reader.active_window_id(),'windows':values}
    reader.close()
    V.write_json(out/(label+'.json'),result)
    ImageGrab.grab(xdisplay=os.environ['DISPLAY']).save(out/(label+'.png'))
    return result


def process_inventory(out, app):
    """Track both cgroup members and descendants/profile users after migration."""
    rows={}
    for p in Path('/proc').iterdir():
        if not p.name.isdigit():continue
        try:
            status=(p/'status').read_text()
            ppid=int(next(s.split()[1] for s in status.splitlines() if s.startswith('PPid:')))
            cg=(p/'cgroup').read_text().strip()
            cmd=(p/'cmdline').read_bytes().replace(b'\0',b' ').decode(errors='replace')
            try:
                env=(p/'environ').read_bytes().split(b'\0')
                profile=('XDG_CONFIG_HOME='+str(out/app/'config')).encode() in env
            except OSError:profile=False
            rows[int(p.name)]={'pid':int(p.name),'ppid':ppid,'cgroup':cg,'command':cmd,'profile_match':profile}
        except (OSError,StopIteration,ValueError):continue
    selected={pid for pid,r in rows.items() if '/'+unit(out,app) in r['cgroup'] or r['profile_match']}
    while True:
        expanded=selected|{pid for pid,r in rows.items() if r['ppid'] in selected}
        if expanded==selected:break
        selected=expanded
    return [rows[pid] for pid in sorted(selected)]


def main():
    global V
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['desktop','start','shot','input','focus','stop','audit','replay','launch-in-bus','private-ca'])
    parser.add_argument('--output-dir',type=Path,required=True)
    parser.add_argument('--app',default='')
    parser.add_argument('--label',default='snapshot')
    parser.add_argument('--trusted-cert',type=Path)
    parser.add_argument('--plan',type=Path)
    parser.add_argument('--uid',type=int)
    parser.add_argument('--gid',type=int)
    argv=sys.argv[1:]
    boundary=argv.index('--') if '--' in argv else len(argv)
    command=argv[boundary+1:]
    ns=parser.parse_args(argv[:boundary])
    out=ns.output_dir.resolve();out.mkdir(parents=True,exist_ok=True)
    if ns.action=='private-ca':
        # Invoked only in a fresh private mount namespace. Never changes host trust.
        assert os.geteuid()==0 and ns.uid is not None and ns.gid is not None
        subprocess.run(['mount','--bind',str(out/'ca-certs'),'/etc/ssl/certs'],check=True)
        env=json.loads((out/'private-ca-env.json').read_text())
        os.setgroups([]);os.setgid(ns.gid);os.setuid(ns.uid)
        os.execvpe(command[0],command,env)
    import visit_window_scenarios as V
    if ns.action=='replay':
        assert ns.plan and ns.app
        plan=json.loads(ns.plan.read_text())
        assert plan['app_key']==ns.app and plan['steps']
        base=[sys.executable,str(Path(__file__).resolve())]
        options=['--output-dir',str(out),'--app',ns.app]
        subprocess.run(base+['focus']+options,check=True)
        if plan.get('required_active_title'):
            focused=json.loads((out/(ns.app+'-after-focus.json')).read_text())
            active=next(w for w in focused['windows'] if w['window_id']==focused['active_window'])
            assert active['net_wm_name']==plan['required_active_title'],'replay initial window mismatch'
        echo_log=out/'xmpp/echo-received.log'
        echo_offset=echo_log.stat().st_size if echo_log.exists() else 0
        for i,step in enumerate(plan['steps']):
            delay=float(step.get('wait_before_s',0))
            if not 0<=delay<=30:raise ValueError('replay wait must be between 0 and 30 seconds')
            time.sleep(delay)
            label='replay-'+str(i)
            subprocess.run(base+['input']+options+['--label',label,'--',*step['xdotool']],check=True)
            expected=step.get('expect_window_title_contains')
            if expected:
                result=json.loads((out/(ns.app+'-after-'+label+'.json')).read_text())
                assert any(expected in w['net_wm_name'] and '/'+unit(out,ns.app) in w['cgroup'] for w in result['windows'])
        if plan.get('expect_local_echo'):
            deadline=time.monotonic()+10
            while time.monotonic()<deadline:
                if echo_log.exists() and plan['expect_local_echo'] in echo_log.read_bytes()[echo_offset:].decode():break
                time.sleep(.2)
            else:raise RuntimeError('local echo service did not receive the replay message')
        print('replay commands completed; inspect action evidence for content assertions')
        return
    if ns.action=='desktop':
        def stop(*_):raise KeyboardInterrupt()
        signal.signal(signal.SIGTERM,stop)
        try:
            with V.desktop('isolated',out):
                V.write_json(out/'desktop-env.json',{k:os.environ[k] for k in
                    ['DISPLAY','XAUTHORITY','GDK_BACKEND','QT_QPA_PLATFORM','XDG_SESSION_TYPE','XDG_CURRENT_DESKTOP'] if k in os.environ})
                while True:time.sleep(1)
        except KeyboardInterrupt:pass
        return
    if ns.action=='launch-in-bus':
        (out/'session-bus.txt').write_text(os.environ['DBUS_SESSION_BUS_ADDRESS'])
        os.execvpe(command[0],command,os.environ)
    elif ns.action=='start':
        assert ns.app and command
        env=environment(out);profile=out/ns.app;profile.mkdir(exist_ok=True)
        if process_inventory(out,ns.app):raise RuntimeError('application is already running')
        # UNIX sockets from an earlier stopped instance must not prevent relaunch.
        if (profile/'runtime').exists():shutil.rmtree(profile/'runtime')
        for name in ['config','data','cache','state','runtime']:
            (profile/name).mkdir(exist_ok=True,mode=0o700)
        env.update(XDG_CONFIG_HOME=str(profile/'config'),XDG_DATA_HOME=str(profile/'data'),
            XDG_CACHE_HOME=str(profile/'cache'),XDG_STATE_HOME=str(profile/'state'),
            XDG_RUNTIME_DIR=str(profile/'runtime'),XDG_CURRENT_DESKTOP='GNOME',
            NO_AT_BRIDGE='1')
        cmd=['systemd-run','--user','--unit='+unit(out,ns.app),'--slice=parp-30-gui.slice',
             '--property=Type=exec','--property=KillMode=control-group','--property=TimeoutStopSec=10','--collect']
        cmd += ['--setenv='+k+'='+v for k,v in env.items()]
        launch=['dbus-run-session',sys.executable,str(Path(__file__).resolve()),'launch-in-bus',
                '--output-dir',str(profile),'--',*command]
        if ns.trusted_cert:
            certs=profile/'ca-certs'
            if not certs.exists():shutil.copytree('/etc/ssl/certs',certs,symlinks=True)
            (certs/'ca-certificates.crt').write_bytes(Path('/etc/ssl/certs/ca-certificates.crt').read_bytes()+b'\n'+ns.trusted_cert.read_bytes())
            (certs/'parp-local.crt').write_bytes(ns.trusted_cert.read_bytes())
            cert_hash=subprocess.check_output(['openssl','x509','-in',str(ns.trusted_cert),'-hash','-noout'],text=True).strip()
            link=certs/(cert_hash+'.0')
            if not link.exists():link.symlink_to('parp-local.crt')
            base={k:v for k,v in os.environ.items() if k in {'PATH','HOME','USER','LOGNAME','LANG','LC_ALL','LC_CTYPE'}}
            (profile/'private-ca-env.json').write_text(json.dumps(dict(base,**env)))
            (profile/'private-ca-env.json').chmod(0o600)
            launch=['sudo','-n','unshare','--mount','--propagation','private',sys.executable,
                str(Path(__file__).resolve()),'private-ca','--output-dir',str(profile),
                '--uid',str(os.getuid()),'--gid',str(os.getgid()),'--',*launch]
        cmd += ['--',*launch]
        subprocess.run(cmd,check=True,capture_output=True,text=True)
        V.write_json(profile/'launch.json',{'unit':unit(out,ns.app),'command':command,'environment':env})
        deadline=time.monotonic()+25
        while time.monotonic()<deadline:
            reader=V._X11PropertyReader()
            owned=False
            for wid in reader.client_window_ids():
                w=reader.window_properties(wid)
                if not w:continue
                try:owned |= ('/'+unit(out,ns.app)) in Path(f'/proc/{w.pid}/cgroup').read_text()
                except OSError:pass
            reader.close()
            if owned:break
            time.sleep(.5)
        time.sleep(1)
        result=snapshot(out,ns.app+'-startup')
        print(json.dumps(result,ensure_ascii=False))
        if not owned:raise RuntimeError('no application window appeared within 25 seconds')
    elif ns.action=='shot':print(json.dumps(snapshot(out,ns.label),ensure_ascii=False))
    elif ns.action=='audit':
        rows=process_inventory(out,ns.app)
        V.write_json(out/ns.app/'process-audit.json',{'processes':rows,
            'outside_original_unit':[r for r in rows if '/'+unit(out,ns.app) not in r['cgroup']]})
        print(json.dumps({'app':ns.app,'processes':len(rows),'outside_original_unit':sum('/'+unit(out,ns.app) not in r['cgroup'] for r in rows)}))
    elif ns.action=='focus':
        before=snapshot(out,ns.app+'-before-focus')
        owned=[w for w in before['windows'] if '/'+unit(out,ns.app) in w['cgroup'] and w['is_normal_window']]
        if not owned:raise RuntimeError('no owned content window')
        subprocess.run(['xdotool','windowactivate','--sync',owned[-1]['window_id']],check=True)
        after=snapshot(out,ns.app+'-after-focus')
        assert after['active_window'] in {w['window_id'] for w in owned}
        print(json.dumps(after,ensure_ascii=False))
    elif ns.action=='input':
        assert ns.app and command
        environment(out)
        before=snapshot(out,ns.app+'-before-'+ns.label)
        active=next((w for w in before['windows'] if w['window_id']==before['active_window']),None)
        if not active or ('/'+unit(out,ns.app)) not in active['cgroup']:
            raise RuntimeError('active window is not owned by requested application')
        subprocess.run(['xdotool',*command],check=True)
        time.sleep(1)
        after=snapshot(out,ns.app+'-after-'+ns.label)
        with (out/ns.app/'actions.jsonl').open('a') as f:
            f.write(json.dumps({'at':time.time(),'command':command,'before':before,'after':after})+'\n')
        print(json.dumps(after,ensure_ascii=False))
    elif ns.action=='stop':
        assert ns.app
        before=process_inventory(out,ns.app)
        subprocess.run(['systemctl','--user','stop',unit(out,ns.app)],check=True)
        remaining=process_inventory(out,ns.app)
        V.write_json(out/ns.app/'cleanup.json',{'before_pids':[r['pid'] for r in before],
            'remaining':remaining,'passed':not remaining})
        if remaining:raise RuntimeError('application processes survived cleanup; inspect cleanup.json')


if __name__=='__main__':main()
