#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
PRED="$ROOT/operation_predictor"
SESSION_ID="${SESSION_ID:-test1_runtime_$(date +%Y%m%d_%H%M%S)}"
MONITOR_DURATION="${MONITOR_DURATION:-240}"
SCENARIO_PATH="${SCENARIO_PATH:-$ROOT/configs/automation/scenario_test1_app_switch_gnome_search.json}"
SCENARIO_ID="${SCENARIO_ID:-test1_app_switch_gnome_search}"
SESSION_DIR="$ROOT/outputs/runtime_monitor/$SESSION_ID"
MONITOR_LOG="$SESSION_DIR/review/runtime_monitor.log"
AUTOMATION_LOG="$SESSION_DIR/review/automation.log"
TRACE="$SESSION_DIR/model/automation_trace.csv"

export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
export DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-unix:path=$XDG_RUNTIME_DIR/bus}"
export DISPLAY="${DISPLAY:-:0}"
export WAYLAND_DISPLAY="${WAYLAND_DISPLAY:-wayland-0}"
export XAUTHORITY="${XAUTHORITY:-$(ls -t "$XDG_RUNTIME_DIR"/.mutter-Xwaylandauth.* 2>/dev/null | head -1 || true)}"
export GDK_BACKEND="${GDK_BACKEND:-x11}"

mkdir -p "$SESSION_DIR/model" "$SESSION_DIR/review"

echo "session_id=$SESSION_ID"
echo "display=$DISPLAY"
echo "xauthority=$XAUTHORITY"
echo "monitor_duration=$MONITOR_DURATION"

python3 "$ROOT/runtime_monitor/monitor.py" \
  --config "$ROOT/configs/runtime/config.yaml" \
  --app-scope-config "$ROOT/configs/runtime/test1_runtime_app_scope.json" \
  --app-mapping "$ROOT/configs/runtime/test1_app_mapping.json" \
  --app-vocab "$PRED/data/vocab/test1/app_vocab.json" \
  --group-vocab "$PRED/data/vocab/test1/user_group_vocab.json" \
  --target-apps FIREFOX,LIBREOFFICE,VLC,GIMP,AUDACITY,THUNDERBIRD,TELEGRAM,EVINCE,FILES,CALCULATOR \
  --foreground-backend x11 \
  --sample-interval 1 \
  --duration "$MONITOR_DURATION" \
  --disable-ebpf \
  --output-dir "$ROOT/outputs/runtime_monitor" \
  --session-id "$SESSION_ID" \
  >"$MONITOR_LOG" 2>&1 &
MONITOR_PID=$!

cleanup() {
  if kill -0 "$MONITOR_PID" 2>/dev/null; then
    kill -INT "$MONITOR_PID" 2>/dev/null || true
    wait "$MONITOR_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT

sleep 3
set +e
bash "$ROOT/automation/run_automation.sh" \
  --scenario "$SCENARIO_PATH" \
  --session-id "$SESSION_ID" \
  --scenario-id "$SCENARIO_ID" \
  --trace-output "$TRACE" \
  --test-slice huawei-test.slice \
  >"$AUTOMATION_LOG" 2>&1
AUTOMATION_RC=$?
set -e

wait "$MONITOR_PID" || MONITOR_RC=$?
MONITOR_RC=${MONITOR_RC:-0}
trap - EXIT

python3 "$ROOT/runtime_monitor/scripts/run_test1_app_prediction.py" \
  --session-dir "$SESSION_DIR" \
  --checkpoint "$PRED/outputs/test1/checkpoints/lsapp_app_lstm.pt" \
  --app-vocab "$PRED/data/vocab/test1/app_vocab.json" \
  --group-vocab "$PRED/data/vocab/test1/user_group_vocab.json" \
  --scope-config "$ROOT/configs/runtime/test1_runtime_app_scope.json" \
  --device cpu \
  >"$SESSION_DIR/review/test1_app_prediction.log" 2>&1

printf 'session_id=%s\nautomation_rc=%s\nmonitor_rc=%s\nsession_dir=%s\n' \
  "$SESSION_ID" "$AUTOMATION_RC" "$MONITOR_RC" "$SESSION_DIR" \
  | tee "$SESSION_DIR/review/test1_collection_summary.txt"
