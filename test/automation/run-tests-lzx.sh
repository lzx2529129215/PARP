#!/usr/bin/env bash
set -euo pipefail

# lzx-note: Use a file entrypoint so Python's stdlib `test` package cannot
# shadow the repository's top-level test workspace after the directory move.
automation_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$automation_root/tests/test_semantic_automation.py" "$@"
