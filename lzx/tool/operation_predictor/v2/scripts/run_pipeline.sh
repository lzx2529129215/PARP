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

mkdir -p data/raw data/vocab data/processed outputs/results/v2 outputs/checkpoints/v2

"$PYTHON_BIN" -c "import torch" >/dev/null 2>&1 || {
  echo "PyTorch is required for v2 app LSTM. Install it first: pip install -r requirements.txt" >&2
  exit 1
}

echo "[1/7] Generate synthetic data"
"$PYTHON_BIN" scripts/data/make_synthetic_data.py \
  --num-users 10 \
  --hours 2 \
  --seed 42 \
  --output-app data/raw/app_events.csv \
  --output-op data/raw/op_events.csv

echo "[2/7] Build app samples"
"$PYTHON_BIN" src/data/build_app_dataset.py \
  --input data/raw/app_events.csv \
  --app-vocab data/vocab/app_vocab.json \
  --group-vocab data/vocab/user_group_vocab.json \
  --output data/processed/app_samples.pkl \
  --history-len 5 \
  --horizons 3 5 10

echo "[3/7] Build op samples"
"$PYTHON_BIN" src/data/build_op_dataset.py \
  --input data/raw/op_events.csv \
  --op-vocab data/vocab/op_vocab.json \
  --group-vocab data/vocab/user_group_vocab.json \
  --output data/processed/op_samples.pkl \
  --history-len 4

echo "[4/7] Split app samples"
"$PYTHON_BIN" src/data/split_dataset.py \
  --input data/processed/app_samples.pkl \
  --task app \
  --output-dir data/processed

echo "[5/7] Split op samples"
"$PYTHON_BIN" src/data/split_dataset.py \
  --input data/processed/op_samples.pkl \
  --task op \
  --output-dir data/processed

echo "[6/7] Train/validate app LSTM v2"
"$PYTHON_BIN" v2/train/train_app_lstm.py \
  --train data/processed/train_app.pkl \
  --val data/processed/val_app.pkl \
  --epochs 20 \
  --batch-size 32 \
  --top-k 1 3 5 \
  --output outputs/results/v2/app_lstm_val_results.csv \
  --checkpoint outputs/checkpoints/v2/app_lstm.pt

echo "[7/7] Train/evaluate op Markov v1"
"$PYTHON_BIN" v2/train/train_op_markov.py \
  --train data/processed/train_op.pkl \
  --test data/processed/test_op.pkl \
  --order 4 \
  --top-k 1 3 5 \
  --output outputs/results/v2/op_markov_results.csv

echo "V2 app LSTM pipeline finished."
