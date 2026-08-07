set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../../" && pwd)"
cd "$ROOT_DIR"

if command -v python3 >/dev/null 2>&1; then
  PYTHON_BIN="${PYTHON_BIN:-python3}"
else
  PYTHON_BIN="${PYTHON_BIN:-python}"
fi

# #模拟raw原始数据
# "$PYTHON_BIN" scripts/data/make_synthetic_data.py \
#   --num-users 10 \
#   --hours 96 \
#   --seed 42 \
#   --output-app data/raw/app_events.csv \
#   --output-op data/raw/op_events.csv

# #划分生成的原始数据为训练/测试集
# "$PYTHON_BIN" src/data/build_app_dataset.py \
#     --input data/raw/app_events.csv \
#     --app-vocab data/vocab/app_vocab.json \
#     --group-vocab data/vocab/user_group_vocab.json \
#     --output data/processed/app_samples.pkl \
#     --history-len 6 \
#     --horizons 3 5 10

#划分生成的原始数据为训练/测试集
"$PYTHON_BIN" src/data/build_app_dataset.py \
    --input data/datasets/LSApp/after_mapping/add_opened_apps/lsapp_mapped_with_opened.tsv.gz \
    --app-vocab data/vocab/app_vocab.json \
    --group-vocab data/vocab/user_group_vocab.json \
    --output data/processed/app_samples.pkl \
    --history-len 6 \
    --horizons 3 5 10


"$PYTHON_BIN" src/data/split_dataset.py \
  --input data/processed/app_samples.pkl \
  --task app \
  --output-dir data/processed