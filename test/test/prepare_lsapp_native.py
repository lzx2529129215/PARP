#!/usr/bin/env python3
"""Preserve all original LSApp identities and reuse the visit-window builder."""
import argparse
import csv
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PREDICTOR = ROOT / 'lzx/tool/operation_predictor'
sys.path.insert(0, str(PREDICTOR))
from v3.src.data.build_app_dataset_visit_window import build_dataset, HORIZONS, VISIT_DEFINITION
from v3.src.data.build_app_dataset_duration import write_csv


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=PREDICTOR/'data/test1/raw/datasets/LSApp/extracted/lsapp.tsv')
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args()
    out = args.output_dir.resolve(); out.mkdir(parents=True, exist_ok=False)
    events, names, users = Counter(), Counter(), set()
    rows = []; opened_by_session = {}
    with args.source.open() as f:
        for row in csv.DictReader(f, delimiter='\t'):
            event, app = row['event_type'], row['app_name']
            assert app and not any(c in app for c in '|;') and not app.startswith('<')
            events[event] += 1; names[app] += 1; users.add(row['user_id'])
            key = (row['user_id'], row['session_id'])
            opened = opened_by_session.setdefault(key, {})
            if event == 'Closed':
                opened.pop(app, None)
            elif event in {'Opened', 'User Interaction'}:
                opened[app] = None
                rows.append({'user_id': row['user_id'], 'timestamp': row['timestamp'],
                    'foreground_app': app, 'raw_foreground_app': app,
                    'opened_apps': ';'.join(opened), 'user_group': '通用用户'})
    vocab = {app:i for i,app in enumerate(sorted(names))}
    vocab.update({'<PAD>':len(vocab), '<UNKNOWN>':len(vocab)+1})
    groups = {'通用用户':0}
    write_csv(out/'app_events.csv', list(rows[0]), rows)
    audit = {'source':str(args.source), 'source_sha256':hashlib.sha256(args.source.read_bytes()).hexdigest(),
        'events':dict(events), 'source_users':len(users), 'original_apps':len(names), 'source_app_counts':dict(names),
        'kept_event_rows':len(rows), 'mapping':'none; exact source app_name retained',
        'vocab_policy':'full observed app-name inventory; no frequency filter; not a test-label-driven subset',
        'opened_definition':'causal Opened/Interaction add, Closed remove, per original user/session; not process residency',
        'visit_definition_note':'same as prior pipeline: consecutive Opened/Interaction app identities are merged; this is not every raw Opened event'}
    (out/'source-audit.json').write_text(json.dumps(audit,ensure_ascii=False,indent=2)+'\n')
    print('raw audit',len(rows),'kept events',len(names),'original apps',flush=True)
    options=argparse.Namespace(history_len=5,duration_cap_s=600.,max_session_gap_s=3600.,
        periodic_anchor_s=180.,anchor_mode='event_plus_periodic',enable_debug_dwell_buckets=False)
    data,segments,stats=build_dataset(rows,vocab,groups,options)
    dataset=out/'dataset';dataset.mkdir()
    meta={'schema_version':1,'horizons_s':HORIZONS,'visit_definition':VISIT_DEFINITION,
        'app_vocab':vocab,'group_vocab':groups,'args':vars(options),
        'source_sha256':audit['source_sha256'],'source':str(args.source),'identity_mapping':'none',
        'split':'time_ordered_70_15_15_equal_timestamps_kept_together','segment_stats':stats,'splits':{}}
    for split,values in data.items():
        assert values
        write_csv(dataset/f'{split}.csv',list(values[0]),values)
        count=Counter(len(set(filter(None,r['opened_apps'].split('|')))-{r['current_app']}) for r in values)
        meta['splits'][split]={'samples':len(values),'start':values[0]['timestamp'],'end':values[-1]['timestamp'],
            'event_opened_background_histogram':dict(sorted(count.items()))}
    write_csv(dataset/'segments.csv',list(segments[0]),segments)
    (dataset/'dataset_meta.json').write_text(json.dumps(meta,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(meta['splits'],ensure_ascii=False),flush=True)


if __name__=='__main__':main()
