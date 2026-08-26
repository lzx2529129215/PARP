#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
DEBUGFS_PATH="${DEBUGFS_PATH:-/sys/kernel/debug/lru_gen_workload_markov}"
MONITOR_DURATION_S="${MONITOR_DURATION_S:-180}"
MARKOV_WAIT_TIMEOUT_S="${MARKOV_WAIT_TIMEOUT_S:-210}"
PRESSURE_TIMEOUT_S="${PRESSURE_TIMEOUT_S:-30}"
TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
SID="${SID:-session_prediction_hit_$TIMESTAMP}"
OUT_DIR="$PROJECT_ROOT/outputs/mglru/prediction_hit_$SID"
SESSION_DIR="$PROJECT_ROOT/outputs/runtime_monitor/$SID"
WRITES_CSV="$SESSION_DIR/model/mglru_markov_debugfs_writes.csv"
BEFORE_STRESS="$OUT_DIR/debugfs_before_stress.txt"
AFTER_STRESS="$OUT_DIR/debugfs_after_stress.txt"
SUMMARY="$OUT_DIR/prediction_hit_summary.md"
MONITOR_LOG="$OUT_DIR/monitor.log"
AUTOMATION_LOG="$OUT_DIR/automation.log"
READY_FILE="$OUT_DIR/pressure_scope.ready"
GO_FILE="$OUT_DIR/pressure_scope.go"
PRESSURE_UNIT="prediction-hit-${TIMESTAMP}"

mkdir -p "$OUT_DIR"
cd "$PROJECT_ROOT"

log()
{
	printf '[prediction-hit] %s\n' "$*"
}

stat_value()
{
	local path="$1"
	local name="$2"
	awk -v key="$name" '$1 == "stat" && $2 == key { print $3; exit }' "$path"
}

count_lines()
{
	local path="$1"
	local prefix="$2"
	awk -v prefix="$prefix" 'index($0, prefix) == 1 { count++ } END { print count + 0 }' "$path"
}

result_from_file()
{
	local path="$1"
	if [[ -f "$path" ]] && grep -Eq 'final_result[^A-Z]*(\*\*)?PASS(\*\*)?' "$path"; then
		printf 'PASS\n'
	else
		printf 'FAIL\n'
	fi
}

markov_ok_count()
{
	if [[ ! -f "$WRITES_CSV" ]]; then
		printf '0\n'
		return
	fi
	awk -F, '$3 == "markov_set" && $5 == "ok" { count++ } END { print count + 0 }' "$WRITES_CSV"
}

if ! sudo -n true; then
	log "FAIL: 需要免密 sudo 读取和写入测试 debugfs。"
	exit 1
fi

sudo -n mount -t debugfs none /sys/kernel/debug 2>/dev/null || true
if [[ ! -e "$DEBUGFS_PATH" ]]; then
	log "FAIL: debugfs 接口不存在: $DEBUGFS_PATH"
	exit 1
fi

initial_debugfs="$(sudo -n cat "$DEBUGFS_PATH")"
for field in prepare_calls per_folio_calls predictions; do
	if ! grep -q "^stat $field" <<<"$initial_debugfs"; then
		log "FAIL: 当前内核缺少 stat $field"
		exit 1
	fi
done

sudo -n chmod o+x /sys/kernel/debug
sudo -n chmod o+rw "$DEBUGFS_PATH"
printf 'clear all\n' | sudo -n tee "$DEBUGFS_PATH" >/dev/null

log "uname=$(uname -r)"
log "sid=$SID"
log "启动 Runtime Monitor，duration=${MONITOR_DURATION_S}s"
python3 runtime_monitor/monitor.py \
	--session-id "$SID" \
	--output-dir outputs/runtime_monitor \
	--duration "$MONITOR_DURATION_S" \
	--sample-interval 1 \
	--enable-online-lstm \
	--enable-cgroup-workload \
	--app-scope-config configs/runtime/runtime_app_scope.json \
	--enable-mglru-markov-debugfs \
	--mglru-markov-strict \
	--min-event-cooldown-s 0 \
	>"$MONITOR_LOG" 2>&1 &
monitor_pid=$!

sleep 3
log "启动 automation"
set +e
automation/run_automation.sh \
	--scenario configs/automation/scenario_local_files.json \
	--trace-output "$SESSION_DIR/model/automation_trace.csv" \
	--session-id "$SID" \
	--scenario-id scenario_local_files \
	--test-slice huawei-test.slice \
	>"$AUTOMATION_LOG" 2>&1
automation_rc=$?
set -e

