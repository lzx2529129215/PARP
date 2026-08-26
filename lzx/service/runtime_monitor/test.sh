#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
SESSION_ID="${SESSION_ID:-session_files_001}"

cd "$PROJECT_ROOT"

python3 runtime_monitor/monitor.py \
  --config configs/runtime/config.yaml \
  --app-scope-config configs/runtime/runtime_app_scope.json \
  --target-app WPS \
  --sample-interval 1 \
  --output-dir outputs/runtime_monitor \
  --path-mode hash \
  --session-id "$SESSION_ID"
