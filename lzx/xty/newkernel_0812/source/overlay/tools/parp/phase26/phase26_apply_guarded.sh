#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0
set -euo pipefail
source "$(dirname "$0")/_common.sh"
phase26_init "$@"

parp_dir=/sys/kernel/debug/parp
trace_root=/sys/kernel/tracing
cgroup=${PHASE26_SYNTHETIC_CGROUP:-/sys/fs/cgroup/huawei-test.slice/parp-phase26-synthetic.scope}
[[ $cgroup = */parp-phase26-synthetic.scope ]]
test -e "$cgroup/memory.reclaim"
test "$(phase26_read_root "$parp_dir/scan_budget_mode")" = 1
test "$(phase26_read_root "$parp_dir/scan_budget_apply_domain")" = 0
test "$(phase26_read_root "$parp_dir/mode")" = 1
test "$(phase26_read_root "$parp_dir/evidence_mode")" = 0
python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); assert d["status"] in ("PASS","APPLY_ALLOWED_FROM_LEVEL3A_ONLY")' \
	"$PHASE26_OUTPUT_ROOT/validation/observe_gate.json"
memory_max=$(<"$cgroup/memory.max")
[[ $memory_max != max ]]
domain_id=$(stat -c %i "$cgroup")
mkdir -p "$PHASE26_OUTPUT_ROOT"/{apply,traces,metrics,validation}
apply_trace="$PHASE26_OUTPUT_ROOT/traces/controlled_apply.raw"
watchdog=""
apply_armed=0

restore()
{
	local rc=$?
	sudo -n sh -c "printf '1\\n' > '$parp_dir/scan_budget_mode'" || true
	sudo -n sh -c "printf '0\\n' > '$parp_dir/scan_budget_apply_domain'" || true
	sudo -n sh -c "printf '0\\n' > '$trace_root/tracing_on'" || true
	if [[ -n $watchdog ]]; then
		kill "$watchdog" 2>/dev/null || true
		wait "$watchdog" 2>/dev/null || true
	fi
	sudo -n install -m 0644 "$trace_root/trace" "$apply_trace" 2>/dev/null || true
	phase26_state_set observe_restored true || true
	if ((rc != 0 && apply_armed)); then
		phase26_state_set apply_status '"CONTROLLED_APPLY_ABORTED_SAFE_RESTORE"' || true
	fi
	return "$rc"
}
trap restore EXIT INT TERM

phase26_run_root true
oom_before=$(awk '/^oom_kill / {print $2}' /proc/vmstat)
swap_before=$(awk '/^SwapTotal:/ {t=$2} /^SwapFree:/ {f=$2} END {print (t-f)*1024}' /proc/meminfo)
memcg_oom_before=$(awk '/^oom / {print $2}' "$cgroup/memory.events")
phase26_run_root sh -c "dmesg > '$PHASE26_OUTPUT_ROOT/apply/dmesg_before.txt'"
phase26_run_root sh -c \
	": > '$trace_root/trace'; printf '1\\n' > '$trace_root/events/parp/parp_scan_budget_decision/enable'; printf '1\\n' > '$trace_root/tracing_on'"

# App 9003 is the non-foreground medium prior (the conservative 0.80 case).
phase26_run_root env PYTHONPATH="$PHASE26_WORK_TREE/tools/parp" python3 \
	"$PHASE26_WORK_TREE/tools/parp/phase26_prior_submit.py" --count 1
phase26_run_root sh -c \
	"printf '%s 9003 120000 1 9\\n' '$domain_id' > '$parp_dir/app_bind'"

sudo -n sh -c "sleep 60; printf '1\\n' > '$parp_dir/scan_budget_mode'; printf '0\\n' > '$parp_dir/scan_budget_apply_domain'" &
watchdog=$!
phase26_run_root sh -c "printf '%s\\n' '$domain_id' > '$parp_dir/scan_budget_apply_domain'"
phase26_run_root sh -c "printf '2\\n' > '$parp_dir/scan_budget_mode'"
test "$(phase26_read_root "$parp_dir/scan_budget_mode")" = 2
test "$(phase26_read_root "$parp_dir/scan_budget_apply_domain")" = "$domain_id"
apply_armed=1
phase26_state_set observe_restored false
phase26_state_set apply_status '"RUNNING_SYNTHETIC_ONLY"'

