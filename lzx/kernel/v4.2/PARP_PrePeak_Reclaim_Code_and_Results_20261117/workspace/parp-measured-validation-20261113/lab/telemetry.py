"""Ordinary-user client of the scoped root collector; no privileged execution."""
import json
from pathlib import Path
import time

BRIDGE=Path.home()/'parp-measured-lab-20261113'


def save(path,row):
    temporary=path.with_suffix('.tmp');temporary.write_text(json.dumps(row));temporary.replace(path)


def active():
    p=BRIDGE/'guard_status.json'
    if not p.exists():return False
    return json.loads(p.read_text()).get('status')=='ACTIVE'


def begin(run_id,base,groups,*,wss=False):
    if not active():raise RuntimeError('compression/WSS guard is not active')
    row=dict(active=True,run_id=run_id,base=str(base),groups={app:str(base/leaf) for app,leaf in groups.items()},
             wss=wss,interval_s=1.0)
    save(BRIDGE/'binding.json',row)
    deadline=time.monotonic()+15
    while time.monotonic()<deadline:
        error=BRIDGE/'collector_error.json'
        if error.exists():
            report=json.loads(error.read_text())
            if report.get('run_id')==run_id:
                save(BRIDGE/'binding.json',dict(active=False,run_id=run_id))
                raise RuntimeError('collector incomplete: '+report['error'])
        ready=BRIDGE/'ready.json'
        if ready.exists():
            report=json.loads(ready.read_text())
            if report.get('run_id')==run_id:return report
        time.sleep(.05)
    raise TimeoutError('real WSS/RSS collector readiness')


def end(run_id,directory):
    path=BRIDGE/'binding.json'
    if path.exists() and json.loads(path.read_text()).get('run_id')==run_id:
        save(path,dict(active=False,run_id=run_id))
        time.sleep(.2)
    source=BRIDGE/('telemetry-'+run_id+'.jsonl')
    if not source.exists():raise RuntimeError('real telemetry missing')
    data=source.read_text();(directory/'root-telemetry.jsonl').write_text(data)
    rows=[json.loads(line) for line in data.splitlines()]
    if not rows or any(not app['valid'] for row in rows for app in row['apps'].values()):
        raise RuntimeError('incomplete measured app coverage')
    return rows
