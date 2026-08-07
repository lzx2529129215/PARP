#!/usr/bin/env bash
set -euo pipefail

# Test4B: only a freshly created test4b-experiment.slice receives finite
# memory.max.  The script never touches huawei-test.slice or global memory
# controls.  cleanup is deliberately scoped to the session's known cgroup,
# sockets and synthetic files.
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
MODE="${TEST4B_MODE:-shadow}"
SESSION_ID="${SESSION_ID:-test4b_foreground_mixed_workingset_reclaim_${MODE}_$(date +%Y%m%d_%H%M%S)}"
SESSION_DIR="$ROOT/outputs/runtime_monitor/$SESSION_ID"
BASE_COVERAGE="$ROOT/configs/automation/test4_validation_sequence_108_4_60.coverage.json"
MONITOR_DURATION="${MONITOR_DURATION:-105}"
cleanup_needed=0

case "$MODE" in shadow|apply-bounded|native) ;; *) echo "mode must be shadow, apply-bounded, or native" >&2; exit 2 ;; esac
mkdir -p "$SESSION_DIR"

cleanup() {
  if [ "$cleanup_needed" = 1 ]; then
    python3 "$ROOT/runtime_monitor/scripts/test4b_cgroup_setup.py" --cleanup \
      --session-dir "$SESSION_DIR" --ballast-config "$SESSION_DIR/ballast/ballast_config.json" \
      >"$SESSION_DIR/cgroup_cleanup_stdout.json" 2>&1 || true
  fi
}
trap cleanup EXIT

make -C "$ROOT/runtime_monitor/tools" all >"$SESSION_DIR/ballast_build.log"
# 48 MiB/app keeps all four required regions (20/4/20/4 MiB) and was chosen
# after an 80 MiB/App shadow preflight observed sustained full PSI during
# foreground construction.  This decreases the synthetic pressure; no
# probability, PSI, or reclaim safety gate is relaxed.
python3 "$ROOT/runtime_monitor/scripts/build_test4b_scenario.py" \
  --session-dir "$SESSION_DIR" --anon-cold-bytes $((20 * 1024 * 1024)) \
  --anon-hot-bytes $((4 * 1024 * 1024)) --file-cold-bytes $((20 * 1024 * 1024)) \
  --file-hot-bytes $((4 * 1024 * 1024)) \
  >"$SESSION_DIR/build_test4b_scenario.json"

python3 "$ROOT/runtime_monitor/scripts/analyze_test4b_probability_scan.py" \
  --output "$SESSION_DIR/analysis_probability_scan.json" >"$SESSION_DIR/probability_scan_stdout.json"
PROBABILITY_THRESHOLD="$(python3 - "$SESSION_DIR/analysis_probability_scan.json" <<'PY'
import json, sys
data=json.load(open(sys.argv[1], encoding='utf-8'))
selected=data.get('selected') or {}
if data.get('status') != 'READY': raise SystemExit(3)
print(selected['threshold'])
PY
)" || { echo "Test4B probability scan found no controlled threshold" >&2; exit 1; }

python3 "$ROOT/runtime_monitor/scripts/test4b_cgroup_setup.py" --create \
  --session-dir "$SESSION_DIR" --ballast-config "$SESSION_DIR/ballast/ballast_config.json" \
  >"$SESSION_DIR/cgroup_create_stdout.json"
CGROUP_STATUS="$(python3 - "$SESSION_DIR/cgroup_create_stdout.json" <<'PY'
import json,sys
print(json.load(open(sys.argv[1],encoding='utf-8')).get('status','BLOCKED'))
PY
)"
if [ "$CGROUP_STATUS" != READY ]; then
  echo "Test4B blocked: finite temporary cgroup could not be safely created" >&2
  exit 1
fi
cleanup_needed=1

