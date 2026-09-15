#!/usr/bin/env bash
set -euo pipefail
runtime_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
repo_root="$(cd -- "$runtime_root/../../.." && pwd)"
predictor_root="$repo_root/lzx/tool/operation_predictor"
checkpoint="${VISIT_CHECKPOINT:-$predictor_root/outputs/lsapp_expanded/visit_window_v1/app_lstm_visit_window.pt}"
exec "${PYTHON_BIN:-python3}" "$runtime_root/monitor.py" \
  --enable-online-lstm --lstm-model-type visit_window \
  --lstm-checkpoint "$checkpoint" \
  --app-vocab "$predictor_root/data/vocab/lsapp_expanded/app_vocab_duration.json" \
  --group-vocab "$predictor_root/data/vocab/lsapp_expanded/user_group_vocab.json" \
  --app-scope-config "$repo_root/lzx/service/configs/runtime/runtime_app_scope.service.json" \
  --config "$repo_root/lzx/service/configs/runtime/config.yaml" \
  --app-mapping "$repo_root/lzx/service/configs/runtime/app_mapping.json" \
  --sample-interval 1 --prediction-ttl-s 30 --periodic-refresh-s 30 \
  --score-mode sigmoid --parp-myfs-mode off --parp-bridge-mode off \
  --foreground-backend desktop "$@"
