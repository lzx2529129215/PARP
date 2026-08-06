#!/usr/bin/env python3
"""Train a first-order Markov model from session_opened_apps.tsv.

Outputs:
 - markov_model_v1.json : {prev_app: {next_app: prob, ...}, ...}
 - markov_counts_v1.json: {prev_app: {next_app: count, ...}, ...}
 - markov_stats.json: summary stats
"""
import os
import json
import gzip
from collections import defaultdict, Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / 'data' / 'datasets' / 'LSApp'
CANDIDATES = [
    DATA_DIR / 'after_mapping' / 'add_opened_apps' / 'session_opened_apps.tsv',
    DATA_DIR / 'after_mapping' / 'session_opened_apps.tsv',
    DATA_DIR / 'session_opened_apps.tsv',
]
OUT_DIR = DATA_DIR / 'models'
OUT_DIR.mkdir(parents=True, exist_ok=True)
OUT_MODEL = OUT_DIR / 'markov_model_v1.json'
OUT_COUNTS = OUT_DIR / 'markov_counts_v1.json'
OUT_STATS = OUT_DIR / 'markov_stats.json'


def find_session_file():
    for p in CANDIDATES:
        if p.exists():
            return p
    raise FileNotFoundError('session_opened_apps.tsv not found in candidates')


def load_sequences(path):
    # stream read TSV, parse JSON list in third column
    with open(path, 'r', encoding='utf-8', errors='replace') as f:
        header = f.readline().rstrip('\n\r')
        cols = header.split('\t')
        # assume opened_apps is the third column
        opened_idx = 2 if len(cols) >= 3 else (cols.index('opened_apps') if 'opened_apps' in cols else 2)
        for line in f:
            parts = line.rstrip('\n\r').split('\t')
            if len(parts) <= opened_idx: continue
            js = parts[opened_idx]
            try:
                seq = json.loads(js)
            except Exception:
                # fallback: try strip quotes
                try:
                    seq = json.loads(js.replace("'", '"'))
                except Exception:
                    continue
            yield seq


def train_markov(session_tsv):
    counts = defaultdict(Counter)
    total_seqs = 0
    total_transitions = 0
    max_state = None
    for seq in load_sequences(session_tsv):
        if not seq or len(seq) < 2:
            continue
        total_seqs += 1
        # optional: remove consecutive duplicates already done earlier, but ensure here
        seq2 = [s for i,s in enumerate(seq) if i==0 or s!=seq[i-1]]
        for a,b in zip(seq2, seq2[1:]):
            counts[a][b] += 1
            total_transitions += 1
    # normalize
    model = {}
    for a, ctr in counts.items():
        total = float(sum(ctr.values()))
        model[a] = {b: c/total for b,c in ctr.items()}
    stats = {
        'total_sequences': total_seqs,
        'total_transitions': total_transitions,
        'num_states': len(counts)
    }
    return model, counts, stats


def main():
    session_file = find_session_file()
    print('Using session file:', session_file)
    model, counts, stats = train_markov(session_file)
    with open(OUT_MODEL, 'w', encoding='utf-8') as f:
        json.dump(model, f, ensure_ascii=False)
    # convert counts to normal dicts
    counts_out = {a: dict(ctr) for a,ctr in counts.items()}
    with open(OUT_COUNTS, 'w', encoding='utf-8') as f:
        json.dump(counts_out, f, ensure_ascii=False)
    with open(OUT_STATS, 'w', encoding='utf-8') as f:
        json.dump(stats, f, ensure_ascii=False)
    print('Saved model to', OUT_MODEL)
    print('Stats:', stats)

if __name__ == '__main__':
    main()
