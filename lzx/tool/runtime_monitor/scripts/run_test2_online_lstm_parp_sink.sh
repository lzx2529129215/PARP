#!/usr/bin/env bash
set -euo pipefail

# Test2: application events -> online test1 v3 app-probability LSTM -> PARP app_bind/app_prior.
# This script does not enable or exercise any reclaim/MGLRU consumer.

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
PRED="$ROOT/operation_predictor"
BRIDGE_MODE="${PARP_BRIDGE_MODE:-dry-run}"
PARP_TEST_GRANT_DEBUGFS_ACCESS="${PARP_TEST_GRANT_DEBUGFS_ACCESS:-0}"
PARP_DEBUGFS_ROOT="${PARP_DEBUGFS_ROOT:-/sys/kernel/debug/parp}"
PARP_BIND_CONFIG="${PARP_BIND_CONFIG:-}"
LSTM_MODEL_TYPE="${LSTM_MODEL_TYPE:-v3}"
LSTM_CHECKPOINT="${LSTM_CHECKPOINT:-$PRED/outputs/checkpoints/app_lstm_duration/lsapp_app_lstm_switch_v3.pt}"
LSTM_APP_VOCAB="${LSTM_APP_VOCAB:-$PRED/data/vocab/test1/app_vocab_duration.json}"
SESSION_ID="${SESSION_ID:-test2_online_lstm_parp_sink_$(date +%Y%m%d_%H%M%S)}"
MONITOR_DURATION="${MONITOR_DURATION:-180}"
SAMPLE_INTERVAL="${SAMPLE_INTERVAL:-0.25}"
POST_AUTOMATION_SETTLE_SECONDS="${POST_AUTOMATION_SETTLE_SECONDS:-5}"
NESTED_DISPLAY="${NESTED_DISPLAY:-:3}"
NESTED_SCREEN="${NESTED_SCREEN:-1280x800}"
HOST_DISPLAY="${HOST_DISPLAY:-:0}"
HOST_XAUTHORITY="${HOST_XAUTHORITY:-$(ls -t /run/user/$(id -u)/.mutter-Xwaylandauth.* 2>/dev/null | head -1 || true)}"
SCENARIO_PATH="${SCENARIO_PATH:-$ROOT/configs/automation/scenario_test1_app_switch.json}"
SCENARIO_ID="${SCENARIO_ID:-test2_online_lstm_parp_sink}"
# Kept off for Test2.  Test3's harness sets this to 1 and reuses the exact
# event/LSTM/bridge pipeline while adding only procfs/cgroup read observation.
TEST3_MEMORY_SHADOW="${TEST3_MEMORY_SHADOW:-0}"
TEST3_MEMORY_SHADOW_INTERVAL_S="${TEST3_MEMORY_SHADOW_INTERVAL_S:-0.25}"
TEST3_MEMORY_SHADOW_TOP_K="${TEST3_MEMORY_SHADOW_TOP_K:-3}"
TEST3_MEMORY_SHADOW_RECOVERY_WINDOW_S="${TEST3_MEMORY_SHADOW_RECOVERY_WINDOW_S:-3}"
TEST4_RECLAIM_CONTROLLER="${TEST4_RECLAIM_CONTROLLER:-0}"
TEST4_RECLAIM_MODE="${TEST4_RECLAIM_MODE:-shadow}"
TEST4_SKIP_TEST1_EVENT_COVERAGE="${TEST4_SKIP_TEST1_EVENT_COVERAGE:-0}"
RUNTIME_APP_SCOPE_CONFIG="${RUNTIME_APP_SCOPE_CONFIG:-$ROOT/configs/runtime/test1_runtime_app_scope.json}"
TEST_SLICE="${TEST_SLICE:-huawei-test.slice}"
TEST4B_BALLAST="${TEST4B_BALLAST:-0}"
TEST4B_BALLAST_CONFIG="${TEST4B_BALLAST_CONFIG:-}"
TEST4B_RECLAIM_MODE="${TEST4B_RECLAIM_MODE:-shadow}"
TEST4B_PROBABILITY_THRESHOLD="${TEST4B_PROBABILITY_THRESHOLD:-0.10}"
TEST4B_REQUIRED_LOW_PROBABILITY_BATCHES="${TEST4B_REQUIRED_LOW_PROBABILITY_BATCHES:-2}"
TEST4B_COLD_QUIET_S="${TEST4B_COLD_QUIET_S:-3}"
TEST4B_TARGET_HEADROOM_BYTES="${TEST4B_TARGET_HEADROOM_BYTES:-$((2500 * 1024 * 1024))}"
TEST4B_MINIMUM_HEADROOM_DEFICIT_BYTES="${TEST4B_MINIMUM_HEADROOM_DEFICIT_BYTES:-$((16 * 1024 * 1024))}"
TEST4B_HARD_MIN_AVAILABLE_BYTES="${TEST4B_HARD_MIN_AVAILABLE_BYTES:-$((512 * 1024 * 1024))}"
TEST4B_PSI_FULL_ABORT_AVG10="${TEST4B_PSI_FULL_ABORT_AVG10:-0.20}"
TEST4B_CONTROLLER_ACTIVATION_FILE="${TEST4B_CONTROLLER_ACTIVATION_FILE:-}"
TEST4B_APPLY_PREFLIGHT_READY="${TEST4B_APPLY_PREFLIGHT_READY:-0}"

