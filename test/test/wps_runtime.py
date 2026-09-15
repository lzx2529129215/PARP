"""Experiment-local WPS runtime for the installed OEM account-module defect.

The installed build launches qing autologin even with EnableAccount=false.
An A/B probe reproduced its SIGSEGV with false and removed it with true.
Keep binaries byte-identical and preserve the host installation and profiles.
"""
import hashlib
import json
from pathlib import Path
import shutil

OFFICE=Path('/opt/kingsoft/wps-office/office6')
EXECUTABLES=('wps','wpp','et','wpsoffice','wpscloudsvr')


def prepare_runtime(destination,source=OFFICE):
    destination=Path(destination);source=Path(source).resolve()
    original=(source/'cfgs/oem.ini').read_text()
    if 'EnableAccount=false' not in original and 'EnableAccount=true' not in original:
        raise RuntimeError('Unsupported WPS OEM configuration: account setting missing')
    destination.mkdir(parents=True,exist_ok=False)
    for entry in source.iterdir():
        target=destination/entry.name
        if entry.name=='cfgs':shutil.copytree(entry,target)
        elif entry.name in EXECUTABLES:shutil.copy2(entry,target)
        else:target.symlink_to(entry,target_is_directory=entry.is_dir())
    fixed=original.replace('EnableAccount=false','EnableAccount=true')
    (destination/'cfgs/oem.ini').write_text(fixed)
    digests={name:hashlib.sha256((source/name).read_bytes()).hexdigest() for name in EXECUTABLES}
    for name,digest in digests.items():
        if hashlib.sha256((destination/name).read_bytes()).hexdigest()!=digest:
            raise RuntimeError('WPS runtime executable copy differs from installed binary')
    manifest=dict(source=str(source),runtime=str(destination.resolve()),
        workaround='Initialize account module required by the automatically started qing service; no login automation.',
        changed_settings={'EnableAccount':'true'},source_oem_sha256=hashlib.sha256(original.encode()).hexdigest(),
        runtime_oem_sha256=hashlib.sha256(fixed.encode()).hexdigest(),binary_sha256=digests,
        source_oem=original,runtime_oem=fixed,resources='Read-only references to installed libraries/resources; copied executables and cfgs.')
    (destination/'parp-runtime-manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n')
    return destination.resolve()
