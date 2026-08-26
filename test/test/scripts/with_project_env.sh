#!/usr/bin/env bash
set -euo pipefail

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ ! -x "$root_dir/.venv/bin/python" ]]; then
  echo "Project virtual environment is missing. Run: python3 -m venv .venv && .venv/bin/pip install -e ." >&2
  exit 2
fi
export PATH="$root_dir/.venv/bin:$PATH"
exec "$@"
