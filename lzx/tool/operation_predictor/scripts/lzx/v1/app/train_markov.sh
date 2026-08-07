set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
cd "$ROOT_DIR"

if command -v python3 >/dev/null 2>&1; then
  PYTHON_BIN="${PYTHON_BIN:-python3}"
else
  PYTHON_BIN="${PYTHON_BIN:-python}"
fi


"$PYTHON_BIN" v1/train/train_app_markov.py \
  --train data/processed/train_app.pkl \
  --test data/processed/test_app.pkl \
  --order 4 \
  --top-k 1 3 5 \
  --output outputs/results/v1/app_markov_results.csv
