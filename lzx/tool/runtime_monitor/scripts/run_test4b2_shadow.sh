#!/usr/bin/env bash
set -euo pipefail

# Test4B-2 is intentionally SHADOW-only.  This runner has no APPLY switch and
# rejects an inherited non-shadow mode rather than silently changing it.
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
MODE="${TEST4B_MODE:-shadow}"
SESSION_ID="${SESSION_ID:-test4b2_low_pressure_shadow_$(date +%Y%m%d_%H%M%S)}"
SESSION_DIR="$ROOT/outputs/runtime_monitor/$SESSION_ID"
BASE_COVERAGE="$ROOT/configs/automation/test4_validation_sequence_108_4_60.coverage.json"
MONITOR_DURATION="${MONITOR_DURATION:-300}"
cleanup_needed=0

if [ "$MODE" != shadow ] || [ "${TEST4B_RECLAIM_MODE:-shadow}" != shadow ]; then
  echo "Test4B-2 is SHADOW-only; APPLY is not permitted by this runner" >&2
  exit 2
fi
mkdir -p "$SESSION_DIR"

cleanup() {
  if [ "$cleanup_needed" = 1 ] && [ -f "$SESSION_DIR/ballast/ballast_config.json" ]; then
    python3 "$ROOT/runtime_monitor/scripts/test4b_cgroup_setup.py" --cleanup \
      --session-dir "$SESSION_DIR" --ballast-config "$SESSION_DIR/ballast/ballast_config.json" \
      >"$SESSION_DIR/cgroup_cleanup_stdout.json" 2>&1 || true
  fi
}
trap cleanup EXIT

make -C "$ROOT/runtime_monitor/tools" all >"$SESSION_DIR/ballast_build.log"
python3 "$ROOT/runtime_monitor/scripts/build_test4b2_shadow_scenario.py" --session-dir "$SESSION_DIR" \
  --startup-ready-timeout-s "${TEST4B2_STARTUP_READY_TIMEOUT_S:-180}" \
  --startup-psi-full-start-avg10 "${TEST4B2_STARTUP_PSI_TARGET_AVG10:-0.05}" >"$SESSION_DIR/build_test4b2_scenario.json"
python3 "$ROOT/runtime_monitor/scripts/analyze_test4b_probability_scan.py" --output "$SESSION_DIR/analysis_probability_scan.json" >"$SESSION_DIR/probability_scan_stdout.json"
read -r PROBABILITY_THRESHOLD REQUIRED_BATCHES < <(python3 - "$SESSION_DIR/analysis_probability_scan.json" <<'PY'
import json, sys
value = json.load(open(sys.argv[1], encoding="utf-8"))
selected = value.get("selected") or {}
if value.get("status") != "READY":
    raise SystemExit("no validation-only threshold/K pair satisfies risk <= 0.10")
print(selected["threshold"], selected["required_low_probability_batches"])
PY
)

python3 "$ROOT/runtime_monitor/scripts/test4b_cgroup_setup.py" --create \
  --session-dir "$SESSION_DIR" --ballast-config "$SESSION_DIR/ballast/ballast_config.json" \
  >"$SESSION_DIR/cgroup_create_stdout.json"
CGROUP_STATUS="$(python3 - "$SESSION_DIR/cgroup_create_stdout.json" <<'PY'
import json,sys
print(json.load(open(sys.argv[1], encoding="utf-8")).get("status", "BLOCKED"))
PY
)"
if [ "$CGROUP_STATUS" != READY ]; then
  echo "Test4B-2 blocked: finite temporary cgroup preflight did not pass" >&2
  exit 1
fi
cleanup_needed=1

export SESSION_ID SCENARIO_PATH="$SESSION_DIR/configs/test4b2_validation_sequence.json"
export SCENARIO_ID="test4b2_validation_108_4_60" RUNTIME_APP_SCOPE_CONFIG="$SESSION_DIR/configs/test4b2_runtime_app_scope.json"
export TEST_SLICE="test4b-experiment.slice" PARP_BRIDGE_MODE="${PARP_BRIDGE_MODE:-shadow-write}"
export TEST4_SKIP_TEST1_EVENT_COVERAGE=1 TEST4B_BALLAST=1 TEST4B_BALLAST_CONFIG="$SESSION_DIR/ballast/ballast_config.json"
export TEST4B_RECLAIM_MODE=shadow TEST4B_APPLY_PREFLIGHT_READY=0 TEST4B_PROBABILITY_THRESHOLD="$PROBABILITY_THRESHOLD"
export TEST4B_REQUIRED_LOW_PROBABILITY_BATCHES="$REQUIRED_BATCHES" TEST4B_COLD_QUIET_S="${TEST4B_COLD_QUIET_S:-3}"
export TEST4B_CONTROLLER_ACTIVATION_FILE="$SESSION_DIR/test4b2_controller_active"
export MONITOR_DURATION SAMPLE_INTERVAL="${SAMPLE_INTERVAL:-0.25}" POST_AUTOMATION_SETTLE_SECONDS="${POST_AUTOMATION_SETTLE_SECONDS:-5}"
set +e
bash "$ROOT/runtime_monitor/scripts/run_test2_online_lstm_parp_sink.sh" \
  --session-id "$SESSION_ID" --duration "$MONITOR_DURATION" --sample-interval "$SAMPLE_INTERVAL" \
  --parp-bridge-mode "$PARP_BRIDGE_MODE" --grant-parp-debugfs-access
RUN_RC=$?
set -e

CGROUP_PATH="$(python3 - "$SESSION_DIR/cgroup_created.json" <<'PY'
import json,sys
print(json.load(open(sys.argv[1], encoding="utf-8")).get("path", ""))
PY
)"
if [ -n "$CGROUP_PATH" ] && [ -d "$CGROUP_PATH" ]; then
  cp "$CGROUP_PATH/memory.events" "$SESSION_DIR/reclaim/test4b2_parent_memory_events_after.txt" 2>/dev/null || true
  cp "$CGROUP_PATH/memory.stat" "$SESSION_DIR/reclaim/test4b2_parent_memory_stat_after.txt" 2>/dev/null || true
  cp "$CGROUP_PATH/memory.pressure" "$SESSION_DIR/reclaim/test4b2_parent_memory_pressure_after.txt" 2>/dev/null || true
fi
set +e
python3 "$ROOT/runtime_monitor/scripts/verify_test4_validation_execution.py" \
  --session-dir "$SESSION_DIR" --coverage "$BASE_COVERAGE" \
  --output "$SESSION_DIR/review/validation_sequence_execution.json" \
  >"$SESSION_DIR/review/validation_sequence_execution.log" 2>&1
VERIFY_RC=$?
python3 "$ROOT/runtime_monitor/scripts/finalize_test4b2_shadow.py" --session-dir "$SESSION_DIR" \
  >"$SESSION_DIR/review/finalize_test4b2.log" 2>&1
FINALIZE_RC=$?
set -e
printf 'mode=shadow\nsession_dir=%s\nrun_rc=%s\nverify_rc=%s\nfinalize_rc=%s\nprobability_threshold=%s\nrequired_low_probability_batches=%s\n' \
  "$SESSION_DIR" "$RUN_RC" "$VERIFY_RC" "$FINALIZE_RC" "$PROBABILITY_THRESHOLD" "$REQUIRED_BATCHES" \
  >"$SESSION_DIR/review/test4b2_collection_summary.txt"
echo "$SESSION_DIR"
if [ "$RUN_RC" -ne 0 ] || [ "$VERIFY_RC" -ne 0 ] || [ "$FINALIZE_RC" -ne 0 ]; then exit 1; fi
