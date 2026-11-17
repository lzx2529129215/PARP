"""Load measured WSS/counts and occupancy; never substitute RSS for WSS."""
import hashlib
import json
from pathlib import Path


def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load(path):return json.loads(Path(path).read_text())


def measured_reference(path):
    path=Path(path);trial=load(path)
    if trial['stage']!='reference' or trial['status']!='COMPLETE' or not trial['cleanup']['complete']:
        raise ValueError('completed, cleaned real reference required')
    if trial['zswap']!='Y':raise ValueError('compression reference required')
    root_rows=trial['root_telemetry']
    if not root_rows or any(not row['cleared_references'] for row in root_rows):raise ValueError('real clear_refs windows required')
    origin=trial['start_monotonic_s'];signals={app:[] for app in trial['initial']['groups']}
    previous={app:row['stats']['pgfault'] for app,row in trial['initial']['groups'].items()}
    for row in root_rows:
        for app,item in row['apps'].items():
            if not item['valid'] or item['errors']:raise ValueError('incomplete WSS process coverage')
            end=row['window_end']-origin;start=max(0,row['window_start']-origin)
            if end<=start:continue
            faults=item['stats']['pgfault']-previous[app]
            if faults<0:raise ValueError('fault counter reset')
            signals[app].append(dict(start_s=start,end_s=end,wss_bytes=item['wss_bytes'],pgfault=faults,
                valid=True,rss_bytes=item['rss_sum_bytes'],cgroup_bytes=item['current']))
            previous[app]=item['stats']['pgfault']
    if any(len(rows)<8 for rows in signals.values()):raise ValueError('reference too short')
    samples=[json.loads(line) for line in (path.parent/'occupancy.jsonl').read_text().splitlines()]
    prefix=dict(time_s=0,total=trial['initial']['current'],apps={app:item['current'] for app,item in trial['initial']['groups'].items()})
    occupancy=[prefix]+[row for row in samples if row['time_s']>0]
    return dict(trial=trial,trial_path=str(path.resolve()),trial_sha256=sha(path),signals=signals,occupancy=occupancy,
                wss_method='clear_refs + smaps Referenced; original collector file-path max convention',
                wss_not_unique_physical_union=True)


def pgfault(trial,app):return trial['final']['groups'][app]['stats']['pgfault']-trial['initial']['groups'][app]['stats']['pgfault']
def task_ms(trial,app):return sum(row['elapsed_ms'] for row in trial['operations'] if row['app']==app and row['kind']!='warm_hot')
def compressed(trial,app):
    before=trial['initial']['groups'][app]['stats']
    return any(row['apps'][app].get('zswap_current',0) or row['apps'][app]['stats'].get('zswapped',0)>before.get('zswapped',0)
               for row in trial.get('root_telemetry',[]))
