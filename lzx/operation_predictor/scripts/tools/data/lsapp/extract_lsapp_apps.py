#!/usr/bin/env python3
import csv
import json
from collections import Counter
import os

DATA_DIR = os.path.join(os.path.dirname(__file__), '..', 'data', 'datasets', 'LSApp')
TSV_PATH = os.path.join(DATA_DIR, 'lsapp.tsv')
OUT_TXT = os.path.join(DATA_DIR, 'app_names.txt')
OUT_JSON = os.path.join(DATA_DIR, 'app_names.json')
OUT_CSV = os.path.join(DATA_DIR, 'app_name_counts.csv')

def main():
    if not os.path.exists(TSV_PATH):
        print('lsapp.tsv not found at', TSV_PATH)
        return
    counts = Counter()
    # Read file in binary and decode per-line to tolerate NULs or malformed bytes
    with open(TSV_PATH, 'rb') as f:
        # header
        header_line = f.readline().decode('utf-8', 'replace').rstrip('\n\r')
        if not header_line:
            print('empty file')
            return
        header = header_line.split('\t')
        header_l = [h.strip().lower() for h in header]
        if 'app_name' in header_l:
            idx = header_l.index('app_name')
        else:
            for cand in ['app','appname','app name']:
                if cand in header_l:
                    idx = header_l.index(cand)
                    break
            else:
                print('could not find app_name column in header:', header)
                return
        for raw in f:
            line = raw.decode('utf-8', 'replace').rstrip('\n\r')
            row = line.split('\t')
            if len(row) <= idx:
                continue
            app = row[idx].strip()
            if app:
                counts[app] += 1
    apps_sorted = [a for a,_ in counts.most_common()]
    # write outputs
    with open(OUT_TXT, 'w', encoding='utf-8') as f:
        for a in apps_sorted:
            f.write(a + '\n')
    with open(OUT_JSON, 'w', encoding='utf-8') as f:
        json.dump(apps_sorted, f, ensure_ascii=False, indent=2)
    with open(OUT_CSV, 'w', encoding='utf-8') as f:
        f.write('app_name,count\n')
        for a,c in counts.most_common():
            f.write(f'"{a}",{c}\n')
    print('unique_apps:', len(apps_sorted))
    print('outputs written to:')
    print(' ', OUT_TXT)
    print(' ', OUT_JSON)
    print(' ', OUT_CSV)

if __name__ == '__main__':
    main()
