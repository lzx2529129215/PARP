#!/usr/bin/env bash
set -u

DEBUGFS_PATH="${DEBUGFS_PATH:-/sys/kernel/debug/lru_gen_workload_markov}"
APP_ID="${APP_ID:-1}"
CGROUP_ID="${CGROUP_ID:-12345}"
PREV_WORKLOAD="${PREV_WORKLOAD:-0}"
CURRENT_WORKLOAD="${CURRENT_WORKLOAD:-2}"
NEXT_WORKLOAD="${NEXT_WORKLOAD:-3}"
CONFIDENCE="${CONFIDENCE:-9000}"
BOOST="${BOOST:-2}"
TTL_MS="${TTL_MS:-300000}"
PRED_TTL_MS="${PRED_TTL_MS:-180000}"

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
OUT_DIR="${OUT_DIR:-$ROOT_DIR/outputs/mglru/debugfs_test_$(date +%Y%m%d_%H%M%S)}"
mkdir -p "$OUT_DIR"

BEFORE="$OUT_DIR/debugfs_before.txt"
AFTER="$OUT_DIR/debugfs_after.txt"
SUMMARY="$OUT_DIR/test_summary.md"

if [[ "${EUID}" -eq 0 ]]; then
  READ_CMD=(cat "$DEBUGFS_PATH")
  WRITE_CMD=(tee "$DEBUGFS_PATH")
  EXISTS_CMD=(test -e "$DEBUGFS_PATH")
else
  READ_CMD=(sudo cat "$DEBUGFS_PATH")
  WRITE_CMD=(sudo tee "$DEBUGFS_PATH")
  EXISTS_CMD=(sudo test -e "$DEBUGFS_PATH")
fi

run_read() {
  "${READ_CMD[@]}"
}

run_write() {
  local cmd="$1"
  printf '%s\n' "$cmd" | "${WRITE_CMD[@]}" >/dev/null
}

bool_word() {
  if [[ "$1" -eq 0 ]]; then
    printf 'true'
  else
    printf 'false'
  fi
}

debugfs_exists=false
read_before_ok=false
clear_all_ok=false
app_current_write_ok=false
app_predict_write_ok=false
workload_update_write_ok=false
markov_set_write_ok=false
read_after_ok=false
expected_state_found=false

if "${EXISTS_CMD[@]}"; then
  debugfs_exists=true
fi

if run_read >"$BEFORE" 2>"$OUT_DIR/debugfs_before.err"; then
  read_before_ok=true
fi

if run_write "clear all" 2>"$OUT_DIR/clear_all.err"; then
  clear_all_ok=true
fi

if run_write "app current $APP_ID $CGROUP_ID $TTL_MS" 2>"$OUT_DIR/app_current.err"; then
  app_current_write_ok=true
fi

if run_write "app predict $PRED_TTL_MS 1 8000 2 5000 3 3000" 2>"$OUT_DIR/app_predict.err"; then
  app_predict_write_ok=true
fi

if run_write "workload update $CGROUP_ID $APP_ID $PREV_WORKLOAD" 2>"$OUT_DIR/workload_update_prev.err" &&
   run_write "workload update $CGROUP_ID $APP_ID $CURRENT_WORKLOAD" 2>"$OUT_DIR/workload_update_current.err"; then
  workload_update_write_ok=true
fi

if run_write "markov set $APP_ID $PREV_WORKLOAD $CURRENT_WORKLOAD $NEXT_WORKLOAD $CONFIDENCE $BOOST" 2>"$OUT_DIR/markov_set.err"; then
  markov_set_write_ok=true
fi

if run_read >"$AFTER" 2>"$OUT_DIR/debugfs_after.err"; then
  read_after_ok=true
fi

if grep -q "^app $APP_ID $CGROUP_ID $CURRENT_WORKLOAD $PREV_WORKLOAD $TTL_MS" "$AFTER" &&
   grep -q "^pred 1 8000" "$AFTER" &&
   grep -q "^pred 2 5000" "$AFTER" &&
   grep -q "^pred 3 3000" "$AFTER" &&
   grep -q "^hist $APP_ID $CGROUP_ID $PREV_WORKLOAD" "$AFTER" &&
   grep -q "^hist $APP_ID $CGROUP_ID $CURRENT_WORKLOAD" "$AFTER" &&
   grep -q "^markov $APP_ID $PREV_WORKLOAD $CURRENT_WORKLOAD $NEXT_WORKLOAD $CONFIDENCE $BOOST" "$AFTER"; then
  expected_state_found=true
fi

final_result=FAIL
if [[ "$debugfs_exists" == true &&
      "$read_before_ok" == true &&
      "$clear_all_ok" == true &&
      "$app_current_write_ok" == true &&
      "$app_predict_write_ok" == true &&
      "$workload_update_write_ok" == true &&
      "$markov_set_write_ok" == true &&
      "$read_after_ok" == true &&
      "$expected_state_found" == true ]]; then
  final_result=PASS
fi

cat >"$SUMMARY" <<EOF
# MGLRU workload Markov debugfs test

- uname_r: $(uname -r)
- debugfs_path: \`$DEBUGFS_PATH\`
- output_dir: \`$OUT_DIR\`
- debugfs_exists: $debugfs_exists
- read_before_ok: $read_before_ok
- clear_all_ok: $clear_all_ok
- app_current_write_ok: $app_current_write_ok
- app_predict_write_ok: $app_predict_write_ok
- workload_update_write_ok: $workload_update_write_ok
- markov_set_write_ok: $markov_set_write_ok
- read_after_ok: $read_after_ok
- expected_state_found: $expected_state_found
- final_result: $final_result

## Expected commands

\`\`\`bash
echo "clear all" | sudo tee $DEBUGFS_PATH > /dev/null
echo "app current $APP_ID $CGROUP_ID $TTL_MS" | sudo tee $DEBUGFS_PATH > /dev/null
echo "app predict $PRED_TTL_MS 1 8000 2 5000 3 3000" | sudo tee $DEBUGFS_PATH > /dev/null
echo "workload update $CGROUP_ID $APP_ID $PREV_WORKLOAD" | sudo tee $DEBUGFS_PATH > /dev/null
echo "workload update $CGROUP_ID $APP_ID $CURRENT_WORKLOAD" | sudo tee $DEBUGFS_PATH > /dev/null
echo "markov set $APP_ID $PREV_WORKLOAD $CURRENT_WORKLOAD $NEXT_WORKLOAD $CONFIDENCE $BOOST" | sudo tee $DEBUGFS_PATH > /dev/null
sudo cat $DEBUGFS_PATH
\`\`\`
EOF

echo "output_dir=$OUT_DIR"
echo "summary=$SUMMARY"
echo "final_result=$final_result"

if [[ "$final_result" == PASS ]]; then
  exit 0
fi
exit 1
