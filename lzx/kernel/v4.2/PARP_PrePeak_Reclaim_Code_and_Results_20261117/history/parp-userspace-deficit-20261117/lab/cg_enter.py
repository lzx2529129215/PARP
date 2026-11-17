"""Enter one explicitly recorded leaf belonging to this test, then exec."""
import json
import os
from pathlib import Path
import sys

if len(sys.argv) < 5 or sys.argv[3] != '--':
    raise SystemExit('cg_enter.py CONFIG LEAF -- COMMAND ...')
config = json.loads(Path(sys.argv[1]).read_text())
base = Path(config['base']).resolve(strict=True)
parent = Path(config['parent']).resolve(strict=True)
if base.parent != parent or not base.name.startswith('parp-measured-'):
    raise SystemExit('unexpected test root')
leaf_name = sys.argv[2]
if leaf_name not in config['leaves'] or Path(leaf_name).name != leaf_name:
    raise SystemExit('unrecorded test leaf')
leaf = (base/leaf_name).resolve(strict=True)
if leaf.parent != base:
    raise SystemExit('leaf escaped test root')
(leaf/'cgroup.procs').write_text(str(os.getpid()))
os.execvpe(sys.argv[4], sys.argv[4:], os.environ.copy())
