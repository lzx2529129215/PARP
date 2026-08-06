#!/usr/bin/env bash
set -euo pipefail

# Test3 is a strict read-only extension of the already validated Test2 path.
# It starts no pressure injector and does not write any memory-management
# interface; the only existing write path is Test2's verified app_bind/prior
# snapshot update ABI, which v4.1 PARP currently stores as metadata only.

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
PRESSURE_LEVEL="${TEST3_PRESSURE_LEVEL:-low}"
SESSION_ID="${SESSION_ID:-test3_app_prediction_memory_value_$(date +%Y%m%d_%H%M%S)}"
BRIDGE_MODE="${PARP_BRIDGE_MODE:-shadow-write}"
MONITOR_DURATION="${MONITOR_DURATION:-180}"
SAMPLE_INTERVAL="${SAMPLE_INTERVAL:-0.25}"

while [ "$#" -gt 0 ]; do
  case "$1" in
    --pressure-level) PRESSURE_LEVEL="$2"; shift 2 ;;
    --session-id) SESSION_ID="$2"; shift 2 ;;
    --duration) MONITOR_DURATION="$2"; shift 2 ;;
    --sample-interval) SAMPLE_INTERVAL="$2"; shift 2 ;;
    --bridge-mode|--parp-bridge-mode) BRIDGE_MODE="$2"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

case "$PRESSURE_LEVEL" in
  low|medium) ;;
  high)
    echo "Test3 high pressure is intentionally not run: no safe natural-workload acceptance gate is configured, and Test3 may not inject reclaim or set cgroup memory controls." >&2
    exit 2
    ;;
  *) echo "pressure level must be low, medium, or high" >&2; exit 2 ;;
esac

# medium denotes the existing fixed ten-app/Test1 natural workload; low uses
# the same reproducible scenario without any additional pressure mechanism.
export TEST3_MEMORY_SHADOW=1
export TEST3_MEMORY_SHADOW_INTERVAL_S="${TEST3_MEMORY_SHADOW_INTERVAL_S:-0.25}"
export TEST3_MEMORY_SHADOW_TOP_K="${TEST3_MEMORY_SHADOW_TOP_K:-3}"
export TEST3_MEMORY_SHADOW_RECOVERY_WINDOW_S="${TEST3_MEMORY_SHADOW_RECOVERY_WINDOW_S:-3}"
export SESSION_ID MONITOR_DURATION SAMPLE_INTERVAL PARP_BRIDGE_MODE="$BRIDGE_MODE"

bash "$ROOT/runtime_monitor/scripts/run_test2_online_lstm_parp_sink.sh" \
  --session-id "$SESSION_ID" --duration "$MONITOR_DURATION" --sample-interval "$SAMPLE_INTERVAL" \
  --parp-bridge-mode "$BRIDGE_MODE" --grant-parp-debugfs-access

SESSION_DIR="$ROOT/outputs/runtime_monitor/$SESSION_ID"
printf '\ntest3_pressure_level=%s\ntest3_observer=procfs_cgroup_read_only\ntest3_forbidden_actions=none\n' "$PRESSURE_LEVEL" >>"$SESSION_DIR/environment.txt"
python3 "$ROOT/runtime_monitor/scripts/analyze_test3_memory_shadow.py" --session-dir "$SESSION_DIR"

echo "Test3 session complete: $SESSION_DIR"
