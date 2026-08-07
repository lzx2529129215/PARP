#!/usr/bin/env python3
import os
import gzip
import json
from collections import Counter

ROOT = os.path.join(os.path.dirname(__file__), '..')
DATA_DIR = os.path.normpath(os.path.join(ROOT, 'data', 'datasets', 'LSApp'))
INPUT_GZ = os.path.join(DATA_DIR, 'after_mapping', 'lsapp_mapped.tsv.gz')
ALT_INPUT = os.path.join(DATA_DIR, 'after_mapping', 'lsapp_mapped.tsv')
OUT_TSV = os.path.join(DATA_DIR, 'session_opened_apps.tsv')

KEEP_EVENTS = set(['opened', 'user interaction'])


def open_input(path):
    if os.path.exists(path + '.gz'):
        return gzip.open(path + '.gz', 'rt', encoding='utf-8', errors='replace')
    if os.path.exists(path):
        # check if file is gz by extension
        if path.endswith('.gz'):
            return gzip.open(path, 'rt', encoding='utf-8', errors='replace')
        return open(path, 'r', encoding='utf-8', errors='replace')
    return None


def generate(input_path, out_path):
    fin = None
    if os.path.exists(INPUT_GZ):
        fin = gzip.open(INPUT_GZ, 'rt', encoding='utf-8', errors='replace')
    elif os.path.exists(ALT_INPUT):
        fin = open(ALT_INPUT, 'r', encoding='utf-8', errors='replace')
    else:
        # try generic
        fin = open_input(input_path)
    if fin is None:
        raise FileNotFoundError('Cannot find input lsapp_mapped file')

    header = fin.readline().rstrip('\n\r')
    cols = header.split('\t')
    col_idx = {c:i for i,c in enumerate(cols)}
    user_idx = col_idx.get('user_id', 0)
    sess_idx = col_idx.get('session_id', 1)
    app_idx = col_idx.get('app_name', 3)
    ev_idx = col_idx.get('event_type', 4)
    ts_idx = col_idx.get('timestamp', 2)

    cur_key = None
    cur_events = []  # list of (timestamp, app, ev)
    sessions = 0
    out_written = 0
    seq_lens = []
    with open(out_path, 'w', encoding='utf-8') as outf:
        outf.write('user_id\tsession_id\topened_apps\n')
        for line in fin:
            parts = line.rstrip('\n\r').split('\t')
            if len(parts) <= max(user_idx, sess_idx, app_idx, ev_idx, ts_idx):
                continue
            key = (parts[user_idx], parts[sess_idx])
            ts = parts[ts_idx]
            app = parts[app_idx]
            ev = parts[ev_idx].strip().lower()
            if cur_key is None:
                cur_key = key
                cur_events = [(ts, app, ev)]
            elif key == cur_key:
                cur_events.append((ts, app, ev))
            else:
                # process previous session
                seq = [a for t,a,e in sorted(cur_events, key=lambda x: x[0]) if e in KEEP_EVENTS]
                # remove consecutive duplicates
                seq = [s for i,s in enumerate(seq) if i==0 or s!=seq[i-1]]
                if seq:
                    outf.write(f"{cur_key[0]}\t{cur_key[1]}\t{json.dumps(seq, ensure_ascii=False)}\n")
                    out_written += 1
                    seq_lens.append(len(seq))
                sessions += 1
                # reset
                cur_key = key
                cur_events = [(ts, app, ev)]
        # last
        if cur_key is not None:
            seq = [a for t,a,e in sorted(cur_events, key=lambda x: x[0]) if e in KEEP_EVENTS]
            seq = [s for i,s in enumerate(seq) if i==0 or s!=seq[i-1]]
            if seq:
                outf.write(f"{cur_key[0]}\t{cur_key[1]}\t{json.dumps(seq, ensure_ascii=False)}\n")
                out_written += 1
                seq_lens.append(len(seq))
            sessions += 1
    fin.close()
    return {'sessions_seen': sessions, 'sessions_with_seq': out_written, 'avg_seq_len': (sum(seq_lens)/len(seq_lens) if seq_lens else 0)}


if __name__ == '__main__':
    stats = generate(INPUT_GZ, OUT_TSV)
    print('done, stats:', stats)
