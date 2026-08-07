#!/usr/bin/env python3
import os
import json
from collections import Counter

ROOT = os.path.join(os.path.dirname(__file__), '..')
DATA_DIR = os.path.normpath(os.path.join(ROOT, 'data', 'datasets', 'LSApp'))
TSV_PATH = os.path.join(DATA_DIR, 'lsapp.tsv')
OUT_TXT = os.path.join(DATA_DIR, 'user_ids.txt')
OUT_JSON = os.path.join(DATA_DIR, 'user_ids.json')
OUT_CSV = os.path.join(DATA_DIR, 'user_id_counts.csv')


def main():
    if not os.path.exists(TSV_PATH):
        print('tsv not found at', TSV_PATH); return
    users = Counter()
    with open(TSV_PATH, 'rb') as f:
        header_line = f.readline().decode('utf-8', 'replace').rstrip('\n\r')
        header = header_line.split('\t')
        hdr_low = [h.strip().lower() for h in header]
        if 'user_id' in hdr_low:
            user_idx = hdr_low.index('user_id')
        else:
            user_idx = 0
        for raw in f:
            line = raw.decode('utf-8', 'replace').rstrip('\n\r')
            if not line: continue
            parts = line.split('\t')
            if len(parts) <= user_idx: continue
            uid = parts[user_idx].strip()
            users[uid] += 1
    user_list = [u for u,_ in users.most_common()]
    with open(OUT_TXT, 'w', encoding='utf-8') as f:
        for u in user_list:
            f.write(u + '\n')
    with open(OUT_JSON, 'w', encoding='utf-8') as f:
        json.dump(user_list, f, ensure_ascii=False, indent=2)
    with open(OUT_CSV, 'w', encoding='utf-8') as f:
        f.write('user_id,count\n')
        for u,c in users.most_common():
            f.write(f'"{u}",{c}\n')
    print('unique users:', len(user_list))
    print('written:', OUT_TXT, OUT_JSON, OUT_CSV)

if __name__ == '__main__':
    main()
