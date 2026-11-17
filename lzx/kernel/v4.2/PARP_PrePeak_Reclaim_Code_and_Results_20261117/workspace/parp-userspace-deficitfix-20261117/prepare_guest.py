"""Prepare a local copy of the passive collector. Does not start privileged code."""
import hashlib
import json
import os
from pathlib import Path
import shutil
ROOT=Path(__file__).resolve().parent
if os.getuid()!=1000:raise RuntimeError('run as qi, not sudo')
bridge=Path.home()/'parp-measured-lab-20261113'
status=bridge/'guard_status.json'
if status.exists() and json.loads(status.read_text()).get('status')=='ACTIVE':raise RuntimeError('previous guard still active')
target=Path.home()/ROOT.name;target.mkdir(mode=0o700,exist_ok=True)
if target.is_symlink() or target.stat().st_uid!=1000:raise RuntimeError('unexpected helper directory')
source=ROOT/'lab/root_guard.py';code=source.read_bytes();compile(code,str(source),'exec')
output=target/'root_guard.py';shutil.copyfile(source,output)
digest=hashlib.sha256(code).hexdigest()
if hashlib.sha256(output.read_bytes()).hexdigest()!=digest:raise RuntimeError('local helper differs')
if (bridge/'STOP').exists():(bridge/'STOP').unlink()
(target/'prepared.json').write_text(json.dumps(dict(root_guard_sha256=digest)))
print('ALIGNED_HELPER_PREPARED',str(output),digest)