deadline=$((SECONDS + MARKOV_WAIT_TIMEOUT_S))
while (( SECONDS < deadline )); do
	if (( $(markov_ok_count) > 0 )); then
		break
	fi
	if ! kill -0 "$monitor_pid" 2>/dev/null; then
		break
	fi
	sleep 2
done

set +e
wait "$monitor_pid"
monitor_rc=$?
set -e
markov_ok="$(markov_ok_count)"
if (( markov_ok == 0 )); then
	log "FAIL: 未观察到成功的 markov_set 写入。"
	exit 1
fi
log "monitor_rc=$monitor_rc automation_rc=$automation_rc markov_set_ok=$markov_ok"

read -r app_id_from_csv _cgroup_id_from_csv < <(
	python3 - "$WRITES_CSV" <<'PY'
import csv
import sys

last = None
with open(sys.argv[1], newline="", encoding="utf-8") as f:
    for row in csv.DictReader(f):
        if row.get("event_type") == "current_app" and row.get("status") == "ok":
            last = row
if last is None:
    raise SystemExit(1)
print(last.get("foreground_app_id", ""), last.get("foreground_cgroup_id", ""))
PY
)
if [[ -z "$app_id_from_csv" ]]; then
	log "FAIL: 无法从 writes CSV 提取 current_app。"
	exit 1
fi

rm -f "$READY_FILE" "$GO_FILE"
if command -v stress-ng >/dev/null 2>&1; then
	pressure_command="touch '$READY_FILE'; while [[ ! -e '$GO_FILE' ]]; do sleep 0.1; done; exec stress-ng --vm 1 --vm-bytes 70% --timeout '${PRESSURE_TIMEOUT_S}s'"
	pressure_backend="stress-ng"
else
	pressure_command="touch '$READY_FILE'; while [[ ! -e '$GO_FILE' ]]; do sleep 0.1; done; exec python3 -c 'import time; blocks=[]\ntry:\n [blocks.append(bytearray(256*1024*1024)) or time.sleep(0.5) for _ in range(20)]\nexcept MemoryError:\n pass\ntime.sleep(10)'"
	pressure_backend="python_fallback"
fi

log "创建受控压力 scope: ${PRESSURE_UNIT}.scope"
systemd-run --user --scope \
	--slice=huawei-test.slice \
	--unit="$PRESSURE_UNIT" \
	-- bash -c "$pressure_command" \
	>"$OUT_DIR/pressure.log" 2>&1 &
pressure_runner_pid=$!

ready_deadline=$((SECONDS + 20))
while [[ ! -e "$READY_FILE" ]] && (( SECONDS < ready_deadline )); do
	sleep 0.1
done
if [[ ! -e "$READY_FILE" ]]; then
	log "FAIL: 压力 scope 未就绪。"
	kill "$pressure_runner_pid" 2>/dev/null || true
	exit 1
fi

control_group="$(systemctl --user show "${PRESSURE_UNIT}.scope" -p ControlGroup --value)"
scope_path="/sys/fs/cgroup${control_group}"
if [[ -z "$control_group" || ! -d "$scope_path" ]]; then
	log "FAIL: 无法解析压力 scope cgroup: ${PRESSURE_UNIT}.scope"
	kill "$pressure_runner_pid" 2>/dev/null || true
	exit 1
fi
cgroup_id_used="$(stat -c '%i' "$scope_path")"
app_id_used="$app_id_from_csv"

log "controlled injection: app_id=$app_id_used cgroup_id=$cgroup_id_used"
printf 'app current %s %s 300000\n' "$app_id_used" "$cgroup_id_used" |
	sudo -n tee "$DEBUGFS_PATH" >/dev/null
printf 'workload update %s %s 0\n' "$cgroup_id_used" "$app_id_used" |
	sudo -n tee "$DEBUGFS_PATH" >/dev/null
printf 'workload update %s %s 2\n' "$cgroup_id_used" "$app_id_used" |
	sudo -n tee "$DEBUGFS_PATH" >/dev/null
printf 'markov set %s 0 2 3 9000 2\n' "$app_id_used" |
	sudo -n tee "$DEBUGFS_PATH" >/dev/null

sudo -n cat "$DEBUGFS_PATH" >"$BEFORE_STRESS"
touch "$GO_FILE"
set +e
wait "$pressure_runner_pid"
pressure_rc=$?
set -e
sudo -n cat "$DEBUGFS_PATH" >"$AFTER_STRESS"

