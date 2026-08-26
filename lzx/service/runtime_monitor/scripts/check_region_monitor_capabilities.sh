#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

echo "== Region Monitor capability check =="
python3 -m runtime_monitor.region_monitor.capability_probe
status=$?
if [[ "$status" -eq 0 ]]; then
  echo "PASS: capability probe completed. If status is SUPPORTED_NEEDS_ROOT, run smoke with sudo on the host."
else
  echo "FAIL: DAMON region monitor cannot start in this environment. See JSON above."
fi
exit "$status"

