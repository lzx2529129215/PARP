#!/usr/bin/env bash
set -euo pipefail

source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/layout.sh"

# lzx-note: One immutable session per service start keeps boot/restart evidence separate.
boot_id="$(tr -d '-' </proc/sys/kernel/random/boot_id | cut -c1-12)"
session_id="service_${boot_id}_$(date +%Y%m%d_%H%M%S)"
scope_config="${PARP_SERVICE_SCOPE_CONFIG:-$PARP_RUNTIME_CONFIG_ROOT/runtime_app_scope.service.json}"
sample_interval="${PARP_SERVICE_SAMPLE_INTERVAL:-1.0}"
foreground_backend="${PARP_SERVICE_FOREGROUND_BACKEND:-desktop}" # lzx-note
history_window="${PARP_SERVICE_HISTORY_WINDOW:-64}"
session_duration="${PARP_SERVICE_SESSION_DURATION:-86400}"
retention_sessions="${PARP_SERVICE_RETENTION_SESSIONS:-7}"
retention_bytes="${PARP_SERVICE_RETENTION_BYTES:-4294967296}"
min_free_bytes="${PARP_SERVICE_MIN_FREE_BYTES:-5368709120}"
storage_check_interval="${PARP_SERVICE_STORAGE_CHECK_INTERVAL:-60}"
app_vocab="${PARP_SERVICE_APP_VOCAB:-$PARP_OPERATION_PREDICTOR_ROOT/data/vocab/lsapp_expanded/app_vocab_duration.json}"
group_vocab="${PARP_SERVICE_GROUP_VOCAB:-$PARP_OPERATION_PREDICTOR_ROOT/data/vocab/lsapp_expanded/user_group_vocab.json}"
checkpoint="${PARP_SERVICE_LSTM_CHECKPOINT:-$PARP_OPERATION_PREDICTOR_ROOT/outputs/lsapp_expanded/checkpoints/app_lstm_switch_v3.pt}"
myfs_device="${PARP_SERVICE_MYFS_DEVICE:-/dev/myfs}"
myfs_mode="${PARP_SERVICE_MYFS_MODE:-apply}"
myfs_enabled="${PARP_SERVICE_ENABLE_MYFS:-1}" # lzx-note
process_event_source="${PARP_SERVICE_PROCESS_EVENT_SOURCE:-connector}"
process_connector_socket="${PARP_SERVICE_PROCESS_CONNECTOR_SOCKET:-/run/user/$(id -u)/parp-process-events.sock}"
process_connector_ready_timeout="${PARP_SERVICE_PROCESS_CONNECTOR_READY_TIMEOUT:-10}"
process_connector_stale_timeout="${PARP_SERVICE_PROCESS_CONNECTOR_STALE_TIMEOUT:-10}"
process_cgroup_routing="${PARP_SERVICE_PROCESS_CGROUP_ROUTING:-systemd}"
process_cgroup_route_timeout="${PARP_SERVICE_PROCESS_CGROUP_ROUTE_TIMEOUT:-2}"
file_event_source="${PARP_SERVICE_FILE_EVENT_SOURCE:-ebpf}"
file_event_socket="${PARP_SERVICE_FILE_EVENT_SOCKET:-/run/user/$(id -u)/parp-file-events.sock}"
file_event_control_socket="${PARP_SERVICE_FILE_EVENT_CONTROL_SOCKET:-/run/parp-file-events-$(id -u).sock}"
file_event_ready_timeout="${PARP_SERVICE_FILE_EVENT_READY_TIMEOUT:-15}"
file_event_stale_timeout="${PARP_SERVICE_FILE_EVENT_STALE_TIMEOUT:-10}"

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
  --enable-online-lstm
  --lstm-model-type v3
  --lstm-checkpoint "$checkpoint"
  --score-mode softmax
  --path-mode hash
  --label SERVICE_BOOT
)

case "$process_event_source" in
  connector)
    monitor_args+=(
      --process-event-source connector
      --process-connector-socket "$process_connector_socket"
      --process-connector-ready-timeout-s "$process_connector_ready_timeout"
      --process-connector-stale-timeout-s "$process_connector_stale_timeout"
      --require-process-connector
    )
    case "$process_cgroup_routing" in
      systemd)
        monitor_args+=(
          --process-cgroup-routing systemd
          --process-cgroup-route-timeout-s "$process_cgroup_route_timeout"
          --require-process-cgroup-routing
        )
        ;;
      off)
        monitor_args+=(--process-cgroup-routing off)
        ;;
      *)
        echo "invalid PARP_SERVICE_PROCESS_CGROUP_ROUTING=$process_cgroup_routing (expected systemd or off)" >&2
        exit 2
        ;;
    esac
    ;;
  procfs)
    monitor_args+=(--process-event-source procfs --disable-ebpf --process-cgroup-routing off)
    ;;
  *)
    echo "invalid PARP_SERVICE_PROCESS_EVENT_SOURCE=$process_event_source (expected connector or procfs)" >&2
    exit 2
    ;;
esac

case "$file_event_source" in
  ebpf)
    monitor_args+=(
      --file-event-source ebpf
      --file-event-socket "$file_event_socket"
      --file-event-control-socket "$file_event_control_socket"
      --file-event-ready-timeout-s "$file_event_ready_timeout"
      --file-event-stale-timeout-s "$file_event_stale_timeout"
      --require-ebpf-file-events
    )
    ;;
  off)
    monitor_args+=(--file-event-source off)
    ;;
  *)
    echo "invalid PARP_SERVICE_FILE_EVENT_SOURCE=$file_event_source (expected ebpf or off)" >&2
    exit 2
    ;;
esac

# Keep collection and online LSTM inference resident for a Native comparison,
# while deliberately removing only the kernel prediction sink.  The default
# remains the production /dev/myfs APPLY path. lzx-note
case "$myfs_enabled" in
  1)
    monitor_args+=(
      --enable-parp-myfs
      --parp-myfs-device "$myfs_device"
      --parp-myfs-mode "$myfs_mode"
    )
    ;;
  0)
    monitor_args+=(--parp-myfs-mode off)
    ;;
  *)
    echo "invalid PARP_SERVICE_ENABLE_MYFS=$myfs_enabled (expected 0 or 1)" >&2
    exit 2
    ;;
esac

# lzx-note: The X11 collector reconnects after graphical login, so the service
# can remain resident from boot without permanently losing desktop events.
if [[ "$foreground_backend" == "x11" || "$foreground_backend" == "desktop" ]]; then
  monitor_args+=(
    --direct-x11-events
    --enable-memory-shadow
    --memory-shadow-interval-s "$sample_interval"
  )
fi

exec python3 "$PARP_RUNTIME_ROOT/monitor.py" "${monitor_args[@]}"
