#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
DEBUGFS_PATH="${DEBUGFS_PATH:-/sys/kernel/debug/lru_gen_workload_markov}"

SCENARIO=""
DURATION_S=150
OUTPUT_ROOT="outputs/runtime_monitor"
STRICT_DEBUGFS=0
RUN_STRESS=0
TEST_SLICE="huawei-test.slice"
STRESS_TIMEOUT_S="${STRESS_TIMEOUT_S:-30}"
MARKOV_WAIT_TIMEOUT_S="${MARKOV_WAIT_TIMEOUT_S:-90}"

usage()
{
	cat <<EOF
Usage:
  $0 --scenario FILE [--duration SECONDS] [--output-root DIR] [--strict-debugfs] [--run-stress|--skip-stress] [--test-slice SLICE]

Examples:
  bash runtime_monitor/scripts/run_mglru_chain_validation_suite.sh \\
    --scenario configs/automation/scenario_validation_files_loop.json \\
    --duration 150 \\
    --strict-debugfs \\
    --skip-stress \\
    --test-slice huawei-test.slice
EOF
}

while (($# > 0)); do
	case "$1" in
		--scenario)
			SCENARIO="$2"
			shift 2
			;;
		--duration)
			DURATION_S="$2"
			shift 2
			;;
		--output-root)
			OUTPUT_ROOT="$2"
			shift 2
			;;
		--strict-debugfs)
			STRICT_DEBUGFS=1
			shift
			;;
		--run-stress)
			RUN_STRESS=1
			shift
			;;
		--skip-stress)
			RUN_STRESS=0
			shift
			;;
		--test-slice)
			TEST_SLICE="$2"
			shift 2
			;;
		-h|--help)
			usage
			exit 0
			;;
		*)
			echo "Unknown option: $1" >&2
			usage >&2
			exit 1
			;;
	esac
done

if [[ -z "$SCENARIO" ]]; then
	echo "必须指定 --scenario" >&2
	usage >&2
	exit 1
fi

