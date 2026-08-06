#!/usr/bin/env bash
set -euo pipefail

APP="WPS"
DURATION="30"
OUTPUT_DIR="outputs/region_monitor_smoke"
CGROUP_PATH=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --app) APP="$2"; shift 2 ;;
    --cgroup) CGROUP_PATH="$2"; shift 2 ;;
    --duration) DURATION="$2"; shift 2 ;;
    --output-dir) OUTPUT_DIR="$2"; shift 2 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

APP_SCOPE_CONFIG="configs/runtime/runtime_app_scope.json"
readarray -t SCOPE_INFO < <(python3 - "$APP_SCOPE_CONFIG" "$APP" <<'PY'
import json, sys
cfg = json.load(open(sys.argv[1], encoding="utf-8"))
app = next((item for item in cfg.get("apps", []) if item.get("app_key") == sys.argv[2]), None)
if not app:
    raise SystemExit(f"app not found in scope config: {sys.argv[2]}")
print(cfg["slice"])
print(app["scope_name"])
PY
)
SLICE_NAME="${SCOPE_INFO[0]}"
SCOPE_NAME="${SCOPE_INFO[1]}"
if [[ -n "$CGROUP_PATH" ]]; then
  SCOPE_PATH="$CGROUP_PATH"
  SLICE_PATH="$(dirname "$SCOPE_PATH")"
else
  TARGET_UID="${SUDO_UID:-$(id -u)}"
  TARGET_USER="${SUDO_USER:-$(id -un)}"
  CONTROL_GROUP=""
  if [[ "$(id -u)" == "0" && -n "${SUDO_USER:-}" ]]; then
    CONTROL_GROUP="$(sudo -u "$TARGET_USER" env \
      XDG_RUNTIME_DIR="/run/user/${TARGET_UID}" \
      DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/${TARGET_UID}/bus" \
      systemctl --user show "$SLICE_NAME" -p ControlGroup --value 2>/dev/null || true)"
  else
    CONTROL_GROUP="$(systemctl --user show "$SLICE_NAME" -p ControlGroup --value 2>/dev/null || true)"
  fi
  if [[ -n "$CONTROL_GROUP" ]]; then
    SLICE_PATH="/sys/fs/cgroup/${CONTROL_GROUP#/}"
  else
    SLICE_PATH="$(find "/sys/fs/cgroup/user.slice/user-${TARGET_UID}.slice/user@${TARGET_UID}.service" \
      -type d -name "$SLICE_NAME" -print -quit 2>/dev/null || true)"
  fi
  if [[ -z "$SLICE_PATH" ]]; then
    echo "FAIL: cannot resolve configured user slice: $SLICE_NAME" >&2
    exit 3
  fi
  SCOPE_PATH="$SLICE_PATH/$SCOPE_NAME"
fi
if [[ ! -d "$SCOPE_PATH" ]]; then
  echo "FAIL: configured cgroup does not exist: $SCOPE_PATH" >&2
  echo "请先通过现有 automation 在 ${SLICE_NAME}/${SCOPE_NAME} 中启动 $APP。" >&2
  exit 3
fi
if [[ -z "$(sed -n '/^[0-9][0-9]*$/p' "$SCOPE_PATH/cgroup.procs" 2>/dev/null)" ]]; then
  echo "FAIL: configured cgroup has no target PID: $SCOPE_PATH/cgroup.procs" >&2
  exit 4
fi

SID="region_monitor_smoke_$(date +%Y%m%d_%H%M%S)"
SESSION_DIR="$OUTPUT_DIR/$SID"
mkdir -p "$SESSION_DIR"

echo "== Region Monitor smoke =="
echo "app=$APP"
echo "session_dir=$SESSION_DIR"
echo "slice_path=$SLICE_PATH"
echo "scope_path=$SCOPE_PATH"
echo "说明：真实 DAMON smoke 通常需要在宿主机终端用 sudo 执行；本脚本不启用 MGLRU Apply，不写 lru_gen_pages。"

python3 -m runtime_monitor.region_monitor.region_monitor \
  --session-dir "$SESSION_DIR" \
  --config runtime_monitor/config/region_monitor.json \
  --app-scope-config configs/runtime/runtime_app_scope.json \
  --slice-path "$SLICE_PATH" \
  --duration-s "$DURATION" \
  >"$SESSION_DIR/region_monitor.log" 2>&1 || true

SUMMARY="$SESSION_DIR/smoke_summary.md"
EVENTS="$SESSION_DIR/region_monitor/region_events.jsonl"
WINDOWS="$SESSION_DIR/region_monitor/region_windows.jsonl"
VOCAB="$SESSION_DIR/region_monitor/region_vocab.json"
RM_SUMMARY="$SESSION_DIR/region_monitor/region_monitor_summary.md"

event_rows=0
window_rows=0
vocab_regions=0
[[ -s "$EVENTS" ]] && event_rows="$(wc -l < "$EVENTS")"
[[ -s "$WINDOWS" ]] && window_rows="$(wc -l < "$WINDOWS")"
if [[ -s "$VOCAB" ]]; then
  vocab_regions="$(python3 - <<'PY' "$VOCAB"
import json, sys
print(len(json.load(open(sys.argv[1], encoding='utf-8')).get('regions', [])))
PY
)"
fi
tracebacks="$(grep -R -n "Traceback" "$SESSION_DIR" 2>/dev/null || true)"
final_result="$(grep -E '^- final_result:' "$RM_SUMMARY" 2>/dev/null | awk '{print $3}' || true)"

{
  echo "# Region Monitor Smoke Summary"
  echo
  echo "- app: $APP"
  echo "- session_dir: \`$SESSION_DIR\`"
  echo "- region_events_rows: $event_rows"
  echo "- region_windows_rows: $window_rows"
  echo "- region_vocab_regions: $vocab_regions"
  echo "- final_result: ${final_result:-UNKNOWN}"
  echo "- traceback: $([[ -z "$tracebacks" ]] && echo no || echo yes)"
  echo "- ready_for_apply: false"
  echo
  echo "本脚本不写 lru_gen_pages，不调用 promote/depromote/protect，不启用 Tier2，不改变 MGLRU 回收行为。"
} > "$SUMMARY"

cat "$SUMMARY"
if [[ "$(id -u)" == "0" && -n "${SUDO_UID:-}" && -n "${SUDO_GID:-}" ]]; then
  chown -R "${SUDO_UID}:${SUDO_GID}" "$SESSION_DIR"
fi
if [[ "$event_rows" -gt 0 && "$window_rows" -gt 0 && "$vocab_regions" -gt 0 && -z "$tracebacks" ]]; then
  exit 0
fi
exit 1
