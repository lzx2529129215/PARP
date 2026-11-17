"""Counter snapshots for a non-migrating app process tree, including reaped children.

memory.stat remains recorded separately: small event deltas can be unflushed.
Snapshot PID membership is checked twice; changing membership is retried, never
silently interpreted as zero faults. The scope must contain the entire tree.
"""
from pathlib import Path
import time


def read_stat(path):
    # comm can contain spaces and parentheses; fields after its final ')' are fixed.
    data=path.read_text().rsplit(')',1)[1].split()
    return dict(ppid=int(data[1]),starttime=int(data[19]),minor=int(data[7]),
                children_minor=int(data[8]),major=int(data[9]),children_major=int(data[10]))


def total(rows):
    return dict(pgfault=sum(r['minor']+r['major']+r['children_minor']+r['children_major'] for r in rows.values()),
                pgmajfault=sum(r['major']+r['children_major'] for r in rows.values()))


def snapshot(group,proc=Path('/proc'),retries=12):
    for attempt in range(retries):
        before=set((group/'cgroup.procs').read_text().split())
        if not before:raise RuntimeError('no live app processes to sample')
        try:
            rows={p:read_stat(proc/p/'stat') for p in before}
            for p in before:
                if (proc/p/'cgroup').read_text().strip()!='0::/'+str(group.relative_to('/sys/fs/cgroup')):
                    raise RuntimeError('app process changed cgroup')
            after=set((group/'cgroup.procs').read_text().split())
            identities={p:read_stat(proc/p/'stat')['starttime'] for p in after}
        except (FileNotFoundError,ProcessLookupError):
            time.sleep(.001);continue
        if before==after and all(identities[p]==rows[p]['starttime'] for p in before):
            return dict(**total(rows),processes=rows,completed_ns=time.monotonic_ns(),attempts=attempt+1)
        time.sleep(.001)
    raise RuntimeError('app membership did not stabilize for a valid counter snapshot')


def difference(before,after):
    values={k:after[k]-before[k] for k in ('pgfault','pgmajfault')}
    if any(v<0 for v in values.values()):raise RuntimeError('task accounting decreased; process-tree scope may have changed')
    return values
