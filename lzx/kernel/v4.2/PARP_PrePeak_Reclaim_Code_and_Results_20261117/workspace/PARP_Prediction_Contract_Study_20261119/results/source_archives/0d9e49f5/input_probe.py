"""Read-only guest feature probe and preservation of executed source snapshots."""
import hashlib
import json
import os
from pathlib import Path
import shutil


def probe(results):
    config_path = Path('/boot')/('config-'+os.uname().release)
    try:
        config = config_path.read_text()
    except OSError:
        config = ''
    features = {name: next((line for line in config.splitlines()
                            if line.startswith(name+'=') or line == '# '+name+' is not set'), 'UNKNOWN')
                for name in ('CONFIG_IDLE_PAGE_TRACKING','CONFIG_DAMON','CONFIG_DAMON_VADDR',
                             'CONFIG_PROC_PAGE_MONITOR','CONFIG_MEMCG')}
    paths = ['/sys/kernel/mm/page_idle/bitmap','/sys/kernel/mm/damon/admin',
             '/proc/self/pagemap','/proc/self/clear_refs','/proc/self/smaps_rollup']
    info = {'uid':os.getuid(),'kernel':os.uname().release,'features':features,
            'paths':{p:{'exists':Path(p).exists(),'readable':os.access(p,os.R_OK),
                        'writable':os.access(p,os.W_OK)} for p in paths},
            'action':'read-only feature/config checks; no page bits changed'}
    (results/'INPUT_AVAILABILITY.json').write_text(json.dumps(info,indent=2)+'\n')
    original = results.parent
    sources = [('initial',original)]
    if (original/'revisions').exists():
        sources += [(p.name,p) for p in (original/'revisions').iterdir() if p.is_dir()]
    archive = results/'source_archives'; archive.mkdir(exist_ok=True)
    for name, source in sources:
        target = archive/name; target.mkdir(exist_ok=True)
        for path in source.glob('*.py'):
            shutil.copyfile(path,target/path.name)
        for filename in ('source_baseline.json','selection.json'):
            if (source/filename).exists(): shutil.copyfile(source/filename,target/filename)
        shutil.copytree(source/'controller',target/'controller',dirs_exist_ok=True)
    print('INPUT_PROBE_AND_EXECUTED_SOURCES_PRESERVED',flush=True)