while [ "$#" -gt 0 ]; do
  case "$1" in
    --parp-bridge-mode|--bridge-mode) BRIDGE_MODE="$2"; shift 2 ;;
    --parp-debugfs-root) PARP_DEBUGFS_ROOT="$2"; shift 2 ;;
    --parp-app-bind-config) PARP_BIND_CONFIG="$2"; shift 2 ;;
    --grant-parp-debugfs-access) PARP_TEST_GRANT_DEBUGFS_ACCESS=1; shift ;;
    --session-id) SESSION_ID="$2"; shift 2 ;;
    --duration) MONITOR_DURATION="$2"; shift 2 ;;
    --sample-interval) SAMPLE_INTERVAL="$2"; shift 2 ;;
    --post-automation-settle-seconds) POST_AUTOMATION_SETTLE_SECONDS="$2"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

case "$BRIDGE_MODE" in
  off|dry-run|shadow-write) ;;
  *) echo "invalid --parp-bridge-mode: $BRIDGE_MODE" >&2; exit 2 ;;
esac
case "$PARP_TEST_GRANT_DEBUGFS_ACCESS" in
  0|1) ;;
  *) echo "PARP_TEST_GRANT_DEBUGFS_ACCESS must be 0 or 1" >&2; exit 2 ;;
esac
case "$TEST3_MEMORY_SHADOW" in
  0|1) ;;
  *) echo "TEST3_MEMORY_SHADOW must be 0 or 1" >&2; exit 2 ;;
esac

SESSION_DIR="$ROOT/outputs/runtime_monitor/$SESSION_ID"
mkdir -p "$SESSION_DIR/model" "$SESSION_DIR/review" "$SESSION_DIR/parp" "$SESSION_DIR/logs"
MONITOR_LOG="$SESSION_DIR/logs/runtime_monitor.log"
AUTOMATION_LOG="$SESSION_DIR/logs/automation.log"
TRACE="$SESSION_DIR/model/automation_trace.csv"
MONITOR_PID=""
PARP_ACCESS_GRANTED=0
PARP_ACCESS_PATHS=()
PARP_ACCESS_OWNER_GROUP=()
PARP_ACCESS_MODE=()

display_number="${NESTED_DISPLAY#:}"
while [ -S "/tmp/.X11-unix/X${display_number}" ] || [ -f "/tmp/.X${display_number}-lock" ]; do
  display_number=$((display_number + 1))
  NESTED_DISPLAY=":${display_number}"
done
unit_suffix="${SESSION_ID//[^a-zA-Z0-9_-]/-}"
XEPHYR_UNIT="test2-xephyr-${unit_suffix}.service"
OPENBOX_UNIT="test2-openbox-${unit_suffix}.service"

grant_parp_debugfs_access() {
  [ "$BRIDGE_MODE" = "shadow-write" ] || return 0
  [ "$PARP_TEST_GRANT_DEBUGFS_ACCESS" = "1" ] || return 0
  if ! sudo -n true; then
    echo "shadow-write requires passwordless sudo to grant temporary PARP debugfs access" >&2
    return 1
  fi

  local test_group path owner_group mode
  test_group="$(id -g)"
  PARP_ACCESS_PATHS=(
    /sys/kernel/debug
    "$PARP_DEBUGFS_ROOT"
    "$PARP_DEBUGFS_ROOT/app_bind"
    "$PARP_DEBUGFS_ROOT/app_prior"
    "$PARP_DEBUGFS_ROOT/snapshot"
    "$PARP_DEBUGFS_ROOT/stats"
  )
  for path in "${PARP_ACCESS_PATHS[@]}"; do
    read -r owner_group mode < <(sudo stat -c '%u:%g %a' "$path")
    PARP_ACCESS_OWNER_GROUP+=("$owner_group")
    PARP_ACCESS_MODE+=("$mode")
  done

  # Permit only this desktop user's group to access the six nodes required by
  # Test2. cleanup restores every original owner and mode.
  sudo chown "0:${test_group}" "${PARP_ACCESS_PATHS[@]}"
  sudo chmod 0710 /sys/kernel/debug
  sudo chmod 0750 "$PARP_DEBUGFS_ROOT"
  sudo chmod 0220 "$PARP_DEBUGFS_ROOT/app_bind" "$PARP_DEBUGFS_ROOT/app_prior"
  sudo chmod 0440 "$PARP_DEBUGFS_ROOT/snapshot" "$PARP_DEBUGFS_ROOT/stats"
  PARP_ACCESS_GRANTED=1
}

