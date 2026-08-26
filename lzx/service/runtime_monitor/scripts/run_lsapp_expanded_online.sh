#!/usr/bin/env bash
set -euo pipefail

# lzx-note: End-to-end fifteen-app Runtime Monitor and Test3 observation run.
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/layout.sh" # lzx-note
ROOT="$PARP_SERVICE_ROOT"
PRED="$PARP_OPERATION_PREDICTOR_ROOT"
SESSION_ID="${SESSION_ID:-lsapp_expanded_online_$(date +%Y%m%d_%H%M%S)}"
TRANSITIONS="${LSAPP_TRANSITIONS:-60}"
SEED="${LSAPP_SEED:-20260814}"
BRIDGE_MODE="${PARP_BRIDGE_MODE:-shadow-write}"
MONITOR_DURATION="${MONITOR_DURATION:-1200}"

while [ "$#" -gt 0 ]; do
  case "$1" in
    --session-id) SESSION_ID="$2"; shift 2 ;;
    --transitions) TRANSITIONS="$2"; shift 2 ;;
    --seed) SEED="$2"; shift 2 ;;
    --duration) MONITOR_DURATION="$2"; shift 2 ;;
    --bridge-mode|--parp-bridge-mode) BRIDGE_MODE="$2"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

CHECKPOINT="$PRED/outputs/lsapp_expanded/checkpoints/app_lstm_switch_v3.pt"
APP_VOCAB="$PRED/data/vocab/lsapp_expanded/app_vocab_duration.json"
GROUP_VOCAB="$PRED/data/vocab/lsapp_expanded/user_group_vocab.json"
DATASET="$PRED/data/lsapp_expanded/processed/app_lstm_duration_switch/test.csv"
RUNTIME_SCOPE="$PARP_RUNTIME_CONFIG_ROOT/runtime_app_scope.lsapp_expanded.json"
SESSION_DIR="$ROOT/outputs/runtime_monitor/$SESSION_ID"
FIXTURE_DIR="$SESSION_DIR/lsapp-fixtures"
SCENARIO="$SESSION_DIR/config/lsapp-expanded-heldout.json"

for path in "$CHECKPOINT" "$APP_VOCAB" "$GROUP_VOCAB" "$DATASET" "$RUNTIME_SCOPE"; do
  if [ ! -f "$path" ]; then
    echo "missing LSAPP-expanded artifact: $path" >&2
    exit 2
  fi
done

mkdir -p "$SESSION_DIR/config"
python3 "$ROOT/runtime_monitor/scripts/build_lsapp_expanded_scenario.py" \
  --dataset "$DATASET" --vocab "$PRED/data/vocab/lsapp_expanded/app_vocab.json" \
  --fixture-dir "$FIXTURE_DIR" --output "$SCENARIO" \
  --coverage-output "$SESSION_DIR/config/lsapp-expanded-heldout.coverage.json" \
  --transitions "$TRANSITIONS" --seed "$SEED"

export SESSION_ID MONITOR_DURATION
export LSTM_CHECKPOINT="$CHECKPOINT"
export LSTM_APP_VOCAB="$APP_VOCAB"
export LSTM_GROUP_VOCAB="$GROUP_VOCAB"
export RUNTIME_APP_SCOPE_CONFIG="$RUNTIME_SCOPE"
export RUNTIME_TARGET_APPS="FIREFOX,LIBREOFFICE,VLC,GIMP,AUDACITY,THUNDERBIRD,EVINCE,FILES,CALCULATOR,CALENDAR,RHYTHMBOX,IMAGE_VIEWER,SHOTWELL,SYSTEM_MONITOR,SOLITAIRE"
export PARP_MODEL_NAME="AppLSTM-v3-lsapp-expanded"
export PARP_MODEL_VERSION=420
export PARP_BRIDGE_MODE="$BRIDGE_MODE"
export SCENARIO_PATH="$SCENARIO"
export SCENARIO_ID="lsapp_expanded_heldout_${TRANSITIONS}_${SEED}"
export TEST_SLICE="lsapp-expanded.slice"
export NESTED_X_SERVER="${NESTED_X_SERVER:-xvfb}" # lzx-note
export TEST3_MEMORY_SHADOW=1
export TEST4_SKIP_TEST1_EVENT_COVERAGE=1
export POST_AUTOMATION_SETTLE_SECONDS="${POST_AUTOMATION_SETTLE_SECONDS:-3}"

bash "$ROOT/runtime_monitor/scripts/run_test2_online_lstm_parp_sink.sh" \
  --session-id "$SESSION_ID" --duration "$MONITOR_DURATION" \
  --sample-interval "${SAMPLE_INTERVAL:-0.20}" --parp-bridge-mode "$BRIDGE_MODE" \
  --grant-parp-debugfs-access

python3 "$ROOT/runtime_monitor/scripts/analyze_lsapp_expanded_online.py" \
  --session-dir "$SESSION_DIR" --runtime-scope "$RUNTIME_SCOPE"
python3 "$ROOT/runtime_monitor/scripts/analyze_test3_memory_shadow.py" --session-dir "$SESSION_DIR"

echo "LSAPP-expanded online session complete: $SESSION_DIR"
