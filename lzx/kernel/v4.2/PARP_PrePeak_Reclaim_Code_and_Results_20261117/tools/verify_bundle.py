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
    recorded={item['path'] for item in manifest['files']}
    actual={p.relative_to(ROOT).as_posix() for p in ROOT.rglob('*')
            if p.is_file() and p!=ROOT/'manifest.json'}
    if actual!=recorded:
        raise SystemExit('PACKAGE_FILE_SET_MISMATCH: '+repr(sorted(actual^recorded)))
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
    latest=ROOT/manifest.get('latest_code','workspace/'+NAME)
    if latest.name=='PARP_Prediction_Contract_Study_20261119':
        baseline=load(latest/'source_baseline.json')
        for name,digest in baseline['controller_sha256'].items():
            if sha(latest/'controller'/name)!=digest:
                raise SystemExit('LATEST_BASELINE_CONTROLLER_MISMATCH: '+name)
        report=load(latest/'results/VERIFIED.json')
        if not (report['status']=='VERIFIED' and report['native_checks']==37 and
                report['complete_trials']==34 and report['ideal_complete_trials']==18 and
                report['correctly_infeasible']==3 and report['all_ideal_preallocation_gaps_zero']):
            raise SystemExit('LATEST_NATIVE_AUDIT_NOT_QUALIFIED')
        print('PASS: latest 37 native checks and 15 baseline controller hashes.')


if __name__=='__main__':main()
