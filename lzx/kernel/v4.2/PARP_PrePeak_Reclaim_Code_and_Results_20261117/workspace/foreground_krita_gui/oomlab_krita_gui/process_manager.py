import os
import hashlib
import json
import logging
import subprocess
import time
from pathlib import Path
from .config import save,SafetyError

QT_PATH_KEYS=('QT_QPA_PLATFORM_PLUGIN_PATH','QT_PLUGIN_PATH','QT_QPA_FONTDIR')
QT_KEYS=QT_PATH_KEYS+('QT_QPA_PLATFORM',)
SESSION_KEYS=('DISPLAY','XAUTHORITY','XDG_RUNTIME_DIR','DBUS_SESSION_BUS_ADDRESS')


def qt_environment(source=None):
    """Copy at the subprocess boundary: importing cv2 may mutate the parent env.

    Never install an alternative Qt search path or mutate os.environ. Audit only
    presence and value digests; paths and session addresses are not logged.
    """
    source=dict(os.environ if source is None else source)
    env=source.copy()
    for key in QT_PATH_KEYS: env.pop(key,None)
    def snapshot(values):
        return {key:dict(present=key in values,
                         value_sha256=hashlib.sha256(values[key].encode()).hexdigest() if key in values else None)
                for key in QT_KEYS}
    audit=dict(policy='native-krita-qt-x11-v1',removed_keys=[k for k in QT_PATH_KEYS if k in source],
               before=snapshot(source),after=snapshot(env),
               session_unchanged={k:env.get(k)==source.get(k) for k in SESSION_KEYS},
               platform_allowed=env.get('QT_QPA_PLATFORM') in (None,'','xcb'))
    return env,audit


def require_x11_platform(env):
    # Reject fallback lists (e.g. xcb;offscreen), Wayland, EGL and unknown values
    # too. An absent/empty value keeps native Qt's desktop default selection.
    if env.get('QT_QPA_PLATFORM') not in (None,'','xcb'):
        raise SafetyError('X11 实验只允许 QT_QPA_PLATFORM 未设置、空值或 xcb；拒绝其他 Qt 后端')


def identity(pid):
    p=Path('/proc')/str(pid); raw=(p/'stat').read_text(); fields=raw[raw.rindex(')')+2:].split()
    return dict(pid=int(pid),ppid=int(fields[1]),starttime=int(fields[19]),state=fields[0],
                exe=os.readlink(p/'exe'))


def existing():
    found=[]
    for p in Path('/proc').iterdir():
        if p.name.isdigit():
            try:
                if (p/'comm').read_text().strip().lower()=='krita': found.append(identity(int(p.name)))
            except (FileNotFoundError,PermissionError,ProcessLookupError): pass
    return found


class Process:
    def __init__(self,run): self.run=run; self.child=None; self.info=None; self.known={}

    def track(self):
        rows={}
        for p in Path('/proc').iterdir():
            if p.name.isdigit():
                try: rows[int(p.name)]=identity(int(p.name))
                except (FileNotFoundError,PermissionError,ProcessLookupError): continue
        selected={self.info['pid']}|set(self.known)
        while True:
            new=selected|{pid for pid,row in rows.items() if row['ppid'] in selected}
            if new==selected: break
            selected=new
        live=[]
        for pid in selected:
            row=rows.get(pid)
            if row and (pid not in self.known or self.known[pid]==row['starttime']):
                self.known[pid]=row['starttime']; live.append(row)
        save(self.run/'known-processes.json',live)
        return live

    def launch(self):
        save(self.run/'existing-processes.json',existing())
        env,audit=qt_environment()
        save(self.run/'qt-environment.json',audit)
        logging.info('Krita child Qt environment audit: %s',json.dumps(audit,sort_keys=True))
        require_x11_platform(env)
        for key,d in [('HOME','private-home'),('XDG_CONFIG_HOME','private-config'),('XDG_DATA_HOME','private-data'),('XDG_CACHE_HOME','private-cache')]:
            p=self.run/d; p.mkdir(mode=0o700); env[key]=str(p)
        if not env.get('XAUTHORITY') and (Path.home()/'.Xauthority').is_file(): env['XAUTHORITY']=str(Path.home()/'.Xauthority')
        env.pop('OOM_KRITA_RUN',None); env.pop('PYTHONPATH',None); env.pop('PYTHONHOME',None)
        # Source-reviewed 5.2.2 singleton key derives from HOME; no unsupported flag,
        # no file arguments, no plugin installation. Actual window proof still required.
        self.log=open(self.run/'krita.log','xb')
        self.child=subprocess.Popen(['krita'],env=env,stdout=self.log,stderr=subprocess.STDOUT,start_new_session=True)
        self.info=identity(self.child.pid); save(self.run/'process.json',self.info)
        return self.info

    def verify(self):
        if not self.child or self.child.poll() is not None: raise SafetyError('本轮 Krita 已退出或请求被转发')
        actual=identity(self.info['pid'])
        if actual['starttime']!=self.info['starttime'] or actual['ppid']!=os.getpid() or actual['state']=='Z': raise SafetyError('PID 身份或父进程不匹配')
        if Path(actual['exe']).name.lower()!='krita': raise SafetyError('实际进程不是原生 Krita，拒绝包装器或代理实例')
        self.track()
        return actual

    def finish(self):
        code=self.child.poll()
        if code is None: raise RuntimeError('Krita 尚未退出')
        if code!=0: raise RuntimeError(f'Krita 异常退出：{code}')
        if self.track(): raise RuntimeError('本轮已知进程仍有残留，保留现场不强杀')
        return dict(returncode=code,exit_wall_ns=time.time_ns(),pid=self.info['pid'])
