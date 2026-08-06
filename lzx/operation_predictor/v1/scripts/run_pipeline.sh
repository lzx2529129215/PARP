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

mkdir -p data/raw data/vocab data/processed outputs/results/v1

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

echo "[6/7] Train/evaluate app Markov v1"
"$PYTHON_BIN" v1/train/train_app_markov.py \
  --train data/processed/train_app.pkl \
  --test data/processed/test_app.pkl \
  --order 4 \
  --top-k 1 3 5 \
  --output outputs/results/v1/app_markov_results.csv

echo "[7/7] Train/evaluate op Markov v1"
"$PYTHON_BIN" v1/train/train_op_markov.py \
  --train data/processed/train_op.pkl \
  --test data/processed/test_op.pkl \
  --order 4 \
  --top-k 1 3 5 \
  --output outputs/results/v1/op_markov_results.csv

echo "V1 Markov pipeline finished."
