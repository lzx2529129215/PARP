#!/usr/bin/env python3
"""Audit coverage and identity-collapse before training a 30-app model."""
import argparse
import csv
import datetime as dt
import hashlib
import json
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PRED = ROOT / 'lzx/tool/operation_predictor'


def load_map(path):
    mapping = {}
    data = json.loads(path.read_text())
    for rule in data['mapping_rules']:
        assert rule['lsapp_apps'], rule['mapped_app']
        for source in rule['lsapp_apps']:
            assert source not in mapping, source
            mapping[source] = rule['mapped_app']
    return mapping


def project_opened(source_opened, mapping):
    """Project after changing source state, so one alias cannot close another."""
    return {mapping[a] for a in source_opened if a in mapping}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args()
    out = args.output_dir; out.mkdir(parents=True, exist_ok=False)
    source = PRED/'data/test1/raw/datasets/LSApp/extracted/lsapp.tsv'
    paths = {'15':PRED/'data/lsapp_expanded/mapping/lsapp_to_linux.json',
             '30':PRED/'data/lsapp_30/mapping/lsapp_to_linux.json'}
    maps = {name:load_map(path) for name,path in paths.items()}
    vocabulary = json.loads((PRED/'data/vocab/lsapp_30/app_vocab_duration.json').read_text())
    targets = {a for a in vocabulary if not a.startswith('<')}
    assert len(targets)==30 and len(vocabulary)==32 and targets==set(maps['30'].values())
    assert len(set(vocabulary.values()))==32
    old_v = json.loads((PRED/'data/vocab/lsapp_expanded/app_vocab_duration.json').read_text())
    assert all(vocabulary[a]==i for a,i in old_v.items() if not a.startswith('<'))
    # The new state projection must retain the domain while any source is open.
    assert project_opened({'Google Chrome'}, maps['30']) == {'Falkon'}
    assert project_opened({'Google Chrome','Samsung Internet Browser'}-{'Google Chrome'},maps['30']) == {'Falkon'}
    counts = {name:Counter() for name in maps}; target_counts = {name:Counter() for name in maps}
    unmapped = {name:Counter() for name in maps}; source_counts=Counter(); previous={}
    with source.open() as f:
        for row in csv.DictReader(f, delimiter='\t'):
            app,event,user = row['app_name'],row['event_type'],row['user_id']
            source_counts[app]+=1
            for name,mapping in maps.items():
                counts[name]['raw_event_rows']+=1
                if app in mapping:
                    counts[name]['mapped_raw_event_rows']+=1
                    target_counts[name][mapping[app]]+=1
                else:unmapped[name][app]+=1
            if event not in {'Opened','User Interaction'}:continue
            when=dt.datetime.fromisoformat(row['timestamp'])
            prior=previous.get(user)
            for name,mapping in maps.items():
                c=counts[name];c['foreground_evidence_rows']+=1
                c['mapped_foreground_evidence_rows']+=int(app in mapping)
                if prior and prior[0]!=app and 0 <= (when-prior[1]).total_seconds() <= 3600:
                    c['original_app_transitions']+=1
                    if prior[0] not in mapping or app not in mapping:
                        c['transitions_with_unmapped_endpoint']+=1
                    elif mapping[prior[0]]==mapping[app]:
                        c['transitions_collapsed_by_mapping']+=1
                    else:c['transitions_retained_as_distinct_targets']+=1
            previous[user]=(app,when)
    report={'source':str(source),'source_sha256':hashlib.sha256(source.read_bytes()).hexdigest(),
        'mapping_sha256':{k:hashlib.sha256(p.read_bytes()).hexdigest() for k,p in paths.items()},
        'definition':'adjacent original Opened/Interaction identity changes per user, gaps <=3600s; no label accuracy claim',
        'validation':'unique source assignment, 30 nonempty targets, 32 unique token IDs, old 15 IDs preserved, source-first state alias test passed',
        'results':{}}
    for name,c in counts.items():
        assert c['original_app_transitions']==sum(c[k] for k in ('transitions_with_unmapped_endpoint','transitions_collapsed_by_mapping','transitions_retained_as_distinct_targets'))
        assert set(maps[name])<=source_counts.keys()
        report['results'][name]={**dict(c),'mapped_source_names':len(maps[name]),
            'target_count':len(set(maps[name].values())),
            'raw_event_coverage':c['mapped_raw_event_rows']/c['raw_event_rows'],
            'foreground_event_coverage':c['mapped_foreground_evidence_rows']/c['foreground_evidence_rows'],
            'distinct_transition_retention':c['transitions_retained_as_distinct_targets']/c['original_app_transitions'],
            'target_event_counts':dict(target_counts[name]),'unmapped_source_event_counts':dict(unmapped[name].most_common())}
    (out/'mapping-audit.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({k:{a:b for a,b in v.items() if not a.endswith('_counts')} for k,v in report['results'].items()},ensure_ascii=False,indent=2))


if __name__=='__main__':main()
