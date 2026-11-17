"""Temporary zswap guard and scoped real WSS/metrics collector. No command RPC."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import time

BRIDGE=Path('/home/qi/parp-measured-lab-20261113')
PARENT=Path('/sys/fs/cgroup/user.slice/user-1000.slice/user@1000.service')
UID=1000
GID=1000
GROUPS={'gimp':'background','libreoffice':'libreoffice','firefox':'firefox','krita':'foreground'}
HEADER=re.compile(r'^[0-9a-f]+-[0-9a-f]+ ')


def read_json(path):return json.loads(path.read_text())


def save(path,row):
    if path.parent.resolve()!=BRIDGE.resolve():raise RuntimeError('output outside bridge')
    temporary=path.with_suffix('.tmp')
    fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_TRUNC|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'w') as output:json.dump(row,output,ensure_ascii=False)
    os.chown(temporary,UID,GID)
    os.replace(temporary,path)


def kv(path):
    result={}
    for line in path.read_text().splitlines():
        parts=line.split(maxsplit=1)
        if parts:result[parts[0]]=parts[1] if len(parts)>1 else ''
    return result


def stat(pid):
    text=Path('/proc',str(pid),'stat').read_text()
    values=text[text.rfind(')')+2:].split()
    return dict(pid=pid,starttime=values[19],minor=int(values[7]),major=int(values[9]))


def validated(binding):
    if not re.fullmatch(r'[a-f0-9]{16}',binding['run_id']):raise ValueError('invalid run id')
    base=Path(binding['base'])
    if base.parent!=PARENT or not re.fullmatch(r'parp-measured-[a-f0-9]{12}',base.name):
        raise ValueError('unowned test cgroup')
    if base.is_symlink() or base.stat().st_uid!=UID:raise ValueError('unexpected cgroup ownership')
    if set(binding['groups'])!=set(GROUPS):raise ValueError('exact four-app binding required')
    for app,name in GROUPS.items():
        if binding['groups'][app]!=str(base/name):raise ValueError('unexpected leaf binding')
        if (base/name).is_symlink():raise ValueError('unexpected leaf symlink')
    return base


def wss(pids,group,clear):
    """Same clear_refs/Referenced convention as the existing dataset collector.

    File-backed references use max per mapped path across processes (a lower
    bound on the union); private anonymous references are summed. Inaccessible
    live PIDs invalidate the sample instead of silently becoming WSS=0.
    """
    anon=0;files={};rss=0;errors=[];identities=[];exited=[]
    for pid in pids:
        try:
            identity=stat(pid)
            status=kv(Path('/proc',str(pid),'status'))
            if int(status['Uid:'].split()[0])!=UID:raise RuntimeError('PID owner changed')
            expected='0::/'+str(group.relative_to('/sys/fs/cgroup'))
            if Path('/proc',str(pid),'cgroup').read_text().strip()!=expected:raise RuntimeError('PID moved')
            text=Path('/proc',str(pid),'smaps').read_text()
            if stat(pid)['starttime']!=identity['starttime']:raise RuntimeError('PID reused')
            if clear:Path('/proc',str(pid),'clear_refs').write_text('1\n')
            mine={};current=None;vmas=[]
            for line in text.splitlines():
                if HEADER.match(line):
                    parts=line.split(None,5)
                    current=dict(path=parts[5] if len(parts)>5 and parts[5].startswith('/') else None,ref=0,anon=0)
                    vmas.append(current)
                elif current is not None and line.startswith('Referenced:'):current['ref']=int(line.split()[1])
                elif current is not None and line.startswith('Anonymous:'):current['anon']=int(line.split()[1])
                elif line.startswith('Rss:'):rss+=int(line.split()[1])*1024
            for item in vmas:
                if item['path']:
                    private=min(item['ref'],item['anon']);anon+=private
                    mine[item['path']]=mine.get(item['path'],0)+item['ref']-private
                else:anon+=item['ref']
            for name,count in mine.items():files[name]=max(files.get(name,0),count)
            identities.append(identity)
        except (FileNotFoundError,ProcessLookupError):exited.append(pid)
        except (OSError,RuntimeError) as exc:errors.append(dict(pid=pid,error=repr(exc)))
    return dict(wss_bytes=(anon+sum(files.values()))*1024,rss_sum_bytes=rss,
                wss_anon_bytes=anon*1024,wss_file_bytes=sum(files.values())*1024,
                identities=identities,exited_pids=exited,errors=errors,valid=not errors and bool(identities))


def metrics(group,pids,read_wss):
    row=wss(pids,group,clear=read_wss)
    row['wss_window_valid']=read_wss
    row.update(current=int((group/'memory.current').read_text()),swap=int((group/'memory.swap.current').read_text()),
        stats={k:int(v) for k,v in kv(group/'memory.stat').items()},
        cpu=kv(group/'cpu.stat') if (group/'cpu.stat').exists() else {},
        memory_pressure=(group/'memory.pressure').read_text(),
        io_pressure=(group/'io.pressure').read_text() if (group/'io.pressure').exists() else None,
        cpu_pressure=(group/'cpu.pressure').read_text() if (group/'cpu.pressure').exists() else None,
        io_stat=(group/'io.stat').read_text() if (group/'io.stat').exists() else None,
        zswap_current=int((group/'memory.zswap.current').read_text()) if (group/'memory.zswap.current').exists() else None)
    return row


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--minutes',type=int,default=360);args=parser.parse_args()
    if os.getuid()!=0 or not 1<=args.minutes<=360:raise SystemExit('sudo required; duration 1..360 minutes')
    if not BRIDGE.is_dir() or BRIDGE.is_symlink() or BRIDGE.stat().st_uid!=UID:
        raise SystemExit('ordinary-user bridge missing or unsafe')
    enabled=Path('/sys/module/zswap/parameters/enabled');prior=enabled.read_text().strip()
    stop=False
    def finish(*_):
        nonlocal stop
        stop=True
    signal.signal(signal.SIGINT,finish);signal.signal(signal.SIGTERM,finish)
    deadline=time.monotonic()+args.minutes*60
    output=None;run_id=None;last_time=None
    source_sha=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    save(BRIDGE/'guard_before.json',dict(zswap=prior,kernel=os.uname().release,pid=os.getpid()))
    try:
        enabled.write_text('1\n')
        if enabled.read_text().strip()!='Y':raise RuntimeError('zswap enable failed')
        save(BRIDGE/'guard_status.json',dict(status='ACTIVE',zswap='Y',pid=os.getpid(),expires_monotonic=deadline,
             source_sha256=source_sha,protocol='same-service-v1'))
        print('MEASURED_COMPRESSION_ACTIVE',flush=True)
        while not stop and time.monotonic()<deadline and not (BRIDGE/'STOP').exists():
            target=BRIDGE/'binding.json'
            if not target.exists():time.sleep(.1);continue
            try:
                binding=read_json(target)
                if not binding.get('active'):
                    if output is not None:output.close();output=None
                    run_id=None;time.sleep(.1);continue
                base=validated(binding)
                if binding.get('interval_s',1.0)!=1.0:raise ValueError('this protocol requires one-second sampling')
                sampling=dict(schema=1,source_sha256=source_sha,interval_s=1.0,
                    mode='reference_clear_refs' if binding['wss'] else 'passive_smaps',clear_refs=binding['wss'])
                if binding['run_id']!=run_id:
                    if output is not None:output.close()
                    run_id=binding['run_id'];path=BRIDGE/('telemetry-'+run_id+'.jsonl')
                    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
                    os.fchown(fd,UID,GID);output=os.fdopen(fd,'w',buffering=1)
                    # Prime before reporting readiness, so first measured window
                    # does not include the application's earlier startup.
                    prime={app:metrics(base/name,[int(p) for p in (base/name/'cgroup.procs').read_text().split()],binding['wss'])
                           for app,name in GROUPS.items()}
                    if not all(row['valid'] for row in prime.values()):raise RuntimeError('WSS/RSS prime incomplete')
                    last_time=time.monotonic();next_sample=last_time+binding.get('interval_s',1.0)
                    save(BRIDGE/'ready.json',dict(run_id=run_id,primed_monotonic=last_time,groups=prime,sampling_contract=sampling))
                if time.monotonic()<next_sample:time.sleep(min(.05,next_sample-time.monotonic()));continue
                begin=time.monotonic()
                apps={app:metrics(base/name,[int(p) for p in (base/name/'cgroup.procs').read_text().split()],binding['wss'])
                      for app,name in GROUPS.items()}
                end=time.monotonic()
                row=dict(run_id=run_id,window_start=last_time,window_end=begin,read_finished=end,
                    collection_seconds=end-begin,cleared_references=binding['wss'],sampling_contract=sampling,apps=apps,
                    total_current=int((base/'memory.current').read_text()),
                    meminfo={k:v for k,v in kv(Path('/proc/meminfo')).items() if k in ('MemFree:','MemAvailable:','Zswap:','Zswapped:')})
                output.write(json.dumps(row)+'\n');last_time=begin
                next_sample=begin+binding.get('interval_s',1.0)
            except (OSError,ValueError,KeyError,RuntimeError) as exc:
                save(BRIDGE/'collector_error.json',dict(run_id=run_id,error=repr(exc),monotonic=time.monotonic()))
                if output is not None:output.close();output=None
                # A disappeared test cgroup means workload cleanup. Any error
                # is visible and invalidates the reference/calibration trial.
                run_id=None;time.sleep(.2)
    finally:
        if output is not None:output.close()
        enabled.write_text('1\n' if prior=='Y' else '0\n')
        save(BRIDGE/'guard_status.json',dict(status='RESTORED',zswap=enabled.read_text().strip(),pid=os.getpid()))
        print('MEASURED_COMPRESSION_RESTORED',flush=True)


if __name__=='__main__':main()
