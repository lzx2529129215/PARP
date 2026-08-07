#!/usr/bin/env python3
"""Apply user-provided mapping from lsapp app names to target vocab.

Reads:
  - LSApp TSV: data/raw/datasets/LSApp/lsapp.tsv
  - Mapping CSV: data/raw/datasets/LSApp/tool/lsapp_to_app_vocab.csv (cols: lsapp_app,mapped_app)

Writes:
  - data/raw/datasets/LSApp/after_mapping/lsapp_mapped.tsv.gz
  - data/raw/datasets/LSApp/after_mapping/mapping_apply_report.json
"""
import csv
import gzip
import json
import os
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
DATA_DIR = ROOT / 'data' / 'raw' / 'datasets' / 'LSApp'

TSV_PATH = DATA_DIR / 'lsapp.tsv'
MAPPING_CSV = DATA_DIR / 'tool' / 'lsapp_to_app_vocab.csv'
OUT_GZ = DATA_DIR / 'after_mapping' / 'lsapp_mapped.tsv.gz'
REPORT = DATA_DIR / 'after_mapping' / 'mapping_apply_report.json'


def load_mapping(path):
    m = {}
    if not os.path.exists(path):
        raise FileNotFoundError(path)
    with open(path, 'r', encoding='utf-8', errors='replace') as f:
        reader = csv.reader(f)
        try:
            header = next(reader)
        except StopIteration:
            return m
        # find columns
        hdr = [h.strip().lower() for h in header]
        try:
            src_idx = hdr.index('lsapp_app')
            tgt_idx = hdr.index('mapped_app')
        except ValueError:
            # fallback first two columns
            src_idx = 0; tgt_idx = 1
        for row in reader:
            if len(row) <= src_idx:
                continue
            src = row[src_idx].strip()
            tgt = row[tgt_idx].strip() if len(row) > tgt_idx else ''
            if src:
                # if target empty, map to empty string (means keep original)
                m[src] = tgt
    return m


def apply_mapping(tsv_path, mapping, out_gz, report_path):
    stats = Counter()
    mapped_examples = {}
    if not os.path.exists(tsv_path):
        raise FileNotFoundError(tsv_path)
    os.makedirs(os.path.dirname(out_gz), exist_ok=True)
    with open(tsv_path, 'rb') as inf, gzip.open(out_gz, 'wt', encoding='utf-8') as outf:
        # read header
        header_line = inf.readline().decode('utf-8', 'replace').rstrip('\n\r')
        if not header_line:
            raise ValueError('empty tsv')
        header = header_line.split('\t')
        # find app_name column
        hdr_low = [h.strip().lower() for h in header]
        if 'app_name' in hdr_low:
            app_idx = hdr_low.index('app_name')
        else:
            app_idx = 3  # assume default
        # write header
        outf.write('\t'.join(header) + '\n')
        for raw in inf:
            line = raw.decode('utf-8', 'replace').rstrip('\n\r')
            if not line:
                continue
            parts = line.split('\t')
            if len(parts) <= app_idx:
                outf.write(line + '\n')
                continue
            orig = parts[app_idx].strip()
            if orig in mapping:
                tgt = mapping[orig]
                if tgt:
                    parts[app_idx] = tgt
                    stats['mapped'] += 1
                    mapped_examples[orig] = mapped_examples.get(orig, 0) + 1
                else:
                    # empty mapping => keep original
                    stats['kept'] += 1
            else:
                stats['unmapped'] += 1
            outf.write('\t'.join(parts) + '\n')
            stats['total_rows'] += 1

    # build report
    report = {
        'total_rows': stats.get('total_rows', 0),
        'mapped_count': stats.get('mapped', 0),
        'kept_count': stats.get('kept', 0),
        'unmapped_count': stats.get('unmapped', 0),
        'mapped_examples_sample': dict(list(mapped_examples.items())[:50]),
        'mapping_entries': len(mapping)
    }
    with open(report_path, 'w', encoding='utf-8') as rf:
        json.dump(report, rf, ensure_ascii=False, indent=2)
    return report


def main():
    mapping = load_mapping(MAPPING_CSV)
    print('loaded mapping entries:', len(mapping))
    report = apply_mapping(TSV_PATH, mapping, OUT_GZ, REPORT)
    print('mapping applied. report:', report)

if __name__ == '__main__':
    main()
