#!/usr/bin/env bash
set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

DURATION=150
RUN_STRESS=1
SLICE="huawei-test.slice"
SCENARIO="configs/automation/scenario_unified_pipeline_bilibili.json"
for arg in "$@"; do
  case "$arg" in
    --skip-stress) RUN_STRESS=0 ;;
    --duration=*) DURATION="${arg#*=}" ;;
    --scenario=*) SCENARIO="${arg#*=}" ;;
    --test-slice=*) SLICE="${arg#*=}" ;;
    --duration) shift ; DURATION="${1:-150}" ;;
    --scenario) shift ; SCENARIO="${1:-$SCENARIO}" ;;
    --test-slice) shift ; SLICE="${1:-$SLICE}" ;;
  esac
  shift || true
done

TS="$(date +%Y%m%d_%H%M%S)"
SID="session_unified_pipeline_${TS}"
SESSION_DIR="$ROOT/outputs/runtime_monitor/$SID"
WORK_DIR="$ROOT/outputs/mglru/unified_pipeline_run_${TS}"
SHARE_DIR="$ROOT/outputs/mglru/share_unified_pipeline_${TS}"
DEBUGFS="/sys/kernel/debug/lru_gen_workload_markov"
mkdir -p "$SESSION_DIR" "$WORK_DIR" "$SHARE_DIR"
mkdir -p "$WORK_DIR"/{precheck,config,lstm,workload,markov,kernel,timeline,reports,logs,source,tests}
mkdir -p "$SHARE_DIR"/{lstm,workload,markov,kernel,timeline,reports,logs,source,tests}

printf '%s\n' "$SID" > "$WORK_DIR/config/session_id.txt"
printf '%s\n' "$SCENARIO" > "$WORK_DIR/config/scenario_path.txt"
cp "$SCENARIO" "$WORK_DIR/config/automation_scenario_used.json" 2>/dev/null || true
uname -a > "$WORK_DIR/precheck/uname.txt"
cat /proc/version > "$WORK_DIR/precheck/proc_version.txt"
cat /proc/cmdline > "$WORK_DIR/precheck/proc_cmdline.txt"
cp "/boot/config-$(uname -r)" "$WORK_DIR/precheck/running_kernel_config.txt" 2>/dev/null || true
git status --short > "$WORK_DIR/precheck/git_status_before.txt"
journalctl -b -k --no-pager > "$WORK_DIR/precheck/kernel_journal_before.txt" 2>&1 || true
python3 runtime_monitor/monitor.py --help > "$WORK_DIR/config/monitor_cli_help.txt" 2>&1

{
  echo "# 本轮统一运行配置"
  echo
  echo "- project_root: $ROOT"
  echo "- runtime_monitor: runtime_monitor/monitor.py"
  echo "- online_lstm: enabled, score_mode=sigmoid"
  echo "- cgroup_workload: enabled, slice=$SLICE"
  echo "- workload_classifier: enabled"
  echo "- online_causal_markov: enabled"
  echo "- markov_debugfs_writer: enabled, strict=true"
  echo "- lstm_reclaim_policy: enabled, mode=observe, apply=0"
  echo "- app_scope_config: configs/runtime/runtime_app_scope.json"
  echo "- automation_scenario: $SCENARIO"
  echo "- duration_s: $DURATION"
  echo "- workload_generator_sequence: 0,1,2,0,1,6,0,1,2,0"
  echo "- workload_generator_budget: 256 MiB"
  echo "- stress: $([ "$RUN_STRESS" -eq 1 ] && echo bounded-3G-30s || echo disabled)"
  echo "- safety: observe-only; no lru_gen_pages, page action, Tier2, swap or kernel build"
} > "$WORK_DIR/config/selected_runtime_configuration.md"

cat "/boot/config-$(uname -r)" 2>/dev/null | rg '^(CONFIG_LRU_GEN|CONFIG_MEMCG|CONFIG_TIER2_WATERMARK|# CONFIG_TIER2_WATERMARK_MEMCG)' > "$WORK_DIR/precheck/relevant_kernel_config.txt" || true
if [[ -r "$DEBUGFS" ]]; then
  cat "$DEBUGFS" > "$WORK_DIR/kernel/debugfs_pre_clear.txt" 2>&1 || true
else
  printf 'debugfs_missing=%s\n' "$DEBUGFS" > "$WORK_DIR/kernel/debugfs_pre_clear.txt"
fi
for name in lru_gen_tier2_state lru_gen_tier2_stats; do
  if [[ -r "/sys/kernel/debug/$name" ]]; then cat "/sys/kernel/debug/$name" > "$WORK_DIR/kernel/${name}_before.txt"; else printf 'not_present\n' > "$WORK_DIR/kernel/${name}_before.txt"; fi
done