restore_parp_debugfs_access() {
  [ "$PARP_ACCESS_GRANTED" = "1" ] || return 0
  local index
  for ((index=${#PARP_ACCESS_PATHS[@]} - 1; index >= 0; index--)); do
    sudo chown "${PARP_ACCESS_OWNER_GROUP[$index]}" "${PARP_ACCESS_PATHS[$index]}" 2>/dev/null || true
    sudo chmod "${PARP_ACCESS_MODE[$index]}" "${PARP_ACCESS_PATHS[$index]}" 2>/dev/null || true
  done
  PARP_ACCESS_GRANTED=0
}

cleanup() {
  if [ -n "$MONITOR_PID" ] && kill -0 "$MONITOR_PID" 2>/dev/null; then
    kill -INT "$MONITOR_PID" 2>/dev/null || true
    wait "$MONITOR_PID" 2>/dev/null || true
  fi
  systemctl --user stop --no-block "$OPENBOX_UNIT" "$XEPHYR_UNIT" 2>/dev/null || true
  restore_parp_debugfs_access
}
trap cleanup EXIT

if [ -z "$HOST_XAUTHORITY" ]; then
  echo "无法找到宿主 GNOME Xwayland XAUTHORITY" >&2
  exit 1
fi

grant_parp_debugfs_access
if [ "$PARP_ACCESS_GRANTED" = "1" ]; then
  cat "$PARP_DEBUGFS_ROOT/snapshot" >"$SESSION_DIR/parp/snapshot_before.txt"
fi

set +e
python3 "$ROOT/runtime_monitor/scripts/check_parp_lstm_bridge_preflight.py" \
  --parp-debugfs-root "$PARP_DEBUGFS_ROOT" \
  --model-version 401 \
  --schema-version parp-app-prior-v1 \
  >"$SESSION_DIR/kernel_facts.json" 2>"$SESSION_DIR/logs/preflight.log"
PREFLIGHT_RC=$?
set -e

{
  echo "session_id=$SESSION_ID"
  echo "bridge_mode=$BRIDGE_MODE"
  echo "parp_debugfs_root=$PARP_DEBUGFS_ROOT"
  echo "kernel=$(uname -r)"
  echo "monitor_duration=$MONITOR_DURATION"
  echo "sample_interval=$SAMPLE_INTERVAL"
  echo "post_automation_settle_seconds=$POST_AUTOMATION_SETTLE_SECONDS"
  echo "test_slice=$TEST_SLICE"
  echo "runtime_app_scope_config=$RUNTIME_APP_SCOPE_CONFIG"
  echo "parp_test_grant_debugfs_access=$PARP_TEST_GRANT_DEBUGFS_ACCESS"
  echo "parp_access_granted=$PARP_ACCESS_GRANTED"
} >"$SESSION_DIR/environment.txt"

if [ "$BRIDGE_MODE" = "shadow-write" ] && [ "$PREFLIGHT_RC" -ne 0 ]; then
  echo "PARP shadow-write preflight failed; refusing to run a fail-closed experiment" >&2
  exit 1
fi

systemd-run --user --unit="$XEPHYR_UNIT" --collect \
  --setenv=DISPLAY="$HOST_DISPLAY" --setenv=XAUTHORITY="$HOST_XAUTHORITY" \
  Xephyr "$NESTED_DISPLAY" -screen "$NESTED_SCREEN" -ac -br -noreset
for _ in $(seq 1 30); do
  DISPLAY="$NESTED_DISPLAY" XAUTHORITY=/dev/null xdpyinfo >/dev/null 2>&1 && break
  sleep 0.5
done
DISPLAY="$NESTED_DISPLAY" XAUTHORITY=/dev/null xdpyinfo >/dev/null

systemd-run --user --unit="$OPENBOX_UNIT" --collect \
  --setenv=DISPLAY="$NESTED_DISPLAY" --setenv=XAUTHORITY=/dev/null \
  --setenv=GDK_BACKEND=x11 openbox --sm-disable
for _ in $(seq 1 20); do
  DISPLAY="$NESTED_DISPLAY" XAUTHORITY=/dev/null wmctrl -m 2>/dev/null | grep -q 'Name: Openbox' && break
  sleep 0.25
done
DISPLAY="$NESTED_DISPLAY" XAUTHORITY=/dev/null wmctrl -m | grep -q 'Name: Openbox'

export DISPLAY="$NESTED_DISPLAY" XAUTHORITY=/dev/null WAYLAND_DISPLAY=""
export XDG_SESSION_TYPE=x11 GDK_BACKEND=x11 MOZ_ENABLE_WAYLAND=0

MONITOR_COMMAND=(
  python3 "$ROOT/runtime_monitor/monitor.py"
  --config "$ROOT/configs/runtime/config.yaml"
  --app-scope-config "$RUNTIME_APP_SCOPE_CONFIG"
  --app-mapping "$ROOT/configs/runtime/test1_app_mapping.json"
  --app-vocab "$LSTM_APP_VOCAB"
  --group-vocab "$PRED/data/vocab/test1/user_group_vocab.json"
  --lstm-checkpoint "$LSTM_CHECKPOINT"
  --lstm-model-type "$LSTM_MODEL_TYPE" --enable-online-lstm --enable-parp-bridge
  --parp-bridge-mode "$BRIDGE_MODE" --parp-debugfs-root "$PARP_DEBUGFS_ROOT"
  --parp-model-name AppLSTM-v3-test1 --parp-model-version 401
  --parp-schema-version parp-app-prior-v1 --parp-prior-ttl-ms 180000
  --target-apps FIREFOX,LIBREOFFICE,VLC,GIMP,AUDACITY,THUNDERBIRD,TELEGRAM,EVINCE,FILES,CALCULATOR
  --foreground-backend x11 --direct-x11-events --sample-interval "$SAMPLE_INTERVAL" --duration "$MONITOR_DURATION"
  --close-grace-windows 1 --test-slice "$TEST_SLICE" --disable-ebpf
  --output-dir "$ROOT/outputs/runtime_monitor" --session-id "$SESSION_ID"
)
if [ -n "$PARP_BIND_CONFIG" ]; then
  MONITOR_COMMAND+=(--parp-app-bind-config "$PARP_BIND_CONFIG")
fi
if [ "$TEST3_MEMORY_SHADOW" = "1" ]; then
  MONITOR_COMMAND+=(
    --enable-memory-shadow
    --memory-shadow-interval-s "$TEST3_MEMORY_SHADOW_INTERVAL_S"
    --memory-shadow-top-k "$TEST3_MEMORY_SHADOW_TOP_K"
    --memory-shadow-recovery-window-s "$TEST3_MEMORY_SHADOW_RECOVERY_WINDOW_S"
  )
fi
if [ "$TEST4_RECLAIM_CONTROLLER" = "1" ]; then
  MONITOR_COMMAND+=(--enable-app-reclaim-controller --app-reclaim-mode "$TEST4_RECLAIM_MODE")
fi
if [ "$TEST4B_BALLAST" = "1" ]; then
  if [ -z "$TEST4B_BALLAST_CONFIG" ]; then
    echo "TEST4B_BALLAST_CONFIG is required when TEST4B_BALLAST=1" >&2
    exit 2
  fi
  MONITOR_COMMAND+=(
    --enable-test4b-ballast --test4b-ballast-config "$TEST4B_BALLAST_CONFIG"
    --test4b-reclaim-mode "$TEST4B_RECLAIM_MODE"
    --test4b-probability-threshold "$TEST4B_PROBABILITY_THRESHOLD"
    --test4b-required-low-probability-batches "$TEST4B_REQUIRED_LOW_PROBABILITY_BATCHES"
    --test4b-cold-quiet-s "$TEST4B_COLD_QUIET_S"
    --test4b-target-headroom-bytes "$TEST4B_TARGET_HEADROOM_BYTES"
    --test4b-minimum-headroom-deficit-bytes "$TEST4B_MINIMUM_HEADROOM_DEFICIT_BYTES"
    --test4b-hard-min-available-bytes "$TEST4B_HARD_MIN_AVAILABLE_BYTES"
    --test4b-psi-full-abort-avg10 "$TEST4B_PSI_FULL_ABORT_AVG10"
  )
  if [ -n "$TEST4B_CONTROLLER_ACTIVATION_FILE" ]; then
    MONITOR_COMMAND+=(--test4b-controller-activation-file "$TEST4B_CONTROLLER_ACTIVATION_FILE")
  fi
  if [ "$TEST4B_APPLY_PREFLIGHT_READY" = "1" ]; then
    MONITOR_COMMAND+=(--test4b-apply-preflight-ready)
  fi
fi
printf '%q ' "${MONITOR_COMMAND[@]}" >"$SESSION_DIR/command.txt"
printf '\n' >>"$SESSION_DIR/command.txt"

"${MONITOR_COMMAND[@]}" >"$MONITOR_LOG" 2>&1 &
MONITOR_PID=$!
sleep 3
set +e
bash "$ROOT/automation/run_automation.sh" \
  --scenario "$SCENARIO_PATH" --display "$NESTED_DISPLAY" --xauthority /dev/null \
  --session-id "$SESSION_ID" --scenario-id "$SCENARIO_ID" --trace-output "$TRACE" \
  --test-slice "$TEST_SLICE" \
  --var "LIBREOFFICE_PROFILE=$SESSION_DIR/automation/libreoffice-profile" >"$AUTOMATION_LOG" 2>&1
AUTOMATION_RC=$?
set -e

# Closing a systemd scope is asynchronous with X11 DestroyNotify.  Keep the
# event-driven monitor alive briefly so every scripted close is observable.
if [ "$AUTOMATION_RC" -eq 0 ] && [ "$POST_AUTOMATION_SETTLE_SECONDS" != "0" ]; then
  sleep "$POST_AUTOMATION_SETTLE_SECONDS"
fi

if [ -n "$MONITOR_PID" ] && kill -0 "$MONITOR_PID" 2>/dev/null; then
  kill -INT "$MONITOR_PID" 2>/dev/null || true
fi
set +e
wait "$MONITOR_PID"
MONITOR_RC=$?
set -e
MONITOR_PID=""

if [ "$PARP_ACCESS_GRANTED" = "1" ]; then
  cat "$PARP_DEBUGFS_ROOT/snapshot" >"$SESSION_DIR/parp/snapshot_after.txt"
fi

set +e
if [ "$TEST4_SKIP_TEST1_EVENT_COVERAGE" = "1" ]; then
  printf 'SKIPPED: Test4 uses a validation-derived sequence, not Test1 42-action contract.\n' >"$SESSION_DIR/review/event_coverage.log"
  COVERAGE_RC=0
else
  python3 "$ROOT/runtime_monitor/scripts/verify_test1_event_coverage.py" \
    --session-dir "$SESSION_DIR" >"$SESSION_DIR/review/event_coverage.log" 2>&1
  COVERAGE_RC=$?
fi
python3 "$ROOT/runtime_monitor/scripts/verify_test2_prediction_sink.py" \
  --session-dir "$SESSION_DIR" \
  --app-scope-config "$RUNTIME_APP_SCOPE_CONFIG" \
  --bridge-mode "$BRIDGE_MODE" >"$SESSION_DIR/review/prediction_sink_coverage.log" 2>&1
SINK_RC=$?
set -e

python3 - "$SESSION_DIR" "$BRIDGE_MODE" "$PREFLIGHT_RC" "$AUTOMATION_RC" "$MONITOR_RC" "$COVERAGE_RC" "$SINK_RC" <<'PY'
import json
import sys
from pathlib import Path

session_dir = Path(sys.argv[1])
manifest = {
    "session_id": session_dir.name,
    "bridge_mode": sys.argv[2],
    "preflight_rc": int(sys.argv[3]),
    "automation_rc": int(sys.argv[4]),
    "monitor_rc": int(sys.argv[5]),
    "event_coverage_rc": int(sys.argv[6]),
    "sink_coverage_rc": int(sys.argv[7]),
    "session_dir": str(session_dir),
}
(session_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY

printf 'session_id=%s\nbridge_mode=%s\npreflight_rc=%s\nautomation_rc=%s\nmonitor_rc=%s\nevent_coverage_rc=%s\nsink_coverage_rc=%s\nsession_dir=%s\n' \
  "$SESSION_ID" "$BRIDGE_MODE" "$PREFLIGHT_RC" "$AUTOMATION_RC" "$MONITOR_RC" "$COVERAGE_RC" "$SINK_RC" "$SESSION_DIR" \
  | tee "$SESSION_DIR/review/test2_collection_summary.txt"

if [ "$AUTOMATION_RC" -ne 0 ] || [ "$MONITOR_RC" -ne 0 ] || [ "$COVERAGE_RC" -ne 0 ] || [ "$SINK_RC" -ne 0 ]; then
  exit 1
fi
