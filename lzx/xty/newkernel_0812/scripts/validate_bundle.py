#!/usr/bin/env python3
import hashlib
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[1]
overlay = root / 'source' / 'overlay'
sha_file = root / 'source' / 'overlay.sha256'
errors = 0

for raw in sha_file.read_text(encoding='utf-8').splitlines():
    if not raw.strip() or raw.startswith('SYMLINK  '):
        continue
    expected, rel = raw.split('  ', 1)
    path = overlay / rel
    if not path.exists():
        print(f'missing overlay file: {rel}')
        errors += 1
        continue
    h = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    got = h.hexdigest()
    if got != expected:
        print(f'sha256 mismatch: {rel} expected={expected} got={got}')
        errors += 1

print('OK' if errors == 0 else f'FAILED errors={errors}')
sys.exit(1 if errors else 0)