PERM_OUT="$WORK_DIR/logs/permission_stdout.log"
PERM_ERR="$WORK_DIR/logs/permission_stderr.log"
bash runtime_monitor/scripts/prepare_mglru_debugfs_access.sh --apply >"$PERM_OUT" 2>"$PERM_ERR" || true
printf '{"path":"%s","readable":%s,"writable":%s}\n' "$DEBUGFS" "$([[ -r "$DEBUGFS" ]] && echo true || echo false)" "$([[ -w "$DEBUGFS" ]] && echo true || echo false)" > "$WORK_DIR/kernel/debugfs_permission_result.json"
if [[ -w "$DEBUGFS" ]]; then printf 'clear all\n' > "$DEBUGFS" || true; elif sudo -n test -e "$DEBUGFS"; then printf 'clear all\n' | sudo -n tee "$DEBUGFS" >/dev/null || true; fi
if [[ -r "$DEBUGFS" ]]; then cat "$DEBUGFS" > "$WORK_DIR/kernel/debugfs_baseline_after_clear.txt" 2>&1 || true; fi

MON_OUT="$WORK_DIR/logs/monitor_stdout.log"
MON_ERR="$WORK_DIR/logs/monitor_stderr.log"
python3 runtime_monitor/monitor.py \
  --session-id "$SID" \
  --output-dir "$ROOT/outputs/runtime_monitor" \
  --duration "$DURATION" \
  --sample-interval 1 \
  --enable-online-lstm \
  --score-mode sigmoid \
  --enable-cgroup-workload \
  --enable-online-workload-markov \
  --app-scope-config configs/runtime/runtime_app_scope.json \
  --test-slice "$SLICE" \
  --cgroup-workload-slice "$SLICE" \
  --enable-mglru-markov-debugfs \
  --mglru-markov-strict \
  --enable-mglru-lstm-reclaim-policy \
  --mglru-lstm-reclaim-policy-config configs/runtime/mglru_lstm_reclaim_policy.json \
  --min-event-cooldown-s 0 \
  --periodic-refresh-s 30 \
  --prediction-ttl-s 30 \
  >"$MON_OUT" 2>"$MON_ERR" &
MON_PID=$!

AUTO_OUT="$WORK_DIR/logs/automation_stdout.log"
AUTO_ERR="$WORK_DIR/logs/automation_stderr.log"
automation/run_automation.sh \
  --scenario "$SCENARIO" \
  --session-id "$SID" \
  --scenario-id scenario_unified_pipeline_bilibili \
  --trace-output "$SESSION_DIR/model/automation_trace.csv" \
  --test-slice "$SLICE" \
  >"$AUTO_OUT" 2>"$AUTO_ERR" &
AUTO_PID=$!

CG=""
for _ in $(seq 1 40); do
  CG="$(systemctl --user show "$SLICE" -p ControlGroup --value 2>/dev/null || true)"
  FILES_CGROUP="/sys/fs/cgroup/${CG#/}/automation-files.scope"
  [[ -d "$FILES_CGROUP" ]] && break
  sleep 1
done

GEN_PID=""
GEN_OUT="$WORK_DIR/logs/generator_stdout.log"
GEN_ERR="$WORK_DIR/logs/generator_stderr.log"
MARKOV_SCOPE_PATH="${FILES_CGROUP:-}" python3 runtime_monitor/scripts/generate_markov_workload_sequence.py \
  --sequence 0,1,2,0,1,6,0,1,2,0 \
  --phase-duration-s 4 \
  --anon-mb 16 \
  --file-mb 16 \
  --hold-after-s 25 \
  --max-memory-mb 256 \
  --output "$WORK_DIR/workload/workload_generator_trace.csv" \
  >"$GEN_OUT" 2>"$GEN_ERR" &
GEN_PID=$!
if [[ -d "${FILES_CGROUP:-}" ]]; then
  if printf '%s\n' "$GEN_PID" > "$FILES_CGROUP/cgroup.procs" 2>/dev/null; then :
  elif sudo -n sh -c "printf '%s\\n' '$GEN_PID' > '$FILES_CGROUP/cgroup.procs'" 2>/dev/null; then :
  else printf 'attach_failed\n' >> "$WORK_DIR/kernel/test_scope_membership.md"; fi
fi
PROC_CGROUP="$(cat "/proc/$GEN_PID/cgroup" 2>/dev/null || true)"
ATTACHED=false
if [[ -n "${FILES_CGROUP:-}" && "$PROC_CGROUP" == *"$FILES_CGROUP"* ]]; then ATTACHED=true; fi
{
  echo "session_id: $SID"
  echo "slice_control_group: ${CG:-missing}"
  echo "scope_path: ${FILES_CGROUP:-missing}"
  echo "generator_pid: $GEN_PID"
  if [[ -n "$GEN_PID" ]]; then
    echo "proc_cgroup: $(tr '\n' ';' <<< "$PROC_CGROUP")"
  fi
} > "$WORK_DIR/kernel/test_scope_membership.md"
printf '{"session_id":"%s","generator_pid":%s,"slice_control_group":"%s","scope_path":"%s","proc_cgroup":"%s","attached":%s}\n' "$SID" "${GEN_PID:-0}" "${CG:-}" "${FILES_CGROUP:-}" "${PROC_CGROUP//$'\n'/;}" "$ATTACHED" > "$WORK_DIR/kernel/test_scope_membership.json"

