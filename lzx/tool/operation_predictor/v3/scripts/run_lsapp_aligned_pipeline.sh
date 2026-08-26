#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

if [[ -x .venv-wsl/bin/python ]]; then
  PYTHON_BIN="${PYTHON_BIN:-.venv-wsl/bin/python}"
else
  PYTHON_BIN="${PYTHON_BIN:-python3}"
fi

SOURCE="data/test1/raw/datasets/LSApp/extracted/lsapp.tsv" # lzx-note
MAPPING="data/lsapp_aligned/mapping/lsapp_to_linux.json"
VOCAB="data/vocab/lsapp_aligned/app_vocab.json"
DURATION_VOCAB="data/vocab/lsapp_aligned/app_vocab_duration.json"
GROUP_VOCAB="data/vocab/lsapp_aligned/user_group_vocab.json"
MAPPED="data/lsapp_aligned/raw/lsapp_mapped.tsv.gz"
EVENTS="data/lsapp_aligned/raw/app_events.csv"
DATASET="data/lsapp_aligned/processed/app_lstm_duration_switch"
REPORT="data/lsapp_aligned/reports/mapping_report.json"
OUTPUT_DIR="outputs/lsapp_aligned/checkpoints"
RESULT="outputs/lsapp_aligned/results/app_lstm_switch_v3.json"

[[ -f "$SOURCE" ]] || { echo "missing LSAPP source: $SOURCE" >&2; exit 1; }
"$PYTHON_BIN" -c 'import torch' >/dev/null 2>&1 || {
  echo "PyTorch is required for v3 training." >&2
  exit 1
}

echo "[1/4] Apply audited LSAPP -> login-free Linux mapping"
"$PYTHON_BIN" scripts/tools/data/lsapp/apply_functional_mapping.py \
  --input "$SOURCE" --mapping "$MAPPING" --app-vocab "$VOCAB" \
  --output "$MAPPED" --report "$REPORT"

echo "[2/4] Build causal opened-app event stream"
"$PYTHON_BIN" scripts/tools/data/lsapp/prepare_lsapp_app_events.py \
  --input "$MAPPED" --output "$EVENTS" --app-vocab "$DURATION_VOCAB" \
  --user-group "通用用户" --strict

echo "[3/4] Build duration-aware, next-switch dataset"
"$PYTHON_BIN" v3/src/data/build_app_dataset_duration.py \
  --source-file "$EVENTS" --app-vocab "$DURATION_VOCAB" \
  --group-vocab "$GROUP_VOCAB" --history-len 5 --duration-cap-s 600 \
  --max-session-gap-s 3600 --periodic-anchor-s 180 --output-dir "$DATASET"

echo "[4/4] Train and evaluate held-out next-switch model"
"$PYTHON_BIN" v3/train/train_app_lstm_switch.py \
  --dataset-dir "$DATASET" --app-vocab "$DURATION_VOCAB" \
  --group-vocab "$GROUP_VOCAB" --epochs "${EPOCHS:-20}" \
  --batch-size "${BATCH_SIZE:-2048}" --class-weight-mode inverse-sqrt \
  --output-dir "$OUTPUT_DIR" --checkpoint-name app_lstm_switch_v3.pt \
  --output "$RESULT"

echo "LSAPP-aligned v3 pipeline finished."
echo "checkpoint=$OUTPUT_DIR/app_lstm_switch_v3.pt"
echo "result=$RESULT"
