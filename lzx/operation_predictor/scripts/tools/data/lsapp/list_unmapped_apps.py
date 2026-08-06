#!/usr/bin/env python3
import csv
import os
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
DATA_DIR = ROOT / 'data' / 'raw' / 'datasets' / 'LSApp'
TOOL_DIR = DATA_DIR / 'tool'
TSV_PATH = DATA_DIR / 'lsapp.tsv'
MAPPING_CSV = TOOL_DIR / 'lsapp_to_app_vocab.csv'
OUT_TXT = TOOL_DIR / 'unmapped_apps.txt'
OUT_CSV = TOOL_DIR / 'unmapped_app_counts.csv'


def load_mapping_keys(path):
    keys = set()
    with open(path, 'r', encoding='utf-8', errors='replace') as f:
        reader = csv.reader(f)
        try:
            header = next(reader)
            hdr = [h.strip().lower() for h in header]
            src_idx = hdr.index('lsapp_app') if 'lsapp_app' in hdr else 0
        except StopIteration:
            return keys
        for row in reader:
            if len(row) > src_idx:
                k = row[src_idx].strip()
                if k:
                    keys.add(k)
    return keys


def main():
    mapping_keys = load_mapping_keys(MAPPING_CSV)
    counts = Counter()
    if not os.path.exists(TSV_PATH):
        print('tsv not found', TSV_PATH); return
    with open(TSV_PATH, 'rb') as f:
        header_line = f.readline().decode('utf-8', 'replace').rstrip('\n\r')
        header = header_line.split('\t')
        hdr_low = [h.strip().lower() for h in header]
        app_idx = hdr_low.index('app_name') if 'app_name' in hdr_low else 3
        for raw in f:
            line = raw.decode('utf-8', 'replace').rstrip('\n\r')
            if not line: continue
            parts = line.split('\t')
            if len(parts) <= app_idx: continue
            app = parts[app_idx].strip()
            if app and app not in mapping_keys:
                counts[app] += 1
    apps = [a for a,_ in counts.most_common()]
    os.makedirs(TOOL_DIR, exist_ok=True)
    with open(OUT_TXT, 'w', encoding='utf-8') as f:
        for a in apps:
            f.write(a + '\n')
    with open(OUT_CSV, 'w', encoding='utf-8') as f:
        f.write('app_name,count\n')
        for a,c in counts.most_common():
            f.write(f'"{a}",{c}\n')
    print('unique unmapped apps:', len(apps))
    print('top 50:')
    for a,c in counts.most_common(50):
        print(f'{a}: {c}')
    print('written:', OUT_TXT, OUT_CSV)

if __name__ == '__main__':
    main()
