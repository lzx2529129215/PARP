"""Verify package hashes, frozen experiment sources and original qualification evidence."""
import hashlib
import json
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
NAME='parp-userspace-deficitfix-20261117'
def load(p):return json.loads(p.read_text(encoding='utf-8'))
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()


def main():
    manifest=load(ROOT/'manifest.json');bad=[]
    for item in manifest['files']:
        p=ROOT/item['path']
        if not p.is_file()or sha(p)!=item['sha256']:bad.append(item['path'])
    if bad:raise SystemExit('PACKAGE_HASH_MISMATCH: '+repr(bad))
    code=ROOT/'workspace'/NAME;run=load(code/'results/RUN-revisit.json')
    for name,digest in run['source_sha256'].items():
        if sha(code/name)!=digest:raise SystemExit('FROZEN_SOURCE_MISMATCH: '+name)
    q=load(code/'results/TOTAL_QUALIFICATION-revisit.json')
    for item in q['controls']+q['trials']:
        if sha(code/item['path'])!=item['sha256']:raise SystemExit('QUALIFICATION_EVIDENCE_MISMATCH: '+item['path'])
    frozen=load(ROOT/'workspace/krita-vm-acceptance/reclaim-frozen-before.json')
    for item in frozen['files']:
        if sha(ROOT/'workspace'/item['path'].replace('\\','/'))!=item['sha256']:
            raise SystemExit('HISTORICAL_SOURCE_MISMATCH: '+item['path'])
    print('PASS: %d packaged files; %d frozen runtime sources; qualification and historical hashes match.'%
          (len(manifest['files']),len(run['source_sha256'])))


if __name__=='__main__':main()
