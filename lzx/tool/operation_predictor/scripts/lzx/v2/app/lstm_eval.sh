#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../../" && pwd)"
cd "$ROOT_DIR"

if [ -x ".venv-wsl/bin/python" ]; then
  PYTHON_BIN="${PYTHON_BIN:-.venv-wsl/bin/python}"
elif command -v python3 >/dev/null 2>&1; then
  PYTHON_BIN="${PYTHON_BIN:-python3}"
else
  PYTHON_BIN="${PYTHON_BIN:-python}"
fi

"$PYTHON_BIN" v2/eval/eval_app_lstm.py \
  --checkpoint outputs/checkpoints/app_lstm/lsapp_app_lstm.pt \
  --test data/processed/lsapp/test_app.pkl \
  --batch-size "${BATCH_SIZE:-4096}" \
  --top-k 1 3 5 \
  --output outputs/results/v2/lsapp_app_lstm_eval_results.csv
