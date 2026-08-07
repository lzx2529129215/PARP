#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

python3 runtime_monitor/monitor.py \
  --config configs/runtime/config.yaml \
  --target-app WPS \
  --sample-interval 1 \
  --output-dir outputs/runtime_monitor \
  --path-mode hash \
  "$@"

