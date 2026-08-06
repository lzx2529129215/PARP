#!/usr/bin/env bash
# 语义自动化采集编排。仅使用已有 Runtime Monitor 参数，不触及 MGLRU apply。
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
SCENARIO=""
SESSION_ID="semantic_$(date +%Y%m%d_%H%M%S)"
DRY_RUN=0
CALIBRATION_ONLY=0
ALLOW_EXTERNAL=0
ASSET_MANIFEST="$ROOT/automation/semantic/assets/assets_manifest.example.json"
APP_PROFILE=""
MONITOR_DURATION=90
SKIP_OPTIONAL=0

usage() { cat <<EOF
用法: $0 --semantic-scenario FILE [--session-id ID] [--dry-run] [--calibration-only]
       [--allow-external-side-effects] [--asset-manifest FILE] [--app-profile FILE]
       [--monitor-duration 秒] [--skip-unavailable-optional-apps]
EOF
}
while (($#)); do
  case "$1" in
    --semantic-scenario) SCENARIO="$2"; shift 2;;
    --session-id) SESSION_ID="$2"; shift 2;;
    --dry-run) DRY_RUN=1; shift;;
    --calibration-only) CALIBRATION_ONLY=1; shift;;
    --allow-external-side-effects) ALLOW_EXTERNAL=1; shift;;
    --asset-manifest) ASSET_MANIFEST="$2"; shift 2;;
    --app-profile) APP_PROFILE="$2"; shift 2;;
    --monitor-duration) MONITOR_DURATION="$2"; shift 2;;
    --skip-unavailable-optional-apps) SKIP_OPTIONAL=1; shift;;
    -h|--help) usage; exit 0;;
    *) echo "未知参数: $1" >&2; usage >&2; exit 2;;
  esac
done
[[ -n "$SCENARIO" ]] || { usage >&2; exit 2; }
[[ "$SCENARIO" = /* ]] || SCENARIO="$ROOT/$SCENARIO"

SESSION_DIR="$ROOT/outputs/runtime_monitor/$SESSION_ID"
WORK_DIR="$ROOT/outputs/automation/semantic_collection_$SESSION_ID"
mkdir -p "$SESSION_DIR/model" "$SESSION_DIR/review" "$WORK_DIR"/{compiled,capability,logs,alignment,reports}
COMPILED="$WORK_DIR/compiled/compiled_scenario.json"

PROBE_ARGS=(--output-dir "$WORK_DIR/capability" --candidate-runtime-config "$ROOT/runtime_monitor/config/runtime_app_scope.semantic_automation.json")
python3 "$ROOT/automation/scripts/probe_semantic_apps.py" "${PROBE_ARGS[@]}" >"$WORK_DIR/logs/probe.log" 2>&1
python3 "$ROOT/automation/scripts/validate_automation_assets.py" --manifest "$ASSET_MANIFEST" --output "$WORK_DIR/capability/asset_validation.csv" >"$WORK_DIR/logs/assets.log" 2>&1

COMPILE_ARGS=(--scenario "$SCENARIO" --output "$COMPILED" --asset-manifest "$ASSET_MANIFEST")
((ALLOW_EXTERNAL)) && COMPILE_ARGS+=(--allow-external-side-effects)
python3 "$ROOT/automation/scripts/compile_semantic_scenario.py" "${COMPILE_ARGS[@]}" >"$WORK_DIR/logs/compile.log" 2>&1

if ((DRY_RUN)); then
  python3 "$ROOT/automation/app_automation.py" "$COMPILED" --dry-run --session-id "$SESSION_ID" --scenario-id "$(basename "${SCENARIO%.json}")" --trace-output "$SESSION_DIR/model/automation_trace.csv" >"$WORK_DIR/logs/automation.log" 2>&1
  echo "semantic_collection_status=DRY_RUN session_dir=$SESSION_DIR"
  exit 0
fi

# Observe-only monitor: no debugfs writer, no LSTM reclaim-policy flag and no eBPF flag.
python3 "$ROOT/runtime_monitor/monitor.py" \
  --session-id "$SESSION_ID" --output-dir "$ROOT/outputs/runtime_monitor" \
  --duration "$MONITOR_DURATION" --sample-interval 1 --disable-ebpf \
  --enable-online-lstm --enable-cgroup-workload --enable-dual-workload-markov \
  --app-scope-config "$ROOT/configs/runtime/runtime_app_scope.json" \
  >"$WORK_DIR/logs/monitor.log" 2>&1 &
MONITOR_PID=$!
sleep 2
AUTOMATION_ARGS=("$COMPILED" --session-id "$SESSION_ID" --scenario-id "$(basename "${SCENARIO%.json}")" --trace-output "$SESSION_DIR/model/automation_trace.csv" --test-slice huawei-test.slice)
((CALIBRATION_ONLY)) && AUTOMATION_ARGS+=(--calibration-only --calibration-output-dir "$WORK_DIR/calibration")
set +e
python3 "$ROOT/automation/app_automation.py" "${AUTOMATION_ARGS[@]}" >"$WORK_DIR/logs/automation.log" 2>&1
AUTOMATION_RC=$?
set -e
wait "$MONITOR_PID" || MONITOR_RC=$?
MONITOR_RC=${MONITOR_RC:-0}

python3 "$ROOT/runtime_monitor/scripts/align_semantic_operations.py" \
  --trace "$SESSION_DIR/model/automation_trace.csv" --foreground "$SESSION_DIR/model/global_state_1s.csv" \
  --workload "$SESSION_DIR/model/cgroup_workload_state_1s.csv" --continue "$SESSION_DIR/model/continue_markov_updates.csv" \
  --reentry "$SESSION_DIR/model/reentry_events.csv" --debugfs-writes "$SESSION_DIR/model/mglru_markov_debugfs_writes.csv" \
  --output "$WORK_DIR/alignment/semantic_operation_alignment.csv" >"$WORK_DIR/logs/alignment.log" 2>&1 || true
python3 "$ROOT/runtime_monitor/scripts/build_semantic_operation_summary.py" --alignment "$WORK_DIR/alignment/semantic_operation_alignment.csv" --output-dir "$WORK_DIR/reports" || true
printf 'session_id=%s\nsession_dir=%s\nautomation_rc=%s\nmonitor_rc=%s\nprofile=%s\nskip_optional=%s\n' "$SESSION_ID" "$SESSION_DIR" "$AUTOMATION_RC" "$MONITOR_RC" "$APP_PROFILE" "$SKIP_OPTIONAL" > "$WORK_DIR/reports/collection_summary.txt"
echo "semantic_collection_status=COMPLETED session_dir=$SESSION_DIR"
