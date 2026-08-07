#!/usr/bin/env bash
set -u -o pipefail

# Test4B-1 is capacity calibration only.  It deliberately keeps every
# memory.reclaim controller disabled, regardless of inherited shell settings.
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
PHASE="${TEST4B_CAL_PHASE:-A_FILE}"
case "$PHASE" in A_FILE|B_ANON|C_BASELINE|C_BASELINE_STABLE|C_MIXED|C_MIXED_20) ;; *) echo "invalid TEST4B_CAL_PHASE: $PHASE" >&2; exit 2;; esac
SESSION_ID="${SESSION_ID:-test4b1_calibration_${PHASE}_$(date +%Y%m%d_%H%M%S)}"
SESSION_DIR="$ROOT/outputs/runtime_monitor/$SESSION_ID"
case "$PHASE" in A_FILE|B_ANON) MONITOR_DURATION="${MONITOR_DURATION:-70}";; C_BASELINE|C_BASELINE_STABLE) MONITOR_DURATION="${MONITOR_DURATION:-115}";; C_MIXED|C_MIXED_20) MONITOR_DURATION="${MONITOR_DURATION:-140}";; esac
cleanup_needed=0
mkdir -p "$SESSION_DIR" "$SESSION_DIR/review"

cleanup() {
  if [ "$cleanup_needed" = 1 ]; then
    python3 "$ROOT/runtime_monitor/scripts/test4b_cgroup_setup.py" --cleanup \
      --session-dir "$SESSION_DIR" --ballast-config "$SESSION_DIR/calibration/ballast_config.json" \
      >"$SESSION_DIR/cgroup_cleanup_stdout.json" 2>&1 || true
  fi
}
trap cleanup EXIT

make -C "$ROOT/runtime_monitor/tools" all >"$SESSION_DIR/ballast_build.log" || exit 1
python3 "$ROOT/runtime_monitor/scripts/build_test4b_calibration.py" \
  --session-dir "$SESSION_DIR" --phase "$PHASE" >"$SESSION_DIR/calibration_build.json" || exit 1
python3 "$ROOT/runtime_monitor/scripts/test4b_cgroup_setup.py" --create \
  --session-dir "$SESSION_DIR" --ballast-config "$SESSION_DIR/calibration/ballast_config.json" \
  >"$SESSION_DIR/cgroup_create_stdout.json" || exit 1
CGROUP_STATUS="$(python3 - "$SESSION_DIR/cgroup_create_stdout.json" <<'PY'
import json,sys
print(json.load(open(sys.argv[1], encoding='utf-8')).get('status', 'BLOCKED'))
PY
)"
if [ "$CGROUP_STATUS" != READY ]; then
  echo "Test4B-1 preflight blocked" >&2
  exit 1
fi
cleanup_needed=1

export SESSION_ID SCENARIO_PATH="$SESSION_DIR/configs/test4b_calibration_scenario.json"
export SCENARIO_ID="test4b1_calibration_${PHASE}" RUNTIME_APP_SCOPE_CONFIG="$SESSION_DIR/configs/test4b_calibration_scope.json"
export TEST_SLICE="test4b-experiment.slice" PARP_BRIDGE_MODE="shadow-write"
export TEST4_SKIP_TEST1_EVENT_COVERAGE=1 TEST3_MEMORY_SHADOW=0 TEST4_RECLAIM_CONTROLLER=0
export TEST4B_BALLAST=0 TEST4B_RECLAIM_MODE=shadow TEST4B_APPLY_PREFLIGHT_READY=0
set +e
bash "$ROOT/runtime_monitor/scripts/run_test2_online_lstm_parp_sink.sh" \
  --session-id "$SESSION_ID" --duration "$MONITOR_DURATION" --sample-interval 0.25 \
  --parp-bridge-mode shadow-write --grant-parp-debugfs-access
TEST2_RC=$?
set -e
python3 - "$SESSION_DIR" "$TEST2_RC" <<'PY'
import json, sys
path = __import__('pathlib').Path(sys.argv[1]) / 'calibration' / 'runner_result.json'
path.write_text(json.dumps({'test2_wrapper_rc': int(sys.argv[2]), 'memory_reclaim_write_attempts': 0}, indent=2) + '\n', encoding='utf-8')
PY
python3 "$ROOT/runtime_monitor/scripts/summarize_test4b_calibration.py" --session-dir "$SESSION_DIR" \
  >"$SESSION_DIR/review/calibration_summary_stdout.json"
printf 'phase=%s\nsession_dir=%s\ntest2_wrapper_rc=%s\nmemory_reclaim_write_attempts=0\n' \
  "$PHASE" "$SESSION_DIR" "$TEST2_RC" >"$SESSION_DIR/review/calibration_collection_summary.txt"
echo "$SESSION_DIR"
