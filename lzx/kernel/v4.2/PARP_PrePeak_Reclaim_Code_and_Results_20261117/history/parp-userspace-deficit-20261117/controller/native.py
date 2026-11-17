"""Real cgroup-v2 implementation; never invoked by the scenario suite."""
import os
from pathlib import Path
import sys


class NativeCgroup:
    def __init__(self, directory, *, background, allow_freeze=False):
        if sys.platform!="linux":raise RuntimeError("native execution requires Linux")
        if background is not True:raise ValueError("only explicitly bound background groups may be controlled")
        self.path=Path(directory).resolve(strict=True)
        self.background=background
        self.allow_freeze=allow_freeze
        self.identity=(self.path.stat().st_dev,self.path.stat().st_ino)
        mounts=[]
        for line in Path('/proc/self/mountinfo').read_text().splitlines():
            left,_,right=line.partition(' - ')
            if right.split()[0]=='cgroup2':
                point=left.split()[4].replace('\\040',' ').replace('\\011','\t').replace('\\134','\\')
                mounts.append(Path(point).resolve())
        if not any(self.path!=m and self.path.is_relative_to(m) for m in mounts):
            raise ValueError('target must be inside a real cgroup2 mount')
        if not (self.path.parent/'cgroup.controllers').is_file():
            raise ValueError("non-root cgroup-v2 required")
        for name in ('memory.current','memory.stat','memory.high','memory.reclaim','memory.min','memory.low','memory.max','memory.pressure','cgroup.freeze'):
            if not (self.path/name).is_file():raise ValueError('missing '+name)
        self.check()

    def read(self,name):return (self.path/name).read_text(encoding='ascii').strip()

    def check(self):
        if not self.background:raise RuntimeError("application became foreground")
        if (self.path.stat().st_dev,self.path.stat().st_ino)!=self.identity:
            raise RuntimeError("cgroup identity changed")
        if any(p.is_dir() and (p/'memory.current').exists() for p in self.path.iterdir()):
            raise RuntimeError("only leaf groups may be controlled")
        flag=self.path/'memory.tier2_enabled'
        if flag.exists() and flag.read_text().strip()!='0':raise RuntimeError("another reclaim policy is active")

    def write(self,name,value):
        data=(str(value)+'\n').encode('ascii')
        # Preserve proactive semantics: lowering high attempts synchronous
        # reclaim even without new charges. Use separate per-app worker threads.
        flags=os.O_WRONLY
        fd=os.open(self.path/name,flags)
        try:
            if os.write(fd,data)!=len(data):raise OSError("short cgroup write")
        finally:os.close(fd)

    def usage(self):return int(self.read('memory.current'))
    def rss(self):
        total=0
        for value in self.read('cgroup.procs').split():
            try:
                fields=dict(line.split(':',1) for line in Path('/proc',value,'status').read_text().splitlines() if ':' in line)
                total+=int(fields.get('VmRSS','0 kB').split()[0])*1024
            except FileNotFoundError:continue
            except OSError as exc:raise RuntimeError('incomplete application RSS') from exc
        return total
    def read_high(self):
        value=self.read('memory.high')
        return None if value=='max' else int(value)
    def write_high(self,value):self.write('memory.high','max' if value is None else value)
    def maximum(self):
        value=self.read('memory.max')
        return 2**63-1 if value=='max' else int(value)
    def protections(self):return int(self.read('memory.min')),int(self.read('memory.low'))
    def reclaim(self,amount):self.write('memory.reclaim',amount)
    def read_freeze(self):return int(self.read('cgroup.freeze'))
    def write_freeze(self,value):
        if not self.allow_freeze:raise RuntimeError("background staggering was not enabled for this binding")
        self.write('cgroup.freeze',value)
    def feedback(self):
        stats=dict(line.split() for line in self.read('memory.stat').splitlines())
        refault=sum(int(stats.get(key,0)) for key in ('workingset_refault_anon','workingset_refault_file'))
        some=next(line for line in self.read('memory.pressure').splitlines() if line.startswith('some '))
        fields=dict(item.split('=') for item in some.split()[1:])
        return refault,int(fields['total'])/1_000_000