case "$MODE" in
  shadow) TEST4B_RECLAIM_MODE=shadow; TEST4B_APPLY_PREFLIGHT_READY=0 ;;
  apply-bounded) TEST4B_RECLAIM_MODE=apply-bounded; TEST4B_APPLY_PREFLIGHT_READY=1 ;;
  native) TEST4B_RECLAIM_MODE=off; TEST4B_APPLY_PREFLIGHT_READY=0 ;;
esac

export SESSION_ID SCENARIO_PATH="$SESSION_DIR/configs/test4b_validation_sequence.json"
export SCENARIO_ID="test4b_validation_108_4_60" RUNTIME_APP_SCOPE_CONFIG="$SESSION_DIR/configs/test4b_runtime_app_scope.json"
export TEST_SLICE="test4b-experiment.slice" PARP_BRIDGE_MODE="${PARP_BRIDGE_MODE:-shadow-write}"
export TEST4_SKIP_TEST1_EVENT_COVERAGE=1 TEST4B_BALLAST=1 TEST4B_BALLAST_CONFIG="$SESSION_DIR/ballast/ballast_config.json"
export TEST4B_RECLAIM_MODE TEST4B_PROBABILITY_THRESHOLD="$PROBABILITY_THRESHOLD" TEST4B_APPLY_PREFLIGHT_READY
export MONITOR_DURATION SAMPLE_INTERVAL="${SAMPLE_INTERVAL:-0.25}" POST_AUTOMATION_SETTLE_SECONDS="${POST_AUTOMATION_SETTLE_SECONDS:-5}"
set +e
bash "$ROOT/runtime_monitor/scripts/run_test2_online_lstm_parp_sink.sh" \
  --session-id "$SESSION_ID" --duration "$MONITOR_DURATION" --sample-interval "$SAMPLE_INTERVAL" \
  --parp-bridge-mode "$PARP_BRIDGE_MODE" --grant-parp-debugfs-access
RUN_RC=$?
set -e

CGROUP_PATH="$(python3 - "$SESSION_DIR/cgroup_created.json" <<'PY'
import json,sys
print(json.load(open(sys.argv[1],encoding='utf-8')).get('path',''))
PY
)"
if [ -n "$CGROUP_PATH" ] && [ -d "$CGROUP_PATH" ]; then
  cp "$CGROUP_PATH/memory.events" "$SESSION_DIR/reclaim/test4b_parent_memory_events_after.txt" 2>/dev/null || true
  cp "$CGROUP_PATH/memory.stat" "$SESSION_DIR/reclaim/test4b_parent_memory_stat_after.txt" 2>/dev/null || true
fi
set +e
python3 "$ROOT/runtime_monitor/scripts/verify_test4_validation_execution.py" \
  --session-dir "$SESSION_DIR" --coverage "$BASE_COVERAGE" \
  --output "$SESSION_DIR/review/validation_sequence_execution.json" \
  >"$SESSION_DIR/review/validation_sequence_execution.log" 2>&1
VERIFY_RC=$?
python3 "$ROOT/runtime_monitor/scripts/finalize_test4b_session.py" \
  --session-dir "$SESSION_DIR" --base-coverage "$BASE_COVERAGE" \
  >"$SESSION_DIR/review/finalize_test4b.log" 2>&1
FINALIZE_RC=$?
set -e
printf 'mode=%s\nsession_dir=%s\nrun_rc=%s\nverify_rc=%s\nfinalize_rc=%s\nprobability_threshold=%s\n' \
  "$MODE" "$SESSION_DIR" "$RUN_RC" "$VERIFY_RC" "$FINALIZE_RC" "$PROBABILITY_THRESHOLD" \
  >"$SESSION_DIR/review/test4b_collection_summary.txt"
echo "$SESSION_DIR"
if [ "$RUN_RC" -ne 0 ] || [ "$VERIFY_RC" -ne 0 ] || [ "$FINALIZE_RC" -ne 0 ]; then exit 1; fi
