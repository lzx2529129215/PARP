#!/usr/bin/env bash
set -euo pipefail

# Run test1 in an isolated X11 desktop nested inside the user's current
# Wayland session. This keeps the experiment reproducible without logging the
# user out, while retaining real X11 focus/minimize/restore/close semantics.

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
PRED="$ROOT/operation_predictor"
SESSION_ID="${SESSION_ID:-test1_nested_x11_$(date +%Y%m%d_%H%M%S)}"
MONITOR_DURATION="${MONITOR_DURATION:-180}"
SAMPLE_INTERVAL="${SAMPLE_INTERVAL:-0.25}"
SCENARIO_PATH="${SCENARIO_PATH:-$ROOT/configs/automation/scenario_test1_app_switch.json}"
SCENARIO_ID="${SCENARIO_ID:-test1_nested_x11_app_switch}"
NESTED_DISPLAY="${NESTED_DISPLAY:-:3}"
NESTED_SCREEN="${NESTED_SCREEN:-1280x800}"
HOST_DISPLAY="${HOST_DISPLAY:-:0}"
HOST_XAUTHORITY="${HOST_XAUTHORITY:-$(ls -t /run/user/$(id -u)/.mutter-Xwaylandauth.* 2>/dev/null | head -1 || true)}"
SESSION_DIR="$ROOT/outputs/runtime_monitor/$SESSION_ID"
MONITOR_LOG="$SESSION_DIR/review/runtime_monitor.log"
AUTOMATION_LOG="$SESSION_DIR/review/automation.log"
TRACE="$SESSION_DIR/model/automation_trace.csv"

display_number="${NESTED_DISPLAY#:}"
while [ -S "/tmp/.X11-unix/X${display_number}" ] || [ -f "/tmp/.X${display_number}-lock" ]; do
  display_number=$((display_number + 1))
  NESTED_DISPLAY=":${display_number}"
done

unit_suffix="${SESSION_ID//[^a-zA-Z0-9_-]/-}"
XEPHYR_UNIT="test1-xephyr-${unit_suffix}.service"
OPENBOX_UNIT="test1-openbox-${unit_suffix}.service"
MONITOR_PID=""

mkdir -p "$SESSION_DIR/model" "$SESSION_DIR/review"

cleanup() {
  if [ -n "$MONITOR_PID" ] && kill -0 "$MONITOR_PID" 2>/dev/null; then
    kill -INT "$MONITOR_PID" 2>/dev/null || true
    wait "$MONITOR_PID" 2>/dev/null || true
  fi
  systemctl --user stop --no-block "$OPENBOX_UNIT" "$XEPHYR_UNIT" 2>/dev/null || true
}
trap cleanup EXIT

if [ -z "$HOST_XAUTHORITY" ]; then
  echo "无法找到宿主 GNOME Xwayland XAUTHORITY" >&2
  exit 1
fi

echo "session_id=$SESSION_ID"
echo "nested_display=$NESTED_DISPLAY"
echo "nested_screen=$NESTED_SCREEN"
echo "monitor_duration=$MONITOR_DURATION"
echo "sample_interval=$SAMPLE_INTERVAL"

systemd-run --user --unit="$XEPHYR_UNIT" --collect \
  --setenv=DISPLAY="$HOST_DISPLAY" \
  --setenv=XAUTHORITY="$HOST_XAUTHORITY" \
  Xephyr "$NESTED_DISPLAY" -screen "$NESTED_SCREEN" -ac -br -noreset

for _ in $(seq 1 30); do
  if DISPLAY="$NESTED_DISPLAY" XAUTHORITY=/dev/null xdpyinfo >/dev/null 2>&1; then
    break
  fi
  sleep 0.5
done
DISPLAY="$NESTED_DISPLAY" XAUTHORITY=/dev/null xdpyinfo >/dev/null

systemd-run --user --unit="$OPENBOX_UNIT" --collect \
  --setenv=DISPLAY="$NESTED_DISPLAY" \
  --setenv=XAUTHORITY=/dev/null \
  --setenv=GDK_BACKEND=x11 \
  openbox --sm-disable

