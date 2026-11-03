"""Verify the six unchanged userspace baseline files."""
import hashlib
import json
from pathlib import Path

root = Path(__file__).resolve().parent.parent
manifest = json.loads((root/'SOURCE_MANIFEST.json').read_text(encoding='utf-8'))
for row in manifest['unchanged_core']:
    actual = hashlib.sha256((root/row['path']).read_bytes()).hexdigest()
    if actual != row['sha256']:
        raise SystemExit('MISMATCH: '+row['path'])
print('UNCHANGED_USERSPACE_CORE_OK:', len(manifest['unchanged_core']))
