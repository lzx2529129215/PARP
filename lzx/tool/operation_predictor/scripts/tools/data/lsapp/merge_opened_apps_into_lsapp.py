#!/usr/bin/env python3
"""Merge session_opened_apps (per-session opened sequence) back into lsapp_mapped TSV.

Result: writes lsapp_mapped_with_opened.tsv.gz under after_mapping/add_opened_apps/.
Each input event row gets an extra column `opened_apps` (JSON list) corresponding to its session.
"""
import os
import gzip
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / 'data' / 'datasets' / 'LSApp'
AFTER = DATA_DIR / 'after_mapping'
ADD_DIR = AFTER / 'add_opened_apps'
ADD_DIR.mkdir(parents=True, exist_ok=True)

SESSION_TSV_CANDIDATES = [
    AFTER / 'add_opened_apps' / 'session_opened_apps.tsv',
    DATA_DIR / 'session_opened_apps.tsv',
    AFTER / 'session_opened_apps.tsv',
]
LSAPP_GZ = AFTER / 'lsapp_mapped.tsv.gz'
LSAPP_PLAIN = AFTER / 'lsapp_mapped.tsv'
OUT_GZ = ADD_DIR / 'lsapp_mapped_with_opened.tsv.gz'


def load_session_map():
    path = None
    for p in SESSION_TSV_CANDIDATES:
        if p.exists():
            path = p
            break
    if path is None:
        raise FileNotFoundError('session_opened_apps.tsv not found in candidates: ' + ','.join(str(p) for p in SESSION_TSV_CANDIDATES))
    sess_map = {}
    with open(path, 'r', encoding='utf-8') as f:
        header = f.readline().rstrip('\n\r')
        cols = header.split('\t')
        u_i = cols.index('user_id') if 'user_id' in cols else 0
        s_i = cols.index('session_id') if 'session_id' in cols else 1
        apps_i = cols.index('opened_apps') if 'opened_apps' in cols else 2
        for line in f:
            parts = line.rstrip('\n\r').split('\t')
            if len(parts) <= apps_i:
                continue
            key = (parts[u_i], parts[s_i])
            sess_map[key] = parts[apps_i]
    return sess_map


def open_input():
    if LSAPP_GZ.exists():
        return gzip.open(LSAPP_GZ, 'rt', encoding='utf-8', errors='replace')
    if LSAPP_PLAIN.exists():
        return open(LSAPP_PLAIN, 'r', encoding='utf-8', errors='replace')
    raise FileNotFoundError('lsapp_mapped not found in after_mapping')


def merge():
    sess_map = load_session_map()
    fin = open_input()
    outf = gzip.open(OUT_GZ, 'wt', encoding='utf-8')
    header = fin.readline().rstrip('\n\r')
    cols = header.split('\t')
    # add column
    new_header = header + '\topened_apps'
    outf.write(new_header + '\n')
    # find indices
    cols_low = [c.lower() for c in cols]
    u_i = cols_low.index('user_id') if 'user_id' in cols_low else 0
    s_i = cols_low.index('session_id') if 'session_id' in cols_low else 1
    total = 0
    matched = 0
    for line in fin:
        total += 1
        parts = line.rstrip('\n\r').split('\t')
        key = (parts[u_i], parts[s_i]) if len(parts) > max(u_i, s_i) else None
        opened = sess_map.get(key, '[]')
        if opened != '[]':
            matched += 1
        outf.write(line.rstrip('\n\r') + '\t' + opened + '\n')
    fin.close()
    outf.close()
    return {'total_rows': total, 'matched_sessions_rows': matched, 'unique_sessions_in_map': len(sess_map)}


if __name__ == '__main__':
    stats = merge()
    print('merge complete:', stats)
