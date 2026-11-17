#!/usr/bin/env python3
"""Enter only the dedicated test leaf before exec; no memory policy changes."""
import os
import json
from pathlib import Path
import sys

if len(sys.argv)<4 or sys.argv[1] not in ("foreground","background","pressure") or sys.argv[2]!="--":
    raise SystemExit("cg_exec.py foreground|background|pressure -- COMMAND ...")
config=Path(os.environ.get('KRITA_LAB_CGROUP_CONFIG',str(Path(__file__).resolve().parent/"runtime-cgroup.json")))
root=Path(json.loads(config.read_text())["base"]).resolve(strict=True)
base=Path(os.environ.get('KRITA_LAB_CGROUP_BASE',str(root))).resolve(strict=True)
if base!=root and root not in base.parents:
    raise SystemExit('test cgroup override must stay below the delegated test root')
leaf=base/sys.argv[1]
(leaf/"cgroup.procs").write_text(str(os.getpid()))
os.execvpe(sys.argv[3],sys.argv[3:],os.environ.copy())
