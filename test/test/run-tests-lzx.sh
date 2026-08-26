#!/usr/bin/env bash
set -euo pipefail

# lzx-note: Anchor discovery at the independent metrics package after the repo move.
test_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$test_root"
python3 -m unittest discover -s tests -p 'test_*.py' "$@"
exec python3 test-parp-acceptance-lzx.py "$@"