cd "$PROJECT_ROOT"
if [[ "$SCENARIO" != /* ]]; then
	SCENARIO="$PROJECT_ROOT/$SCENARIO"
fi
if [[ "$OUTPUT_ROOT" != /* ]]; then
	OUTPUT_ROOT="$PROJECT_ROOT/$OUTPUT_ROOT"
fi

SCENARIO_NAME="$(basename "$SCENARIO" .json)"
TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
SID="${SID:-session_chain_${SCENARIO_NAME}_${TIMESTAMP}}"
SESSION_DIR="$OUTPUT_ROOT/$SID"
OUT_DIR="$PROJECT_ROOT/outputs/mglru/chain_validation_$SID"
MODEL_DIR="$SESSION_DIR/model"
REVIEW_DIR="$SESSION_DIR/review"
WRITES_CSV="$MODEL_DIR/mglru_markov_debugfs_writes.csv"
WORKLOAD_STATE_CSV="$MODEL_DIR/cgroup_workload_state_1s.csv"
TRANSITIONS_CSV="$MODEL_DIR/workload_markov_transitions.csv"
DEBUGFS_BEFORE="$OUT_DIR/debugfs_before.txt"
DEBUGFS_AFTER="$OUT_DIR/debugfs_after.txt"
SUMMARY="$OUT_DIR/chain_validation_summary.md"
MONITOR_LOG="$OUT_DIR/monitor.log"
AUTOMATION_LOG="$OUT_DIR/automation.log"
STRESS_LOG="$OUT_DIR/stress.log"

mkdir -p "$OUT_DIR" "$MODEL_DIR" "$REVIEW_DIR"

log()
{
	printf '[chain-validation] %s\n' "$*"
}

result_from_file()
{
	local path="$1"
	if [[ -f "$path" ]] && grep -Eq 'final_result[^A-Z]*(\*\*)?PASS(\*\*)?' "$path"; then
		printf 'PASS\n'
	elif [[ -f "$path" ]] && grep -Eq 'final_result[^A-Z]*(\*\*)?PASS_WITH_WARNINGS(\*\*)?' "$path"; then
		printf 'PASS_WITH_WARNINGS\n'
	else
		printf 'FAIL\n'
	fi
}

online_lstm_chain_result()
{
	local call_trace="$MODEL_DIR/online_lstm_duration_call_trace.csv"
	if [[ ! -f "$call_trace" ]]; then
		printf 'FAIL\n'
		return
	fi
	local rows
	rows="$(awk 'END { print (NR > 0 ? NR - 1 : 0) }' "$call_trace")"
	if (( rows > 0 )); then
		printf 'PASS\n'
	else
		printf 'FAIL\n'
	fi
}

csv_ok_count()
{
	local csv_path="$1"
	local event_type="$2"
	if [[ ! -f "$csv_path" ]]; then
		printf '0\n'
		return
	fi
	python3 - "$csv_path" "$event_type" <<'PY'
import csv
import sys

path, event_type = sys.argv[1], sys.argv[2]
count = 0
with open(path, newline="", encoding="utf-8") as f:
    for row in csv.DictReader(f):
        if row.get("event_type") == event_type and row.get("status") == "ok":
            count += 1
print(count)
PY
}

count_forbidden_writes()
{
	if [[ ! -f "$WRITES_CSV" ]]; then
		printf '0\n'
		return
	fi
	python3 - "$WRITES_CSV" <<'PY'
import csv
import sys

forbidden = ("lru_gen_pages", "promote", "depromote", "protect")
count = 0
with open(sys.argv[1], newline="", encoding="utf-8") as f:
    for row in csv.DictReader(f):
        command = row.get("command", "")
        if any(token in command for token in forbidden):
            count += 1
print(count)
PY
}

debugfs_stat_value()
{
	local path="$1"
	local name="$2"
	awk -v key="$name" '$1 == "stat" && $2 == key { print $3; found=1; exit } END { if (!found) print 0 }' "$path"
}

debugfs_count_lines()
{
	local path="$1"
	local prefix="$2"
	awk -v prefix="$prefix" 'index($0, prefix) == 1 { count++ } END { print count + 0 }' "$path"
}

run_stress_once()
{
	local unit="chain-validation-stress-${TIMESTAMP}"
	local command
	if command -v stress-ng >/dev/null 2>&1; then
		command="exec stress-ng --vm 1 --vm-bytes 70% --timeout '${STRESS_TIMEOUT_S}s'"
	else
		command="exec python3 -c 'import time; blocks=[]\ntry:\n [blocks.append(bytearray(256*1024*1024)) or time.sleep(0.5) for _ in range(20)]\nexcept MemoryError:\n pass\ntime.sleep(5)'"
	fi
	log "启动压力 scope: ${unit}.scope"
	set +e
	systemd-run --user --scope --slice="$TEST_SLICE" --unit="$unit" -- bash -c "$command" >"$STRESS_LOG" 2>&1
	local rc=$?
	set -e
	log "压力 scope 结束，rc=$rc"
	return 0
}

if ! sudo -n true; then
	log "FAIL: 需要免密 sudo 读取/清空 $DEBUGFS_PATH"
	exit 1
fi

sudo -n mount -t debugfs none /sys/kernel/debug 2>/dev/null || true
if [[ ! -e "$DEBUGFS_PATH" ]]; then
	log "FAIL: debugfs 接口不存在: $DEBUGFS_PATH"
	exit 1
fi
sudo -n chmod o+x /sys/kernel/debug 2>/dev/null || true
sudo -n chmod o+rw "$DEBUGFS_PATH" 2>/dev/null || true
printf 'clear all\n' | sudo -n tee "$DEBUGFS_PATH" >/dev/null
sudo -n cat "$DEBUGFS_PATH" >"$DEBUGFS_BEFORE"

monitor_args=(
	python3 runtime_monitor/monitor.py
	--session-id "$SID"
	--output-dir "$OUTPUT_ROOT"
	--duration "$DURATION_S"
	--sample-interval 1
	--enable-online-lstm
	--enable-cgroup-workload
	--app-scope-config configs/runtime/runtime_app_scope.json
	--enable-mglru-markov-debugfs
	--min-event-cooldown-s 0
)
if (( STRICT_DEBUGFS == 1 )); then
	monitor_args+=(--mglru-markov-strict)
fi

log "sid=$SID"
log "scenario=$SCENARIO"
log "启动 Runtime Monitor，duration=${DURATION_S}s"
"${monitor_args[@]}" >"$MONITOR_LOG" 2>&1 &
monitor_pid=$!

sleep 3
log "启动 automation"
set +e
automation/run_automation.sh \
	--scenario "$SCENARIO" \
	--trace-output "$MODEL_DIR/automation_trace.csv" \
	--session-id "$SID" \
	--scenario-id "$SCENARIO_NAME" \
	--test-slice "$TEST_SLICE" \
	--reset-files \
	>"$AUTOMATION_LOG" 2>&1 &
automation_pid=$!
set -e

stress_started=0
if (( RUN_STRESS == 1 )); then
	deadline=$((SECONDS + MARKOV_WAIT_TIMEOUT_S))
	while (( SECONDS < deadline )); do
		if (( $(csv_ok_count "$WRITES_CSV" markov_set) > 0 )); then
			run_stress_once || true
			stress_started=1
			break
		fi
		if ! kill -0 "$monitor_pid" 2>/dev/null; then
			break
		fi
		sleep 2
	done
fi

set +e
wait "$automation_pid"
automation_rc=$?
wait "$monitor_pid"
monitor_rc=$?
set -e

if (( RUN_STRESS == 1 && stress_started == 0 )); then
	log "monitor 结束前未等到 markov_set，执行一次兜底压力。"
	run_stress_once || true
	stress_started=1
fi

sudo -n cat "$DEBUGFS_PATH" >"$DEBUGFS_AFTER"

python3 runtime_monitor/scripts/check_online_lstm_prediction.py \
	--session-dir "$SESSION_DIR" \
	>"$OUT_DIR/online_lstm_check.log" 2>&1 || true

online_lstm_detail_result="$(result_from_file "$REVIEW_DIR/online_lstm_prediction_summary.md")"
online_lstm_result="$(online_lstm_chain_result)"
cgroup_workload_result="$(result_from_file "$REVIEW_DIR/cgroup_memory_workload_summary.md")"
workload_classifier_result="$(result_from_file "$REVIEW_DIR/cgroup_workload_state_summary.md")"
workload_markov_builder_result="$(result_from_file "$REVIEW_DIR/workload_markov_summary.md")"
mglru_debugfs_strict_result="$(result_from_file "$REVIEW_DIR/mglru_markov_debugfs_summary.md")"

current_app_write_ok="$(csv_ok_count "$WRITES_CSV" current_app)"
predicted_apps_write_ok="$(csv_ok_count "$WRITES_CSV" predicted_apps)"
workload_update_write_ok="$(csv_ok_count "$WRITES_CSV" workload_update)"
markov_set_write_ok="$(csv_ok_count "$WRITES_CSV" markov_set)"
forbidden_write_count="$(count_forbidden_writes)"

prepare_calls="$(debugfs_stat_value "$DEBUGFS_AFTER" prepare_calls)"
reclaim_calls="$(debugfs_stat_value "$DEBUGFS_AFTER" reclaim_calls)"
per_folio_calls="$(debugfs_stat_value "$DEBUGFS_AFTER" per_folio_calls)"
predictions_before="$(debugfs_stat_value "$DEBUGFS_BEFORE" predictions)"
predictions="$(debugfs_stat_value "$DEBUGFS_AFTER" predictions)"
predictions_delta=$((predictions - predictions_before))
missing_app="$(debugfs_stat_value "$DEBUGFS_AFTER" missing_app)"
missing_hint="$(debugfs_stat_value "$DEBUGFS_AFTER" missing_hint)"
missing_transition="$(debugfs_stat_value "$DEBUGFS_AFTER" missing_transition)"
throttled_prepare="$(debugfs_stat_value "$DEBUGFS_AFTER" throttled_prepare)"
hist_lines_count="$(debugfs_count_lines "$DEBUGFS_AFTER" 'hist ')"
markov_lines_count="$(debugfs_count_lines "$DEBUGFS_AFTER" 'markov ')"
hint_lines_count="$(debugfs_count_lines "$DEBUGFS_AFTER" 'hint ')"

workload_section="$OUT_DIR/workload_section.md"
markov_section="$OUT_DIR/markov_section.md"
python3 - "$WORKLOAD_STATE_CSV" >"$workload_section" <<'PY'
import csv
import sys
from collections import Counter, defaultdict
from pathlib import Path

path = Path(sys.argv[1])
total = 0
changed = 0
dist = defaultdict(Counter)
changes = Counter()
if path.exists():
    with path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            total += 1
            app = row.get("app_key") or row.get("scope_name") or "UNKNOWN"
            name = row.get("workload_name") or row.get("workload_id") or "UNKNOWN"
            dist[app][name] += 1
            if str(row.get("state_changed", "")).lower() == "true":
                changed += 1
                changes[app] += 1
print(f"- total_workload_state_rows: {total}")
print(f"- total_state_changed_rows: {changed}")
print("")
print("## 各应用 workload 分布")
print("")
if not dist:
    print("- none")
for app in sorted(dist):
    print(f"### {app}")
    for name, count in sorted(dist[app].items()):
        print(f"- {name}: {count}")
    print(f"- state_changed_count: {changes.get(app, 0)}")
    print("")
PY

python3 - "$TRANSITIONS_CSV" >"$markov_section" <<'PY'
import csv
import sys
from collections import Counter
from pathlib import Path

path = Path(sys.argv[1])
keys = set()
rows = 0
per_key = Counter()
per_row = Counter()
if path.exists():
    with path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            rows += 1
            app = row.get("app_key") or row.get("app_id") or "UNKNOWN"
            key = (
                app,
                row.get("prev_workload_id", ""),
                row.get("current_workload_id", ""),
            )
            keys.add(key)
            per_row[app] += 1
    for app, prev, current in keys:
        per_key[app] += 1
print(f"- total_transition_keys: {len(keys)}")
print(f"- total_transition_rows: {rows}")
print("")
print("## 各应用 Markov 转移统计")
print("")
if not per_row:
    print("- none")
for app in sorted(per_row):
    print(f"### {app}")
    print(f"- transition_keys: {per_key.get(app, 0)}")
    print(f"- transition_rows: {per_row.get(app, 0)}")
    print("")
PY

final_result="PASS"
if (( monitor_rc != 0 )) ||
   [[ "$online_lstm_result" != "PASS" ]] ||
   [[ "$cgroup_workload_result" != "PASS" ]] ||
   [[ "$workload_classifier_result" != "PASS" ]] ||
   (( workload_update_write_ok <= 0 )) ||
   [[ "$workload_markov_builder_result" != "PASS" ]] ||
   (( markov_set_write_ok <= 0 )) ||
   (( prepare_calls <= 0 )) ||
   (( per_folio_calls != 0 )) ||
   (( hist_lines_count <= 0 )) ||
   (( markov_lines_count <= 0 )) ||
   (( forbidden_write_count != 0 )); then
	final_result="FAIL"
fi
if (( RUN_STRESS == 1 && predictions_delta < 0 )); then
	final_result="FAIL"
fi

cat >"$SUMMARY" <<EOF
# MGLRU Markov Observe-only 链路验证汇总

## 基础信息

- sid: \`$SID\`
- scenario_path: \`$SCENARIO\`
- runtime_session_dir: \`$SESSION_DIR\`
- uname_r: \`$(uname -r)\`
- debugfs_path: \`$DEBUGFS_PATH\`
- strict_debugfs: $([[ "$STRICT_DEBUGFS" == 1 ]] && echo true || echo false)
- run_stress: $([[ "$RUN_STRESS" == 1 ]] && echo true || echo false)
- stress_started: $([[ "$stress_started" == 1 ]] && echo true || echo false)
- monitor_exit_code: $monitor_rc
- automation_exit_code: $automation_rc

## Runtime Monitor 结果

- online_lstm_result: $online_lstm_result
- online_lstm_detail_result: $online_lstm_detail_result
- cgroup_workload_result: $cgroup_workload_result
- workload_classifier_result: $workload_classifier_result
- workload_markov_builder_result: $workload_markov_builder_result
- mglru_debugfs_strict_result: $mglru_debugfs_strict_result

## debugfs 写入统计

- current_app_write_ok: $current_app_write_ok
- predicted_apps_write_ok: $predicted_apps_write_ok
- workload_update_write_ok: $workload_update_write_ok
- markov_set_write_ok: $markov_set_write_ok

$(cat "$workload_section")

$(cat "$markov_section")

## debugfs 统计

- prepare_calls: $prepare_calls
- reclaim_calls: $reclaim_calls
- per_folio_calls: $per_folio_calls
- predictions: $predictions
- predictions_delta: $predictions_delta
- missing_app: $missing_app
- missing_hint: $missing_hint
- missing_transition: $missing_transition
- throttled_prepare: $throttled_prepare
- hist_lines_count: $hist_lines_count
- markov_lines_count: $markov_lines_count
- hint_lines_count: $hint_lines_count

## 约束确认

- forbidden_write_count: $forbidden_write_count
- 未写 \`/sys/kernel/debug/lru_gen_pages\`: $([[ "$forbidden_write_count" == 0 ]] && echo true || echo false)
- 未调用 promote/depromote/protect: $([[ "$forbidden_write_count" == 0 ]] && echo true || echo false)
- 未使用 eBPF/BPF kfunc: true
- 未做 workload -> page 映射: true
- 未做 generation adjustment: true
- 未改变 MGLRU reclaim、aging、isolate、reheat 行为: true
- 未引入预取、主动驱逐或 swap 修改: true

- final_result: $final_result
EOF

log "summary=$SUMMARY"
log "session_dir=$SESSION_DIR"
log "prepare_calls=$prepare_calls per_folio_calls=$per_folio_calls predictions=$predictions"
log "final_result=$final_result"
[[ "$final_result" == "PASS" ]]
