#!/usr/bin/env bash
set -euo pipefail
predictor_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$predictor_root"
python_bin="${PYTHON_BIN:-python3}"
dataset_dir="${DATASET_DIR:-data/lsapp_expanded/processed/app_visit_window_v1}"
output_dir="${OUTPUT_DIR:-outputs/lsapp_expanded/visit_window_v1}"
# Reuse the existing mapped LSApp events and vocabularies; never overwrite them.
if [[ ! -f "$dataset_dir/dataset_meta.json" ]]; then
  "$python_bin" v3/src/data/build_app_dataset_visit_window.py --output-dir "$dataset_dir"
fi
"$python_bin" -u v3/train/train_app_lstm_visit_window.py \
  --dataset-dir "$dataset_dir" --output-dir "$output_dir" \
  --epochs "${EPOCHS:-20}" --batch-size "${BATCH_SIZE:-2048}" --threads "${THREADS:-2}"
