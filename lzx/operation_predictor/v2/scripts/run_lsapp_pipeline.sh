#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

if [ -x ".venv-wsl/bin/python" ]; then
  PYTHON_BIN="${PYTHON_BIN:-.venv-wsl/bin/python}"
elif command -v python3 >/dev/null 2>&1; then
  PYTHON_BIN="${PYTHON_BIN:-python3}"
else
  PYTHON_BIN="${PYTHON_BIN:-python}"
fi

mkdir -p data/raw/lsapp data/processed/lsapp outputs/results/v2 outputs/checkpoints/v2

"$PYTHON_BIN" -c "import torch" >/dev/null 2>&1 || {
  echo "PyTorch is required for v2 app LSTM. Install it first: pip install -r requirements.txt" >&2
  exit 1
}

echo "[1/5] Prepare LSApp app events"
"$PYTHON_BIN" scripts/tools/prepare_lsapp_app_events.py \
  --input data/raw/datasets/LSApp/after_mapping/add_opened_apps/lsapp_mapped_with_opened.clean.tsv \
  --output data/raw/lsapp/app_events.csv \
  --app-vocab data/vocab/app_vocab.json

echo "[2/5] Build LSApp app samples"
"$PYTHON_BIN" src/data/build_app_dataset.py \
  --input data/raw/lsapp/app_events.csv \
  --app-vocab data/vocab/app_vocab.json \
  --group-vocab data/vocab/user_group_vocab.json \
  --output data/processed/lsapp/app_samples.pkl \
  --history-len 5 \
  --horizons 3 5 10

echo "[3/5] Split LSApp app samples"
"$PYTHON_BIN" src/data/split_dataset.py \
  --input data/processed/lsapp/app_samples.pkl \
  --task app \
  --output-dir data/processed/lsapp

echo "[4/5] Train/validate LSApp app LSTM v2"
"$PYTHON_BIN" v2/train/train_app_lstm.py \
  --train data/processed/lsapp/train_app.pkl \
  --val data/processed/lsapp/val_app.pkl \
  --epochs "${EPOCHS:-20}" \
  --batch-size "${BATCH_SIZE:-32}" \
  --top-k 1 3 5 \
  --output outputs/results/v2/lsapp_app_lstm_val_results.csv \
  --checkpoint outputs/checkpoints/v2/lsapp_app_lstm.pt

echo "[5/5] Done"
echo "LSApp v2 app LSTM pipeline finished."
