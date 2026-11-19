"""Ordinary-user retry channel. Privileged parent only waits and cleans its tree."""
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parent


def save(path,value):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(value,indent=2)+"\n"); tmp.replace(path)


def main():
    if os.getuid() == 0:
        raise RuntimeError("ordinary-user channel required")
    shared = Path(json.loads((ROOT/"bridge.json").read_text())["shared_root"]).resolve()
    control = shared/"control"; control.mkdir(exist_ok=True)
    results = ROOT/"results"; results.mkdir(exist_ok=True)
    seen = {p.name for p in control.glob("run-*.json")}
    first, expires = True, time.monotonic()+3600
    while time.monotonic() < expires:
        if (control/"STOP.json").exists():
            break
        requests = [p for p in sorted(control.glob("run-*.json")) if p.name not in seen]
        if not first and not requests:
            time.sleep(.25); continue
        if first:
            source, token = ROOT, "initial"
            first = False
        else:
            request = requests[0]; seen.add(request.name)
            token = uuid.uuid4().hex[:8]
            source = ROOT/"revisions"/token
            shutil.copytree(shared,source,ignore=shutil.ignore_patterns("results","control","__pycache__","revisions"))
        save(results/"SERVICE.json",{"status":"RUNNING","revision":token,"uid":os.getuid()})
        env = dict(os.environ,PARP_RESULTS_ROOT=str(results),PARP_DELEGATION_FILE=str(results/"delegation.json"))
        process = subprocess.Popen([sys.executable,"-B","-u",str(source/"run_native.py")],
                                   cwd=source,env=env,start_new_session=True)
        while process.poll() is None:
            if (control/"STOP.json").exists():
                os.killpg(process.pid,signal.SIGTERM)
                process.wait(timeout=30)
                break
            time.sleep(.25)
        save(results/"SERVICE.json",{"status":"WAITING","last_exit_code":process.returncode,
                                     "revision":token,"uid":os.getuid()})
        print("ORDINARY_USER_CHANNEL_WAITING: results preserved; corrections can run without another sudo setup",flush=True)
    save(results/"SERVICE.json",{"status":"STOPPED","uid":os.getuid()})


if __name__ == "__main__":
    main()