prepare_before="$(stat_value "$BEFORE_STRESS" prepare_calls)"
prepare_after="$(stat_value "$AFTER_STRESS" prepare_calls)"
per_folio_after="$(stat_value "$AFTER_STRESS" per_folio_calls)"
predictions_before="$(stat_value "$BEFORE_STRESS" predictions)"
predictions_after="$(stat_value "$AFTER_STRESS" predictions)"
predictions_delta=$((predictions_after - predictions_before))
missing_app_after="$(stat_value "$AFTER_STRESS" missing_app)"
missing_hint_after="$(stat_value "$AFTER_STRESS" missing_hint)"
missing_transition_after="$(stat_value "$AFTER_STRESS" missing_transition)"
throttled_prepare_after="$(stat_value "$AFTER_STRESS" throttled_prepare)"
hist_lines_count="$(count_lines "$AFTER_STRESS" 'hist ')"
markov_lines_count="$(count_lines "$AFTER_STRESS" 'markov ')"
hint_lines_count="$(count_lines "$AFTER_STRESS" 'hint ')"

python3 runtime_monitor/scripts/check_online_lstm_prediction.py \
	--session-dir "$SESSION_DIR" \
	>"$OUT_DIR/online_lstm_check.log" 2>&1 || true

online_lstm_result="$(result_from_file "$SESSION_DIR/review/online_lstm_prediction_summary.md")"
cgroup_workload_result="$(result_from_file "$SESSION_DIR/review/cgroup_memory_workload_summary.md")"
workload_classifier_result="$(result_from_file "$SESSION_DIR/review/cgroup_workload_state_summary.md")"
workload_markov_builder_result="$(result_from_file "$SESSION_DIR/review/workload_markov_summary.md")"
mglru_debugfs_strict_result="$(result_from_file "$SESSION_DIR/review/mglru_markov_debugfs_summary.md")"

final_result="PASS"
if (( prepare_after <= prepare_before )) ||
   (( per_folio_after != 0 )) ||
   (( predictions_delta <= 0 )) ||
   (( hist_lines_count <= 0 )) ||
   (( markov_lines_count <= 0 )) ||
   [[ "$online_lstm_result" != "PASS" ]] ||
   [[ "$cgroup_workload_result" != "PASS" ]] ||
   [[ "$workload_classifier_result" != "PASS" ]] ||
   [[ "$workload_markov_builder_result" != "PASS" ]] ||
   [[ "$mglru_debugfs_strict_result" != "PASS" ]]; then
	final_result="FAIL"
fi

cat >"$SUMMARY" <<EOF
# MGLRU Markov Prediction-Hit 运行态验证汇总

- uname_r: \`$(uname -r)\`
- sid: \`$SID\`
- runtime_session_dir: \`$SESSION_DIR\`
- debugfs_path: \`$DEBUGFS_PATH\`
- controlled_injection: true
- current_app_refreshed: true
- app_id_used: $app_id_used
- cgroup_id_used: $cgroup_id_used
- pressure_backend: $pressure_backend
- pressure_exit_code: $pressure_rc
- prepare_calls_before_stress: $prepare_before
- prepare_calls_after_stress: $prepare_after
- per_folio_calls_after_stress: $per_folio_after
- predictions_before_stress: $predictions_before
- predictions_after_stress: $predictions_after
- predictions_delta: $predictions_delta
- missing_app_after_stress: $missing_app_after
- missing_hint_after_stress: $missing_hint_after
- missing_transition_after_stress: $missing_transition_after
- throttled_prepare_after_stress: $throttled_prepare_after
- hist_lines_count: $hist_lines_count
- markov_lines_count: $markov_lines_count
- hint_lines_count: $hint_lines_count
- online_lstm_result: $online_lstm_result
- cgroup_workload_result: $cgroup_workload_result
- workload_classifier_result: $workload_classifier_result
- workload_markov_builder_result: $workload_markov_builder_result
- mglru_debugfs_strict_result: $mglru_debugfs_strict_result
- final_result: $final_result

## 约束确认

- 未写 \`/sys/kernel/debug/lru_gen_pages\`。
- 未使用 eBPF 或 BPF kfunc。
- 未调用 promote、depromote 或 protect。
- generation adjustment 保持 no-op。
- 未改变 MGLRU reclaim、aging、isolate 或 reheat 行为。
- 未引入预取、主动驱逐或 swap 修改。
EOF

log "summary=$SUMMARY"
log "prepare=$prepare_before->$prepare_after predictions=$predictions_before->$predictions_after"
log "final_result=$final_result"
[[ "$final_result" == "PASS" ]]
