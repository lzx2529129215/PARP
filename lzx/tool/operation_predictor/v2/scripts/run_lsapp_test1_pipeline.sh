#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

if [ -x ".venv-wsl/bin/python" ]; then
  PYTHON_BIN="${PYTHON_BIN:-.venv-wsl/bin/python}"
else
  PYTHON_BIN="${PYTHON_BIN:-python3}"
fi

VOCAB="data/vocab/test1/app_vocab.json"
GROUP_VOCAB="data/vocab/test1/user_group_vocab.json"
RAW_SOURCE="data/test1/raw/datasets/LSApp/extracted/lsapp.tsv"
MAPPING="data/test1/mapping/lsapp_to_linux_test1.json"
MAPPED="data/test1/raw/lsapp/lsapp_test1_mapped.tsv.gz"
MAPPING_REPORT="data/test1/reports/mapping_apply_report.json"
EVENTS="data/test1/raw/lsapp/app_events.csv"
PROCESSED="data/test1/processed/lsapp"
RESULT="outputs/test1/results/lsapp_app_lstm_val_results.csv"
CHECKPOINT="outputs/test1/checkpoints/lsapp_app_lstm.pt"

mkdir -p "$(dirname "$MAPPED")" "$(dirname "$MAPPING_REPORT")" "$PROCESSED" \
  "$(dirname "$RESULT")" "$(dirname "$CHECKPOINT")"

"$PYTHON_BIN" -c "import torch" >/dev/null 2>&1 || {
  echo "PyTorch is required for v2 app LSTM." >&2
  exit 1
}

echo "[1/6] Apply functional LSApp -> Linux mapping"
"$PYTHON_BIN" scripts/tools/data/lsapp/apply_functional_mapping.py \
  --input "$RAW_SOURCE" \
  --mapping "$MAPPING" \
  --app-vocab "$VOCAB" \
  --output "$MAPPED" \
  --report "$MAPPING_REPORT"

echo "[2/6] Prepare mapped LSApp app events"
"$PYTHON_BIN" scripts/tools/data/lsapp/prepare_lsapp_app_events.py \
  --input "$MAPPED" \
  --output "$EVENTS" \
  --app-vocab "$VOCAB" \
  --user-group "通用用户"

echo "[3/6] Build test1 app samples"
"$PYTHON_BIN" src/data/build_app_dataset.py \
  --input "$EVENTS" \
  --app-vocab "$VOCAB" \
  --group-vocab "$GROUP_VOCAB" \
  --output "$PROCESSED/app_samples.pkl" \
  --history-len 5 \
  --horizons 3 5 10

echo "[4/6] Split test1 app samples"
"$PYTHON_BIN" src/data/split_dataset.py \
  --input "$PROCESSED/app_samples.pkl" \
  --task app \
  --output-dir "$PROCESSED"

echo "[5/6] Train test1 app LSTM"
"$PYTHON_BIN" v2/train/train_app_lstm.py \
  --train "$PROCESSED/train_app.pkl" \
  --val "$PROCESSED/val_app.pkl" \
  --app-vocab "$VOCAB" \
  --group-vocab "$GROUP_VOCAB" \
  --epochs "${EPOCHS:-20}" \
  --batch-size "${BATCH_SIZE:-256}" \
  --top-k 1 3 5 \
  --output "$RESULT" \
  --checkpoint "$CHECKPOINT"

echo "[6/6] Evaluate held-out test split"
"$PYTHON_BIN" v2/eval/eval_app_lstm.py \
  --checkpoint "$CHECKPOINT" \
  --test "$PROCESSED/test_app.pkl" \
  --app-vocab "$VOCAB" \
  --top-k 1 3 5 \
  --batch-size "${EVAL_BATCH_SIZE:-2048}" \
  --output "outputs/test1/results/lsapp_app_lstm_test_results.csv"

echo "test1 LSApp pipeline finished."
