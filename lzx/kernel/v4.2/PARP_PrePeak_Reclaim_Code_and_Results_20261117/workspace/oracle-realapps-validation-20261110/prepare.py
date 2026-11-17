"""Private native application resources; no sudo or global knob changes."""
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import time
import uuid

HERE=Path(__file__).resolve().parent


def save(p,d):p.write_text(json.dumps(d,ensure_ascii=False,indent=1)+'\n',encoding='utf-8')
def identity(pid):
    s=Path(f'/proc/{pid}/stat').read_text()
    return dict(pid=pid,starttime=s[s.rfind(')')+2:].split()[19])


def display(root,name):
    d=root/f'display-{name}';d.mkdir(mode=0o700)
    n=next(i for i in range(90,130) if not Path(f'/tmp/.X11-unix/X{i}').exists() and not Path(f'/tmp/.X{i}-lock').exists())
    address=f':{n}';auth=d/'Xauthority';auth.touch(mode=0o600)
    subprocess.run(['xauth','-f',str(auth),'add',address,'.',secrets.token_hex(16)],check=True,capture_output=True)
    with (d/'xephyr.log').open('wb') as log:
        server=subprocess.Popen(['Xephyr',address,'-screen','1600x850','-nolisten','tcp','-auth',str(auth)],
                                stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
    env=os.environ.copy();env.update(DISPLAY=address,XAUTHORITY=str(auth),XDG_SESSION_TYPE='x11',QT_QPA_PLATFORM='xcb')
    end=time.monotonic()+15
    while subprocess.run(['xdpyinfo'],env=env,capture_output=True).returncode:
        if server.poll() is not None or time.monotonic()>end:raise RuntimeError('private display startup failed')
        time.sleep(.2)
    with (d/'openbox.log').open('wb') as log:
        manager=subprocess.Popen(['openbox','--sm-disable'],env=env,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
    time.sleep(.5)
    if manager.poll() is not None:raise RuntimeError('private window manager startup failed')
    out=dict(server=identity(server.pid),manager=identity(manager.pid),
             env={k:env[k] for k in ('DISPLAY','XAUTHORITY','XDG_SESSION_TYPE','QT_QPA_PLATFORM')})
    save(d/'context.json',out);return out


def main():
    if (HERE/'runtime.json').exists():raise RuntimeError('do not replace existing application setup')
    if os.getuid()==0:raise RuntimeError('ordinary desktop user required')
    if Path('/sys/module/zswap/parameters/enabled').read_text().strip()!='N':raise RuntimeError('keep the same existing disk-swap setup')
    master=Path('/proc/sys/vm/parp_operations_enabled')
    if master.exists() and master.read_text().strip()!='0':raise RuntimeError('custom kernel proactive reclaim must remain disabled')
    root=Path.home()/f'oracle-native-apps-{uuid.uuid4().hex[:8]}';root.mkdir(mode=0o700)
    assets=root/'assets';assets.mkdir()
    source=Path.home()/'krita-userspace-realapps-20261105/assets'
    for name in ('formal_4096_48layers.ora','functional_4096_4layers.ora','peak_4096_24layers.ora'):
        shutil.copy2(source/name,assets/name)
    template=Path.home()/'krita-userspace-peak-20261103/profile-template'
    if not template.is_dir():raise RuntimeError('initialized Krita template missing')
    parent=subprocess.check_output(['systemctl','--user','show','-p','ControlGroup','--value'],text=True).strip()
    data=dict(root=str(root),assets=str(assets),template=str(template),
        parent=str(Path('/sys/fs/cgroup')/parent.lstrip('/')),kernel=os.uname().release,
        boot_id=Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
        displays={n:display(root,n) for n in ('front','peak')},zswap='N')
    save(HERE/'runtime.json',data);print('NATIVE_APP_SETUP_COMPLETE',flush=True)


if __name__=='__main__':main()
