#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
MODE="${TEST4_MODE:-shadow}"
SESSION_ID="${SESSION_ID:-test4_probability_activity_reclaim_${MODE}_$(date +%Y%m%d_%H%M%S)}"
SCENARIO="$ROOT/configs/automation/test4_validation_sequence_108_4_60.json"
if [[ "$MODE" != shadow && "$MODE" != apply-bounded ]]; then echo "mode must be shadow or apply-bounded" >&2; exit 2; fi
python3 "$ROOT/runtime_monitor/scripts/build_automation_from_lstm_split.py" --output "$SCENARIO" --coverage-output "${SCENARIO%.json}.coverage.json" >/dev/null
SESSION_DIR="$ROOT/outputs/runtime_monitor/$SESSION_ID"; mkdir -p "$SESSION_DIR/review"
set +e
python3 "$ROOT/runtime_monitor/scripts/check_test4_reclaim_preflight.py" --mode apply-bounded --output "$SESSION_DIR/apply_preflight.json"
APPLY_RC=$?
set -e
if [[ "$MODE" == apply-bounded ]]; then
  echo "apply-bounded refused: $(cat "$SESSION_DIR/apply_preflight.json")" >&2
  exit "$APPLY_RC"
fi
export SESSION_ID SCENARIO_PATH="$SCENARIO" SCENARIO_ID="test4_validation_108_4_60" PARP_BRIDGE_MODE="${PARP_BRIDGE_MODE:-shadow-write}"
export TEST4_RECLAIM_CONTROLLER=1 TEST4_RECLAIM_MODE=shadow TEST4_SKIP_TEST1_EVENT_COVERAGE=1
export MONITOR_DURATION="${MONITOR_DURATION:-100}" SAMPLE_INTERVAL="${SAMPLE_INTERVAL:-0.25}"
set +e
bash "$ROOT/runtime_monitor/scripts/run_test2_online_lstm_parp_sink.sh" --session-id "$SESSION_ID" --duration "$MONITOR_DURATION" --sample-interval "$SAMPLE_INTERVAL" --parp-bridge-mode "$PARP_BRIDGE_MODE" --grant-parp-debugfs-access
TEST2_RC=$?
set -e
EXECUTION_COVERAGE="$SESSION_DIR/review/validation_sequence_execution.json"
set +e
python3 "$ROOT/runtime_monitor/scripts/verify_test4_validation_execution.py" --session-dir "$SESSION_DIR" --coverage "${SCENARIO%.json}.coverage.json" --output "$EXECUTION_COVERAGE" >"$SESSION_DIR/review/validation_sequence_execution.log" 2>&1
EXECUTION_RC=$?
set -e
python3 "$ROOT/runtime_monitor/scripts/finalize_test4_session.py" --session-dir "$SESSION_DIR" --coverage "${SCENARIO%.json}.coverage.json" --execution-coverage "$EXECUTION_COVERAGE" --apply-preflight "$SESSION_DIR/apply_preflight.json"
echo "Test4 shadow session: $SESSION_DIR"
if [[ "$TEST2_RC" -ne 0 || "$EXECUTION_RC" -ne 0 ]]; then
  exit 1
fi
