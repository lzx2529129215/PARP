"""Ordinary-user client of the scoped root collector; no privileged execution."""
import json
import hashlib
from pathlib import Path
import time

BRIDGE=Path.home()/'parp-measured-lab-20261113'


def save(path,row):
    temporary=path.with_suffix('.tmp');temporary.write_text(json.dumps(row));temporary.replace(path)


def active():
    p=BRIDGE/'guard_status.json'
    if not p.exists():return False
    return json.loads(p.read_text()).get('status')=='ACTIVE'


def expected_sampling(wss=False):
    source=Path(__file__).with_name('root_guard.py')
    return dict(schema=1,source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),interval_s=1.0,
        mode='reference_clear_refs' if wss else 'passive_smaps',clear_refs=wss)


def require_active():
    path=BRIDGE/'guard_status.json'
    status=json.loads(path.read_text()) if path.exists() else {}
    if status.get('status')!='ACTIVE' or status.get('source_sha256')!=expected_sampling()['source_sha256']:
        raise RuntimeError('matching same-service collector must be active before loading any workloads')


def begin(run_id,base,groups,*,wss=False):
    require_active()
    row=dict(active=True,run_id=run_id,base=str(base),groups={app:str(base/leaf) for app,leaf in groups.items()},
             wss=wss,interval_s=1.0,sampling_contract=expected_sampling(wss))
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
            if report.get('run_id')==run_id:
                if report.get('sampling_contract')!=row['sampling_contract']:raise RuntimeError('collector readiness contract differs')
                return report
        time.sleep(.05)
    raise TimeoutError('real WSS/RSS collector readiness')


def end(run_id,directory):
    path=BRIDGE/'binding.json'
    expected=None
    if path.exists() and json.loads(path.read_text()).get('run_id')==run_id:
        expected=json.loads(path.read_text()).get('sampling_contract')
        save(path,dict(active=False,run_id=run_id))
        time.sleep(.2)
    source=BRIDGE/('telemetry-'+run_id+'.jsonl')
    if not source.exists():raise RuntimeError('real telemetry missing')
    data=source.read_text();(directory/'root-telemetry.jsonl').write_text(data)
    rows=[json.loads(line) for line in data.splitlines()]
    if not rows or any(not app['valid'] for row in rows for app in row['apps'].values()):
        raise RuntimeError('incomplete measured app coverage')
    if expected is None or any(row.get('sampling_contract')!=expected for row in rows):
        raise RuntimeError('sampling changed during the trial')
    return rows
