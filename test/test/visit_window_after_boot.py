#!/usr/bin/env python3
"""Run the authorized, hash-pinned experiment once after the kernel switch."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--manifest',type=Path,required=True)
    args=parser.parse_args();manifest=json.loads(args.manifest.read_text())
    root=args.manifest.parent
    lock=(root/'after-boot.lock').open('w');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    state=root/'boot-status.json'
    def record(status,**extra):
        temporary=state.with_suffix('.tmp')
        temporary.write_text(json.dumps({'state':status,'kernel':os.uname().release,**extra},indent=2)+'\n');temporary.replace(state)
    if os.uname().release!=manifest['target_kernel']:
        record('wrong_kernel',expected=manifest['target_kernel']);return 2
    for name,expected in manifest['source_sha256'].items():
        if hashlib.sha256(Path(name).read_bytes()).hexdigest()!=expected:
            record('source_changed',path=name);return 2
    for _ in range(60):
        if Path('/dev/myfs').exists():break
        time.sleep(1)
    else:record('missing_myfs');return 2
    # uaccess is normally installed by logind. A lingering user unit can start
    # before graphical login; grant only this already-authorized user access
    # for the experiment and restore the original ACL afterwards.
    acl=None;process=None
    try:
        if not os.access('/dev/myfs',os.R_OK|os.W_OK):
            acl=subprocess.check_output(['sudo','-n','getfacl','-p','/dev/myfs'],text=True)
            subprocess.run(['sudo','-n','setfacl','-m',f'u:{os.getuid()}:rw','/dev/myfs'],check=True)
        args.manifest.rename(root/'running-manifest.json')
        def stop(sig,*_):
            if process is not None and process.poll() is None:
                process.send_signal(signal.SIGTERM)
                process.wait(timeout=45)
            raise SystemExit(128+sig)
        signal.signal(signal.SIGTERM,stop);signal.signal(signal.SIGINT,stop)
        record('running',output_dir=manifest['output_dir'])
        with (root/'kernel-test.log').open('w') as log:
            process=subprocess.Popen([sys.executable,manifest['runner'],'run','--output-dir',manifest['output_dir']],
                cwd=manifest['repository'],stdout=log,stderr=subprocess.STDOUT,
                env={**os.environ,'PYTHONUNBUFFERED':'1'})
            code=process.wait()
        progress=Path(manifest['output_dir'])/'progress.json'
        record('finished' if code==0 else 'failed',returncode=code,
               progress=json.loads(progress.read_text()) if progress.exists() else None)
        return code
    finally:
        if acl is not None:
            subprocess.run(['sudo','-n','setfacl','--restore=-'],input=acl,text=True,check=True)


if __name__=='__main__':raise SystemExit(main())