for _ in $(seq 1 20); do
  if DISPLAY="$NESTED_DISPLAY" XAUTHORITY=/dev/null wmctrl -m 2>/dev/null | grep -q 'Name: Openbox'; then
    break
  fi
  sleep 0.25
done
DISPLAY="$NESTED_DISPLAY" XAUTHORITY=/dev/null wmctrl -m | grep -q 'Name: Openbox'

export DISPLAY="$NESTED_DISPLAY"
export XAUTHORITY=/dev/null
export WAYLAND_DISPLAY=""
export XDG_SESSION_TYPE=x11
export GDK_BACKEND=x11
export MOZ_ENABLE_WAYLAND=0

python3 "$ROOT/runtime_monitor/monitor.py" \
  --config "$ROOT/configs/runtime/config.yaml" \
  --app-scope-config "$ROOT/configs/runtime/test1_runtime_app_scope.json" \
  --app-mapping "$ROOT/configs/runtime/test1_app_mapping.json" \
  --app-vocab "$PRED/data/vocab/test1/app_vocab.json" \
  --group-vocab "$PRED/data/vocab/test1/user_group_vocab.json" \
  --target-apps FIREFOX,LIBREOFFICE,VLC,GIMP,AUDACITY,THUNDERBIRD,TELEGRAM,EVINCE,FILES,CALCULATOR \
  --foreground-backend x11 \
  --sample-interval "$SAMPLE_INTERVAL" \
  --duration "$MONITOR_DURATION" \
  --close-grace-windows 1 \
  --test-slice huawei-test.slice \
  --disable-ebpf \
  --output-dir "$ROOT/outputs/runtime_monitor" \
  --session-id "$SESSION_ID" \
  >"$MONITOR_LOG" 2>&1 &
MONITOR_PID=$!

sleep 3
set +e
bash "$ROOT/automation/run_automation.sh" \
  --scenario "$SCENARIO_PATH" \
  --display "$NESTED_DISPLAY" \
  --xauthority /dev/null \
  --session-id "$SESSION_ID" \
  --scenario-id "$SCENARIO_ID" \
  --trace-output "$TRACE" \
  --test-slice huawei-test.slice \
  >"$AUTOMATION_LOG" 2>&1
AUTOMATION_RC=$?
set -e

if [ -n "$MONITOR_PID" ] && kill -0 "$MONITOR_PID" 2>/dev/null; then
  kill -INT "$MONITOR_PID" 2>/dev/null || true
fi
wait "$MONITOR_PID" || MONITOR_RC=$?
MONITOR_RC=${MONITOR_RC:-0}
MONITOR_PID=""

python3 "$ROOT/runtime_monitor/scripts/run_test1_app_prediction.py" \
  --session-dir "$SESSION_DIR" \
  --checkpoint "$PRED/outputs/test1/checkpoints/lsapp_app_lstm.pt" \
  --app-vocab "$PRED/data/vocab/test1/app_vocab.json" \
  --group-vocab "$PRED/data/vocab/test1/user_group_vocab.json" \
  --scope-config "$ROOT/configs/runtime/test1_runtime_app_scope.json" \
  --device cpu \
  >"$SESSION_DIR/review/test1_app_prediction.log" 2>&1

set +e
python3 "$ROOT/runtime_monitor/scripts/verify_test1_event_coverage.py" \
  --session-dir "$SESSION_DIR" \
  >"$SESSION_DIR/review/event_coverage.log" 2>&1
COVERAGE_RC=$?
set -e

printf 'session_id=%s\nautomation_rc=%s\nmonitor_rc=%s\ncoverage_rc=%s\nnested_display=%s\nsession_dir=%s\n' \
  "$SESSION_ID" "$AUTOMATION_RC" "$MONITOR_RC" "$COVERAGE_RC" "$NESTED_DISPLAY" "$SESSION_DIR" \
  | tee "$SESSION_DIR/review/test1_collection_summary.txt"

if [ "$AUTOMATION_RC" -ne 0 ] || [ "$MONITOR_RC" -ne 0 ] || [ "$COVERAGE_RC" -ne 0 ]; then
  exit 1
fi
