"""Package only the independent external-GUI module; never include ORA or old code."""
import hashlib
from pathlib import Path
import stat
import zipfile
import argparse

ROOT=Path(__file__).resolve().parent.parent


def build(output=None):
    files=[ROOT/n for n in ['.gitignore','README.md','DEPLOY.md','VALIDATION.md','requirements.txt','outputs/.gitkeep']]
    for folder,suffix in [('configs','.json'),('oomlab_krita_gui','.py'),('tests','.py'),('scripts','.py'),('scripts','.sh')]:
        files+=sorted(p for p in (ROOT/folder).rglob('*'+suffix) if '__pycache__' not in p.parts)
    files+=[ROOT/'outputs/qt-env-validation'/n for n in ['summary.json','unittest.log','preflight.json']]
    data={'foreground_krita_gui/'+str(p.relative_to(ROOT)):p.read_bytes() for p in files}
    names=sorted([*data,'foreground_krita_gui/FILELIST.txt','foreground_krita_gui/SHA256SUMS'])
    data['foreground_krita_gui/FILELIST.txt']=('\n'.join(names)+'\n').encode()
    data['foreground_krita_gui/SHA256SUMS']=''.join(hashlib.sha256(data[n]).hexdigest()+'  '+n+'\n' for n in names if not n.endswith('/SHA256SUMS')).encode()
    output=Path(output) if output else ROOT.parent/'foreground_krita_gui_qt_env_fix_bundle.zip'
    with zipfile.ZipFile(output,'x',compression=zipfile.ZIP_DEFLATED) as z:
        for name in names:
            i=zipfile.ZipInfo(name,(1980,1,1,0,0,0));i.create_system=3
            i.external_attr=(stat.S_IFREG|(0o755 if name.endswith('.sh') else 0o644))<<16
            i.compress_type=zipfile.ZIP_DEFLATED; z.writestr(i,data[name])
    print(output); print(output.stat().st_size); print(hashlib.sha256(output.read_bytes()).hexdigest())


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',help='新 ZIP 路径；拒绝覆盖已有文件')
    build(parser.parse_args().output)