if [[ "$RUN_STRESS" -eq 1 ]]; then
  systemd-run --user --scope --slice="$SLICE" --unit="unified-pipeline-pressure-$TS" \
    stress-ng --vm 1 --vm-bytes 3G --vm-keep --timeout 30s \
    >"$WORK_DIR/logs/stress_stdout.log" 2>"$WORK_DIR/logs/stress_stderr.log" &
  STRESS_PID=$!
else
  STRESS_PID=""
  : > "$WORK_DIR/logs/stress_stdout.log"
  : > "$WORK_DIR/logs/stress_stderr.log"
fi

(
  sleep "$((DURATION / 3))"
  [[ -r "$DEBUGFS" ]] && cat "$DEBUGFS" > "$WORK_DIR/kernel/debugfs_during_1.txt" 2>&1 || true
  sleep "$((DURATION / 3))"
  [[ -r "$DEBUGFS" ]] && cat "$DEBUGFS" > "$WORK_DIR/kernel/debugfs_during_2.txt" 2>&1 || true
) & SNAP_PID=$!

wait "$MON_PID"; MON_RC=$?
kill "$GEN_PID" 2>/dev/null || true
wait "$GEN_PID" 2>/dev/null || true
kill "$AUTO_PID" 2>/dev/null || true
wait "$AUTO_PID" 2>/dev/null || true
[[ -n "${STRESS_PID:-}" ]] && wait "$STRESS_PID" 2>/dev/null || true
wait "$SNAP_PID" 2>/dev/null || true
[[ -r "$DEBUGFS" ]] && cat "$DEBUGFS" > "$WORK_DIR/kernel/debugfs_after.txt" 2>&1 || true
for name in lru_gen_tier2_state lru_gen_tier2_stats; do
  if [[ -r "/sys/kernel/debug/$name" ]]; then cat "/sys/kernel/debug/$name" > "$WORK_DIR/kernel/${name}_after.txt"; else printf 'not_present\n' > "$WORK_DIR/kernel/${name}_after.txt"; fi
done
bash runtime_monitor/scripts/prepare_mglru_debugfs_access.sh --restore > "$WORK_DIR/kernel/debugfs_permission_restore.txt" 2>&1 || true

AUDIT_WORK="$WORK_DIR/audit"
AUDIT_SHARE="$SHARE_DIR"
python3 runtime_monitor/scripts/run_pipeline_intermediate_audit.py \
  --session-dir "$SESSION_DIR" \
  --share-input "$SESSION_DIR" \
  --work-dir "$AUDIT_WORK" \
  --share-dir "$AUDIT_SHARE" > "$WORK_DIR/logs/audit_stdout.log" 2> "$WORK_DIR/logs/audit_stderr.log"
AUDIT_RC=$?

cp -a "$WORK_DIR/kernel/." "$AUDIT_WORK/kernel/" 2>/dev/null || true
cp -a "$WORK_DIR/precheck/." "$AUDIT_WORK/precheck/" 2>/dev/null || true
cp -a "$WORK_DIR/logs/." "$AUDIT_WORK/logs/" 2>/dev/null || true
cp -a "$SESSION_DIR/model/." "$SHARE_DIR/model/" 2>/dev/null || true
cp -a "$SESSION_DIR/review/." "$SHARE_DIR/review/" 2>/dev/null || true
cp -a "$WORK_DIR/workload/." "$SHARE_DIR/workload/" 2>/dev/null || true
cp -a "$WORK_DIR/config/." "$SHARE_DIR/config/" 2>/dev/null || true
git diff -- runtime_monitor automation configs > "$SHARE_DIR/source/relevant_source_diff.txt" 2>/dev/null || true
git status --short > "$SHARE_DIR/git_status.txt"

python3 runtime_monitor/scripts/build_unified_pipeline_summary.py \
  --session-dir "$SESSION_DIR" \
  --work-dir "$WORK_DIR" \
  --share-dir "$SHARE_DIR" \
  --audit-exit-code "$AUDIT_RC" \
  --monitor-exit-code "$MON_RC" \
  --scenario "$SCENARIO" \
  > "$WORK_DIR/logs/summary_builder.log" 2>&1 || true

tar -czf "${SHARE_DIR}.tar.gz" -C "$(dirname "$SHARE_DIR")" "$(basename "$SHARE_DIR")"
sha256sum "${SHARE_DIR}.tar.gz" > "${SHARE_DIR}.tar.gz.sha256"
printf 'SID=%s\nSESSION_DIR=%s\nWORK_DIR=%s\nSHARE_DIR=%s\nTAR=%s.tar.gz\nMONITOR_RC=%s\nAUDIT_RC=%s\n' "$SID" "$SESSION_DIR" "$WORK_DIR" "$SHARE_DIR" "$SHARE_DIR" "$MON_RC" "$AUDIT_RC"
exit "$MON_RC"