for iteration in $(seq 1 20); do
	kill -0 "$watchdog" 2>/dev/null || { phase26_log "ABORT watchdog exited"; exit 90; }
	available=$(awk '/^MemAvailable:/ {print $2 * 1024}' /proc/meminfo)
	total=$(awk '/^MemTotal:/ {print $2 * 1024}' /proc/meminfo)
	((available * 100 >= total * 15)) || { phase26_log "ABORT MemAvailable below 15%"; exit 90; }
	current=$(<"$cgroup/memory.current")
	((current * 100 < memory_max * 95)) || { phase26_log "ABORT memory.current reached 95%"; exit 90; }
	oom_now=$(awk '/^oom_kill / {print $2}' /proc/vmstat)
	memcg_oom_now=$(awk '/^oom / {print $2}' "$cgroup/memory.events")
	((oom_now == oom_before && memcg_oom_now == memcg_oom_before)) || { phase26_log "ABORT OOM delta"; exit 90; }
	swap_now=$(awk '/^SwapTotal:/ {t=$2} /^SwapFree:/ {f=$2} END {print (t-f)*1024}' /proc/meminfo)
	((swap_now - swap_before <= 268435456)) || { phase26_log "ABORT swap growth above 256MiB"; exit 90; }
	psi_full=$(awk '/^full/ {for (i=1;i<=NF;i++) if ($i ~ /^avg10=/) {split($i,a,"="); print a[2]}}' /proc/pressure/memory)
	awk -v value="$psi_full" 'BEGIN {exit !(value <= 5.0)}' || { phase26_log "ABORT memory PSI full avg10 above 5%"; exit 90; }
	phase26_run_root sh -c "printf '4M\\n' > '$cgroup/memory.reclaim'"
	sleep 2
	phase26_run_root install -m 0644 "$trace_root/trace" "$apply_trace"
	if awk -v wanted="$domain_id" '
		/parp_scan_budget_decision:/ {
			delete f; for (i=1;i<=NF;i++) {split($i,p,"="); f[p[1]]=p[2]}
			if (f["mode"] == 2 && f["domain"] != wanted && f["applied_units"] != f["native_units"]) bad=1
			if (f["mode"] == 2 && f["scope"] != 3 && f["applied_units"] != f["native_units"]) bad=1
		} END {exit !bad}' "$apply_trace"; then
		phase26_log "ABORT Apply escaped synthetic target"
		exit 90
	fi
	decisions=$(awk -v wanted="$domain_id" '
		/parp_scan_budget_decision:/ {delete f; for(i=1;i<=NF;i++){split($i,p,"=");f[p[1]]=p[2]} if(f["mode"]==2 && f["domain"]==wanted) n++} END{print n+0}' "$apply_trace")
	((decisions < 20)) || break
done

phase26_run_root sh -c "printf '1\\n' > '$parp_dir/scan_budget_mode'"
phase26_run_root sh -c "printf '0\\n' > '$parp_dir/scan_budget_apply_domain'"
apply_armed=0
kill "$watchdog" 2>/dev/null || true
wait "$watchdog" 2>/dev/null || true
watchdog=""
phase26_require_observe "$parp_dir"
test "$(phase26_read_root "$parp_dir/scan_budget_apply_domain")" = 0
phase26_run_root sh -c "dmesg > '$PHASE26_OUTPUT_ROOT/apply/dmesg_after.txt'"
phase26_run_root install -m 0644 "$trace_root/trace" "$apply_trace"
awk -v wanted="$domain_id" '
	/parp_scan_budget_decision:/ {delete f; for(i=1;i<=NF;i++){split($i,p,"=");f[p[1]]=p[2]}
	 if(f["mode"]==2 && f["domain"]==wanted && f["applied_units"]==f["proposed_units"] && f["proposed_units"]<f["native_units"]) ok=1}
	END {exit !ok}' "$apply_trace" || { phase26_log "APPLY_EFFECT_NOT_OBSERVED"; exit 91; }
phase26_state_set observe_restored true
phase26_state_set apply_status '"COMPLETED_PENDING_ANALYSIS"'
trap - EXIT INT TERM
restore
phase26_finish
