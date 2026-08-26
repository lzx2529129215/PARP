#!/usr/bin/env bash
set -euo pipefail

source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/layout.sh"

# lzx-note: One immutable session per service start keeps boot/restart evidence separate.
boot_id="$(tr -d '-' </proc/sys/kernel/random/boot_id | cut -c1-12)"
session_id="service_${boot_id}_$(date +%Y%m%d_%H%M%S)"
scope_config="${PARP_SERVICE_SCOPE_CONFIG:-$PARP_RUNTIME_CONFIG_ROOT/runtime_app_scope.service.json}"
sample_interval="${PARP_SERVICE_SAMPLE_INTERVAL:-1.0}"
foreground_backend="${PARP_SERVICE_FOREGROUND_BACKEND:-manual}"
history_window="${PARP_SERVICE_HISTORY_WINDOW:-64}"
session_duration="${PARP_SERVICE_SESSION_DURATION:-86400}"
retention_sessions="${PARP_SERVICE_RETENTION_SESSIONS:-7}"
retention_bytes="${PARP_SERVICE_RETENTION_BYTES:-4294967296}"
min_free_bytes="${PARP_SERVICE_MIN_FREE_BYTES:-5368709120}"
storage_check_interval="${PARP_SERVICE_STORAGE_CHECK_INTERVAL:-60}"
app_vocab="${PARP_SERVICE_APP_VOCAB:-$PARP_OPERATION_PREDICTOR_ROOT/data/vocab/lsapp_expanded/app_vocab_duration.json}"
group_vocab="${PARP_SERVICE_GROUP_VOCAB:-$PARP_OPERATION_PREDICTOR_ROOT/data/vocab/lsapp_expanded/user_group_vocab.json}"

mkdir -p "$PARP_SERVICE_OUTPUT_ROOT"
# lzx-note: Rotate resident collection into daily sessions and remove only
# validated service_* sessions.  Test outputs live under PARP_TEST_OUTPUT_ROOT
# and are deliberately outside this retention boundary.
python3 "$PARP_RUNTIME_ROOT/scripts/prune_service_outputs.py" \
  --output-root "$PARP_SERVICE_OUTPUT_ROOT" \
  --max-sessions "$retention_sessions" \
  --reserve-sessions 1 \
  --max-bytes "$retention_bytes" \
  --min-free-bytes "$min_free_bytes"

monitor_args=(
  --config "$PARP_RUNTIME_CONFIG_ROOT/config.yaml"
  --app-scope-config "$scope_config"
  --app-mapping "$PARP_RUNTIME_CONFIG_ROOT/app_mapping.json"
  --app-vocab "$app_vocab"
  --group-vocab "$group_vocab"
  --output-dir "$PARP_SERVICE_OUTPUT_ROOT"
  --session-id "$session_id"
  --sample-interval "$sample_interval"
  --duration "$session_duration"
  --history-window "$history_window"
  --max-output-root-bytes "$retention_bytes"
  --min-output-free-bytes "$min_free_bytes"
  --storage-check-interval "$storage_check_interval"
  --foreground-backend "$foreground_backend"
  --path-mode hash
  --disable-ebpf
  --label SERVICE_BOOT
)

# lzx-note: Boot-time manual mode must remain headless-safe; X11 mode adds the
# foreground-dependent memory observer only when explicitly requested.
if [[ "$foreground_backend" == "x11" ]]; then
  monitor_args+=(
    --direct-x11-events
    --enable-memory-shadow
    --memory-shadow-interval-s "$sample_interval"
  )
fi

exec python3 "$PARP_RUNTIME_ROOT/monitor.py" "${monitor_args[@]}"
