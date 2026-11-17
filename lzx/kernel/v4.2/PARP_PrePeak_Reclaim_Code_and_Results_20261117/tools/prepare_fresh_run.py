"""Copy the archived tested code to a fresh sibling directory; never overwrite evidence."""
import argparse
from pathlib import Path
import re
import shutil

ROOT=Path(__file__).resolve().parents[1]
NAME='parp-userspace-deficitfix-20261117'


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--name',required=True,help='ASCII letters, numbers, hyphens or underscores')
    args=p.parse_args()
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,80}',args.name):raise ValueError('invalid fresh directory name')
    work=ROOT/'workspace';source=work/NAME;target=work/args.name
    if target.exists():raise ValueError('destination already exists; archived evidence is never overwritten')
    target.mkdir()
    for file in source.rglob('*'):
        rel=file.relative_to(source)
        if any(x in ('results','__pycache__','.git')for x in rel.parts):continue
        if file.is_file()and(file.suffix in('.py','.sh')or rel.as_posix()in('SOURCE_BASELINE.json','lab/firefox-install.json')):
            out=target/rel;out.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(file,out)
    (target/'results').mkdir()
    print(target)
    print('Fresh code only. Collect a new unclipped reference before any newly scored cohort.')


if __name__=='__main__':main()
