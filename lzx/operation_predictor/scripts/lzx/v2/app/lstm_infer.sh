set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../../" && pwd)"
cd "$ROOT_DIR"


.venv-wsl/bin/python v2/infer/infer_app_lstm.py \
  --checkpoint outputs/checkpoints/v2/lsapp_app_lstm.pt \
  --history-apps 飞书 华为浏览器 腾讯QQ 华为浏览器 夸克浏览器 \
  --opened-apps 飞书 夸克浏览器 \
  --timestamp "2018-01-16 06:26:26" \
  --user-group 通用用户 \
  --top-k 5 \
  --score-mode softmax
# --score-mode sigmoid