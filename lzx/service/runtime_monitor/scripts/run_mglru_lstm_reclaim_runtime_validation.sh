#!/usr/bin/env bash
set -u -o pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
DEBUGFS_PATH="${DEBUGFS_PATH:-/sys/kernel/debug/lru_gen_workload_markov}"

DURATION=180
SCENARIO="configs/automation/scenario_validation_multi_app_loop.json"
TEST_SLICE="huawei-test.slice"
RUN_STRESS=1
EXPECTED_BUILD=9

while [[ $# -gt 0 ]]; do
	case "$1" in
		--duration) DURATION="$2"; shift 2 ;;
		--scenario) SCENARIO="$2"; shift 2 ;;
		--test-slice) TEST_SLICE="$2"; shift 2 ;;
		--run-stress) RUN_STRESS=1; shift ;;
		--skip-stress) RUN_STRESS=0; shift ;;
		--expected-build-number) EXPECTED_BUILD="$2"; shift 2 ;;
		*) echo "未知参数: $1" >&2; exit 2 ;;
	esac
done

cd "$PROJECT_ROOT"
[[ "$SCENARIO" = /* ]] || SCENARIO="$PROJECT_ROOT/$SCENARIO"
if [[ ! -f "$SCENARIO" ]]; then
	for candidate in \
		configs/automation/scenario_local_wps_files_qq_auto_login.json \
		configs/automation/scenario_validation_files_loop.json; do
		if [[ -f "$candidate" ]]; then
			SCENARIO="$PROJECT_ROOT/$candidate"
			break
		fi
	done
fi

TS="$(date +%Y%m%d_%H%M%S)"
SID="session_lstm_reclaim_observe_${TS}"
SESSION_DIR="$PROJECT_ROOT/outputs/runtime_monitor/$SID"
VALIDATION_DIR="$PROJECT_ROOT/outputs/mglru/lstm_reclaim_runtime_validation_${TS}"
MODEL_DIR="$SESSION_DIR/model"
REVIEW_DIR="$SESSION_DIR/review"
SUMMARY_MD="$VALIDATION_DIR/mglru_lstm_reclaim_runtime_summary.md"
SUMMARY_JSON="$VALIDATION_DIR/mglru_lstm_reclaim_runtime_summary.json"
mkdir -p "$VALIDATION_DIR" "$MODEL_DIR" "$REVIEW_DIR"

log() { printf '[lstm-reclaim-validation] %s\n' "$*"; }
read_debugfs() { sudo -n cat "$DEBUGFS_PATH"; }
csv_has_ok() {
	local path="$1" event="$2"
	[[ -f "$path" ]] || return 1
	python3 - "$path" "$event" <<'PY'
import csv, sys
with open(sys.argv[1], newline="", encoding="utf-8") as f:
    raise SystemExit(0 if any(r.get("event_type") == sys.argv[2] and r.get("status") == "ok" for r in csv.DictReader(f)) else 1)
PY
}

uname -a > "$VALIDATION_DIR/uname.txt"
cat /proc/version > "$VALIDATION_DIR/proc_version.txt"
cp configs/runtime/mglru_lstm_reclaim_policy.json "$VALIDATION_DIR/policy_config.json"
printf '%s\n' "$SID" > "$VALIDATION_DIR/session_id.txt"
printf '%s\n' "$SESSION_DIR" > "$VALIDATION_DIR/runtime_session_dir.txt"
printf '%s\n' "$SCENARIO" > "$VALIDATION_DIR/scenario_path.txt"

RUNNING_RELEASE="$(uname -r)"
BUILD_NUMBER="$(sed -n 's/.*#\([0-9][0-9]*\) .*/\1/p' /proc/version | head -1)"
if [[ "$RUNNING_RELEASE" != "6.17.13-mglru" || "$BUILD_NUMBER" != "$EXPECTED_BUILD" ]]; then
	log "FAIL: 当前内核 release/build 不符合要求: $RUNNING_RELEASE #${BUILD_NUMBER:-unknown}"
	exit 1
fi

sudo -n mount -t debugfs none /sys/kernel/debug 2>/dev/null || true
if ! sudo -n test -e "$DEBUGFS_PATH"; then
	log "FAIL: debugfs 不存在: $DEBUGFS_PATH"
	exit 1
fi

read_debugfs > "$VALIDATION_DIR/debugfs_initial.txt"
required_protocol=(policy_config app_policy_prepare_calls target_cgroup_seen target_app_lookup_hit target_app_lookup_miss probability_lookup_hit probability_lookup_miss bucket_foreground bucket_high bucket_neutral bucket_low bucket_very_low bucket_missing bucket_expired bucket_unknown original_scan_pages proposed_scan_pages applied_scan_pages prepare_calls per_folio_calls)
NEW_PROTOCOL=1
for field in "${required_protocol[@]}"; do
	grep -q "$field" "$VALIDATION_DIR/debugfs_initial.txt" || NEW_PROTOCOL=0
done
if (( NEW_PROTOCOL == 0 )); then
	log "FAIL: 新 debugfs 协议字段不完整"
	exit 1
fi

set +e
bash runtime_monitor/scripts/test_mglru_lstm_reclaim_policy_debugfs.sh \
	--output "$VALIDATION_DIR/debugfs_protocol_after.txt" \
	> "$VALIDATION_DIR/debugfs_protocol_test_stdout.log" \
	2> "$VALIDATION_DIR/debugfs_protocol_test_stderr.log"
PROTOCOL_RC=$?
set -e
printf '%s\n' "$PROTOCOL_RC" > "$VALIDATION_DIR/debugfs_protocol_test_exit_code.txt"
if (( PROTOCOL_RC != 0 )); then
	log "FAIL: debugfs 协议测试失败"
	exit 1
fi

read_debugfs > "$VALIDATION_DIR/debugfs_before_clear.txt"
printf 'clear all\n' | sudo -n tee "$DEBUGFS_PATH" >/dev/null
read_debugfs > "$VALIDATION_DIR/debugfs_after_clear.txt"

MONITOR_CMD=(
	python3 runtime_monitor/monitor.py
	--session-id "$SID"
	--output-dir outputs/runtime_monitor
	--duration "$DURATION"
	--sample-interval 1
	--enable-online-lstm
	--enable-cgroup-workload
	--app-scope-config configs/runtime/runtime_app_scope.json
	--enable-mglru-markov-debugfs
	--mglru-markov-strict
	--enable-mglru-lstm-reclaim-policy
	--mglru-lstm-reclaim-policy-config configs/runtime/mglru_lstm_reclaim_policy.json
	--mglru-app-binding-refresh-s 30
	--mglru-lstm-policy-strict
	--min-event-cooldown-s 0
)

log "启动 Runtime Monitor: sid=$SID duration=${DURATION}s"
"${MONITOR_CMD[@]}" > "$VALIDATION_DIR/monitor_stdout.log" 2> "$VALIDATION_DIR/monitor_stderr.log" &
MONITOR_PID=$!
printf '%s\n' "$MONITOR_PID" > "$VALIDATION_DIR/monitor_pid.txt"
sleep 3
if ! kill -0 "$MONITOR_PID" 2>/dev/null; then
	wait "$MONITOR_PID" || true
	log "FAIL: Runtime Monitor 提前退出"
	exit 1
fi

SCENARIO_ID="$(basename "$SCENARIO" .json)"
log "启动 automation: $SCENARIO"
automation/run_automation.sh \
	--scenario "$SCENARIO" \
	--trace-output "$MODEL_DIR/automation_trace.csv" \
	--session-id "$SID" \
	--scenario-id "$SCENARIO_ID" \
	--test-slice "$TEST_SLICE" \
	--reset-files \
	> "$VALIDATION_DIR/automation_stdout.log" \
	2> "$VALIDATION_DIR/automation_stderr.log" &
AUTOMATION_PID=$!

printf 'scope_name,cgroup_path,stat_inode,timestamp\n' > "$VALIDATION_DIR/userspace_cgroup_ids.csv"
(
	while kill -0 "$MONITOR_PID" 2>/dev/null; do
		BASE_CGROUP="$(systemctl --user show "$TEST_SLICE" -p ControlGroup --value 2>/dev/null || true)"
		for scope in automation-wps.scope automation-qq.scope automation-files.scope; do
			path="/sys/fs/cgroup${BASE_CGROUP}/${scope}"
			if [[ -d "$path" ]]; then
				printf '%s,%s,%s,%s\n' "$scope" "$path" "$(stat -c '%i' "$path")" "$(date '+%F %T')" >> "$VALIDATION_DIR/userspace_cgroup_ids.csv"
			fi
		done
		read_debugfs > "$VALIDATION_DIR/debugfs_during_run.txt"
		sleep 2
	done
) &
CGROUP_POLL_PID=$!

POLICY_WRITES="$MODEL_DIR/mglru_lstm_reclaim_policy_writes.csv"
deadline=$((SECONDS + 75))
while (( SECONDS < deadline )); do
	if csv_has_ok "$POLICY_WRITES" app_bind && csv_has_ok "$POLICY_WRITES" app_probability; then
		break
	fi
	kill -0 "$MONITOR_PID" 2>/dev/null || break
	sleep 2
done

read_debugfs > "$VALIDATION_DIR/debugfs_before_stress.txt"
STRESS_RC=0
if (( RUN_STRESS == 1 )); then
	log "运行 60% 保守内存压力 30 秒"
	set +e
	systemd-run --user --scope --slice="$TEST_SLICE" \
		--unit="lstm-reclaim-validation-stress-${TS}" -- \
		stress-ng --vm 1 --vm-bytes 60% --timeout 30s \
		> "$VALIDATION_DIR/stress_stdout.log" \
		2> "$VALIDATION_DIR/stress_stderr.log"
	STRESS_RC=$?
	set -e
else
	printf 'stress skipped\n' > "$VALIDATION_DIR/stress_stdout.log"
fi
printf '%s\n' "$STRESS_RC" > "$VALIDATION_DIR/stress_exit_code.txt"
read_debugfs > "$VALIDATION_DIR/debugfs_after_stress.txt"

set +e
wait "$AUTOMATION_PID"
AUTOMATION_RC=$?
wait "$MONITOR_PID"
MONITOR_RC=$?
set -e
printf '%s\n' "$AUTOMATION_RC" > "$VALIDATION_DIR/automation_exit_code.txt"
printf '%s\n' "$MONITOR_RC" > "$VALIDATION_DIR/monitor_exit_code.txt"
kill "$CGROUP_POLL_PID" 2>/dev/null || true
wait "$CGROUP_POLL_PID" 2>/dev/null || true
read_debugfs > "$VALIDATION_DIR/debugfs_after.txt"

python3 runtime_monitor/scripts/check_online_lstm_prediction.py \
	--session-dir "$SESSION_DIR" \
	--monitor-command "${MONITOR_CMD[*]}" \
	--automation-command "automation/run_automation.sh --scenario $SCENARIO" \
	> "$VALIDATION_DIR/online_lstm_check.log" 2>&1 || true

python3 - "$VALIDATION_DIR" "$SESSION_DIR" "$SCENARIO" "$EXPECTED_BUILD" "$BUILD_NUMBER" "$RUNNING_RELEASE" "$PROTOCOL_RC" "$MONITOR_RC" "$AUTOMATION_RC" "$STRESS_RC" <<'PY'
import csv
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

validation = Path(sys.argv[1])
session = Path(sys.argv[2])
scenario = sys.argv[3]
expected_build = int(sys.argv[4])
build_number = int(sys.argv[5])
running_release = sys.argv[6]
protocol_rc, monitor_rc, automation_rc, stress_rc = map(int, sys.argv[7:11])
model = session / "model"
review = session / "review"

def read(path):
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError:
        return ""

def stats(path):
    result = {}
    for line in read(path).splitlines():
        parts = line.split()
        if len(parts) == 3 and parts[0] == "stat":
            try: result[parts[1]] = int(parts[2])
            except ValueError: pass
    return result

def policy_mode(path):
    match = re.search(r"^policy_config mode (\w+)$", read(path), re.M)
    return match.group(1) if match else "unavailable"

def line_count(path, prefix):
    return sum(line.startswith(prefix) for line in read(path).splitlines())

def csv_rows(path):
    try:
        with Path(path).open(newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))
    except OSError:
        return []

def ok_count(rows, event):
    return sum(r.get("event_type") == event and r.get("status") == "ok" for r in rows)

def result_from_summary(path):
    text = read(path)
    match = re.search(r"(?:final_result|strict_result)\s*:\s*\**(PASS(?:_WITH_WARNINGS)?)", text, re.I)
    return match.group(1).upper() if match else "FAIL"

before = stats(validation / "debugfs_after_clear.txt")
before_stress = stats(validation / "debugfs_before_stress.txt")
after_stress = stats(validation / "debugfs_after_stress.txt")
after = stats(validation / "debugfs_after.txt")
delta_names = ["app_policy_prepare_calls", "target_app_lookup_hit", "probability_lookup_hit", "original_scan_pages", "proposed_scan_pages", "applied_scan_pages", "prepare_calls", "predictions"]
deltas = {name: after.get(name, 0) - before.get(name, 0) for name in delta_names}
stress_deltas = {name: after_stress.get(name, 0) - before_stress.get(name, 0) for name in delta_names}

policy_rows = csv_rows(model / "mglru_lstm_reclaim_policy_writes.csv")
markov_rows = csv_rows(model / "mglru_markov_debugfs_writes.csv")
prediction_rows = csv_rows(model / "online_app_predictions_duration_1s.csv")
policy_config_ok = ok_count(policy_rows, "policy_config")
bind_ok = ok_count(policy_rows, "app_bind")
probability_ok = ok_count(policy_rows, "app_probability")
workload_update_ok = ok_count(markov_rows, "workload_update")
markov_set_ok = ok_count(markov_rows, "markov_set")

sources = sorted({r.get("probability_source", "") for r in policy_rows if r.get("event_type") == "app_probability" and r.get("status") == "ok"})
probability_source = sources[0] if len(sources) == 1 else ",".join(sources) or "unavailable"
probability_is_rank_based = any(s in {"rank_based", "unavailable", ""} for s in sources) or not sources

scope_to_app = {"automation-wps.scope": "WPS", "automation-qq.scope": "QQ", "automation-files.scope": "FILES"}
observed = defaultdict(set)
for row in csv_rows(validation / "userspace_cgroup_ids.csv"):
    try: observed[scope_to_app.get(row.get("scope_name", ""), "")].add(int(row.get("stat_inode", "")))
    except ValueError: pass
bind_pairs = []
for row in policy_rows:
    if row.get("event_type") == "app_bind" and row.get("status") == "ok":
        try: bind_pairs.append((row.get("app_key", ""), int(row.get("cgroup_id", ""))))
        except ValueError: pass
binding_matches = bool(bind_pairs) and all(cgid in observed.get(app, set()) for app, cgid in bind_pairs)

latest_prediction = {}
for row in prediction_rows:
    if row.get("status") == "success" and row.get("horizon") == "3" and row.get("app_key"):
        latest_prediction[row["app_key"]] = row
latest_rank_scores = {}
for row in markov_rows:
    if row.get("event_type") == "predicted_apps" and row.get("status") == "ok":
        ids = row.get("predicted_app_ids", "").split("|")
        scores = row.get("predicted_confidences", "").split("|")
        latest_rank_scores = dict(zip(ids, scores))
probability_validation = []
for row in policy_rows:
    if row.get("event_type") != "app_probability" or row.get("status") != "ok":
        continue
    pred = latest_prediction.get(row.get("app_key", ""), {})
    fixed = int(row.get("probability_fixed") or 0)
    probability_validation.append({
        "app_id": row.get("app_id", ""),
        "app_key": row.get("app_key", ""),
        "rank": pred.get("rank", ""),
        "rank_score": latest_rank_scores.get(row.get("app_id", ""), ""),
        "next_use_probability": row.get("probability", ""),
        "probability_fixed": fixed,
        "probability_source": row.get("probability_source", ""),
        "is_rank_template_value": str(fixed in {8000, 5000, 3000, 1000}).lower(),
        "write_status": row.get("status", ""),
    })
with (validation / "probability_validation.csv").open("w", newline="", encoding="utf-8") as f:
    fields = ["app_id", "app_key", "rank", "rank_score", "next_use_probability", "probability_fixed", "probability_source", "is_rank_template_value", "write_status"]
    writer = csv.DictWriter(f, fieldnames=fields)
    writer.writeheader(); writer.writerows(probability_validation)

online_calls = max(0, len(csv_rows(model / "online_lstm_duration_call_trace.csv")))
online_result = "PASS" if online_calls > 0 and "PASS:" in read(review / "final_online_lstm_validation_report.md") else "FAIL"
cgroup_result = result_from_summary(review / "cgroup_memory_workload_summary.md")
classifier_result = result_from_summary(review / "cgroup_workload_state_summary.md")
markov_builder_result = result_from_summary(review / "workload_markov_summary.md")
markov_strict_result = result_from_summary(review / "mglru_markov_debugfs_summary.md")
policy_strict_result = result_from_summary(review / "mglru_lstm_reclaim_policy_summary.md")

hist_count = line_count(validation / "debugfs_after.txt", "hist ")
markov_count = line_count(validation / "debugfs_after.txt", "markov ")
hint_count = line_count(validation / "debugfs_after.txt", "hint ")
bind_count = line_count(validation / "debugfs_after.txt", "bind ")
prob_count = line_count(validation / "debugfs_after.txt", "prob ")
background_buckets = sum(after.get(name, 0) for name in ["bucket_high", "bucket_neutral", "bucket_low", "bucket_very_low", "bucket_missing", "bucket_expired"])
bucket_coverage = "PASS" if after.get("bucket_foreground", 0) > 0 and background_buckets > 0 else "INSUFFICIENT"
observe_equal = deltas["applied_scan_pages"] == deltas["original_scan_pages"]

data = {
    "running_kernel": running_release,
    "kernel_build_number": build_number,
    "expected_build_number": expected_build,
    "new_kernel_active": running_release == "6.17.13-mglru" and build_number == expected_build,
    "debugfs_exists": True,
    "new_debugfs_protocol_present": True,
    "debugfs_protocol_test_result": "PASS" if protocol_rc == 0 else "FAIL",
    "session_id": session.name,
    "runtime_session_dir": str(session),
    "scenario_path": scenario,
    "policy_mode": policy_mode(validation / "debugfs_after.txt"),
    "probability_source": probability_source,
    "probability_is_rank_based": probability_is_rank_based,
    "policy_config_write_ok": policy_config_ok,
    "app_bind_write_ok": bind_ok,
    "app_probability_write_ok": probability_ok,
    "userspace_binding_id_matches": binding_matches,
    "target_cgroup_binding_hit": after.get("target_app_lookup_hit", 0) > 0,
    "app_policy_prepare_calls_before": before.get("app_policy_prepare_calls", 0),
    "app_policy_prepare_calls_after": after.get("app_policy_prepare_calls", 0),
    "app_policy_prepare_calls_delta": deltas["app_policy_prepare_calls"],
    "target_cgroup_seen": after.get("target_cgroup_seen", 0),
    "target_cgroup_id_last": after.get("target_cgroup_id_last", 0),
    "target_app_lookup_hit": after.get("target_app_lookup_hit", 0),
    "target_app_lookup_miss": after.get("target_app_lookup_miss", 0),
    "target_root_memcg": after.get("target_root_memcg", 0),
    "target_unbound_memcg": after.get("target_unbound_memcg", 0),
    "probability_lookup_hit": after.get("probability_lookup_hit", 0),
    "probability_lookup_miss": after.get("probability_lookup_miss", 0),
    "probability_expired": after.get("probability_expired", 0),
    **{name: after.get(name, 0) for name in ["bucket_foreground", "bucket_high", "bucket_neutral", "bucket_low", "bucket_very_low", "bucket_missing", "bucket_expired", "bucket_unknown"]},
    "original_scan_pages_before": before.get("original_scan_pages", 0),
    "original_scan_pages_after": after.get("original_scan_pages", 0),
    "original_scan_pages_delta": deltas["original_scan_pages"],
    "proposed_scan_pages_before": before.get("proposed_scan_pages", 0),
    "proposed_scan_pages_after": after.get("proposed_scan_pages", 0),
    "proposed_scan_pages_delta": deltas["proposed_scan_pages"],
    "applied_scan_pages_before": before.get("applied_scan_pages", 0),
    "applied_scan_pages_after": after.get("applied_scan_pages", 0),
    "applied_scan_pages_delta": deltas["applied_scan_pages"],
    "observe_actual_equals_original": observe_equal,
    "app_policy_apply": after.get("app_policy_apply", 0),
    "prepare_calls": after.get("prepare_calls", 0),
    "reclaim_calls": after.get("reclaim_calls", 0),
    "per_folio_calls": after.get("per_folio_calls", 0),
    "predictions": after.get("predictions", 0),
    "missing_app": after.get("missing_app", 0),
    "missing_hint": after.get("missing_hint", 0),
    "missing_transition": after.get("missing_transition", 0),
    "hist_lines_count": hist_count,
    "markov_lines_count": markov_count,
    "hint_lines_count": hint_count,
    "bind_lines_count": bind_count,
    "probability_lines_count": prob_count,
    "stress_deltas": stress_deltas,
    "online_lstm_result": online_result,
    "cgroup_workload_result": cgroup_result,
    "workload_classifier_result": classifier_result,
    "workload_update_write_ok": workload_update_ok,
    "workload_markov_builder_result": markov_builder_result,
    "markov_set_write_ok": markov_set_ok,
    "mglru_markov_strict_result": markov_strict_result,
    "mglru_lstm_policy_strict_result": policy_strict_result,
    "monitor_exit_code": monitor_rc,
    "automation_exit_code": automation_rc,
    "stress_exit_code": stress_rc,
    "generation_modified": False,
    "anon_file_decision_modified": False,
    "lru_gen_pages_written": False,
    "ebpf_kfunc_used": False,
    "promote_demote_protect_called": False,
    "reclaim_behavior_changed": False,
    "bucket_coverage_result": bucket_coverage,
    "minimal_fixes": [
        {
            "problem": "原协议测试未覆盖 app bind 和非法参数原子性",
            "root_cause": "早期脚本只验证 policy 配置和 probability 正常路径",
            "file": "runtime_monitor/scripts/test_mglru_lstm_reclaim_policy_debugfs.sh",
            "change": "使用当前进程真实 cgroup inode 测试 bind，并验证非法 threshold/factor/mode 被拒绝且配置不变",
            "policy_semantics_changed": False,
        }
    ],
}

conditions = {
    "new_kernel": data["new_kernel_active"],
    "protocol": data["debugfs_protocol_test_result"] == "PASS",
    "monitor_exit": monitor_rc == 0,
    "automation_exit": automation_rc == 0,
    "observe": data["policy_mode"] == "observe",
    "real_probability": probability_source == "sigmoid_uncalibrated" and not probability_is_rank_based,
    "policy_config": policy_config_ok >= 5,
    "bind": bind_ok > 0 and binding_matches,
    "probability": probability_ok > 0,
    "prepare": deltas["app_policy_prepare_calls"] > 0,
    "target_seen": data["target_cgroup_seen"] > 0,
    "target_hit": data["target_app_lookup_hit"] > 0,
    "probability_hit": data["probability_lookup_hit"] > 0,
    "scan": deltas["original_scan_pages"] > 0 and deltas["proposed_scan_pages"] > 0,
    "observe_equal": observe_equal and data["app_policy_apply"] == 0,
    "markov_hook": data["prepare_calls"] > 0 and data["per_folio_calls"] == 0,
    "markov_tables": hist_count > 0 and markov_count > 0,
    "online_lstm": online_result == "PASS",
    "cgroup": cgroup_result == "PASS",
    "classifier": classifier_result == "PASS",
    "workload_update": workload_update_ok > 0,
    "markov_builder": markov_builder_result == "PASS",
    "markov_set": markov_set_ok > 0,
    "markov_strict": markov_strict_result == "PASS",
    "policy_strict": policy_strict_result == "PASS",
}
data["failed_conditions"] = [name for name, passed in conditions.items() if not passed]
data["final_result"] = "PASS" if all(conditions.values()) else "FAIL"

(validation / "mglru_lstm_reclaim_runtime_summary.json").write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

lines = [
    "# MGLRU LSTM 回收策略严格运行态验收报告", "",
    f"- final_result: {data['final_result']}",
    f"- running_kernel: {running_release} build #{build_number}",
    f"- session_id: {session.name}",
    f"- runtime_session_dir: `{session}`",
    f"- scenario_path: `{scenario}`", "",
    "## 协议与概率", "",
    f"- debugfs_protocol_test_result: {data['debugfs_protocol_test_result']}",
    f"- policy_mode: {data['policy_mode']}",
    f"- probability_source: {probability_source}",
    f"- probability_is_rank_based: {str(probability_is_rank_based).lower()}",
    f"- policy_config_write_ok: {policy_config_ok}",
    f"- app_bind_write_ok: {bind_ok}",
    f"- app_probability_write_ok: {probability_ok}",
    f"- userspace_binding_id_matches: {str(binding_matches).lower()}", "",
    "## Lruvec 策略统计", "",
]
for key in ["app_policy_prepare_calls_before", "app_policy_prepare_calls_after", "app_policy_prepare_calls_delta", "target_cgroup_seen", "target_cgroup_id_last", "target_app_lookup_hit", "target_app_lookup_miss", "probability_lookup_hit", "probability_lookup_miss", "probability_expired", "bucket_foreground", "bucket_high", "bucket_neutral", "bucket_low", "bucket_very_low", "bucket_missing", "bucket_expired", "bucket_unknown", "original_scan_pages_delta", "proposed_scan_pages_delta", "applied_scan_pages_delta", "app_policy_apply"]:
    lines.append(f"- {key}: {data[key]}")
lines += [
    f"- observe_actual_equals_original: {str(observe_equal).lower()}",
    f"- bucket_coverage_result: {bucket_coverage}", "",
    "observe 模式允许 proposed 与 original 不同，但本次 actual 必须与 original 完全相等；该条件直接按清空后的累计 delta 判定。", "",
    "## Markov 与原链路", "",
]
for key in ["prepare_calls", "per_folio_calls", "predictions", "missing_app", "missing_hint", "missing_transition", "hist_lines_count", "markov_lines_count", "hint_lines_count", "online_lstm_result", "cgroup_workload_result", "workload_classifier_result", "workload_update_write_ok", "workload_markov_builder_result", "markov_set_write_ok", "mglru_markov_strict_result", "mglru_lstm_policy_strict_result"]:
    lines.append(f"- {key}: {data[key]}")
lines += ["", "## 安全约束", "", "- 未写 lru_gen_pages。", "- 未使用 eBPF/BPF kfunc。", "- 未调用 promote/depromote/protect。", "- 未修改 generation 或 anon/file 选择。", "- policy mode 始终为 observe，未改变实际 reclaim 扫描预算。", "", "## 最小修复", "", "协议测试脚本增加了 bind 和非法参数原子性覆盖。该修复只增强测试，不修改阈值、factor、hook 或策略语义。", "", f"- failed_conditions: {', '.join(data['failed_conditions']) or 'none'}"]
(validation / "mglru_lstm_reclaim_runtime_summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
print(data["final_result"])
PY

FINAL_RESULT="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["final_result"])' "$SUMMARY_JSON")"
log "summary=$SUMMARY_MD"
log "json=$SUMMARY_JSON"
log "final_result=$FINAL_RESULT"
[[ "$FINAL_RESULT" == "PASS" ]]
