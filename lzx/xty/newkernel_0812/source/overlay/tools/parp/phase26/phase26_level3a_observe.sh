#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0
set -euo pipefail
source "$(dirname "$0")/_common.sh"
phase26_init "$@"

release=$(phase26_runtime_release)
[[ $(uname -r) = "$release" ]] || { phase26_log "TARGET_KERNEL_NOT_BOOTED"; exit 79; }
duration=${PHASE26_LEVEL3A_DURATION:-180}
[[ $duration =~ ^[0-9]+$ ]] && ((duration >= 180 && duration <= 600))
parp_dir=/sys/kernel/debug/parp
trace_root=/sys/kernel/tracing
damon_admin=/sys/kernel/mm/damon/admin
test -d "$parp_dir" && test -d "$trace_root/events/parp"
test -d "$damon_admin"
"$PHASE26_SCRIPT_DIR/phase26_restore_observe.sh" --output-root "$PHASE26_OUTPUT_ROOT"
phase26_run_root true

mkdir -p "$PHASE26_OUTPUT_ROOT"/{level3a,traces,damon,lstm,cgroup,metrics,validation}
cgroup_root=$(phase26_find_cgroup2_memory_root || true)
cgroup=${cgroup_root:+$cgroup_root/huawei-test.slice/parp-phase26-synthetic.scope}
workload_pid=""
metrics_pid=""
damon_started=0
cgroup_created=0

cleanup()
{
	local rc=$?
	[[ -z $workload_pid ]] || kill "$workload_pid" 2>/dev/null || true
	[[ -z $metrics_pid ]] || kill "$metrics_pid" 2>/dev/null || true
	[[ -z $workload_pid ]] || wait "$workload_pid" 2>/dev/null || true
	[[ -z $metrics_pid ]] || wait "$metrics_pid" 2>/dev/null || true
	if ((damon_started)); then
		sudo -n sh -c "printf 'off\\n' > '$damon_admin/kdamonds/0/state'" || true
	fi
	sudo -n sh -c "printf '0\\n' > '$trace_root/tracing_on'" || true
	sudo -n install -m 0644 "$trace_root/trace" \
		"$PHASE26_OUTPUT_ROOT/traces/level3a_trace.raw" || true
	sudo -n sh -c "printf '0\\n' > '$trace_root/events/parp/enable'" || true
	if [[ -e $trace_root/events/damon/damon_aggregated/enable ]]; then
		sudo -n sh -c "printf '0\\n' > '$trace_root/events/damon/damon_aggregated/enable'" || true
	fi
	if ((cgroup_created)); then
		sudo -n rmdir "$cgroup" 2>/dev/null || true
		sudo -n rmdir "$(dirname "$cgroup")" 2>/dev/null || true
	fi
	sudo -n sh -c "printf '1\\n' > '$parp_dir/scan_budget_mode'; printf '0\\n' > '$parp_dir/scan_budget_apply_domain'; printf '1\\n' > '$parp_dir/mode'; printf '0\\n' > '$parp_dir/evidence_mode'" || true
	phase26_state_set observe_restored true || true
	return "$rc"
}
trap cleanup EXIT INT TERM

state_file="$damon_admin/kdamonds/0/state"
if [[ -e $state_file && $(phase26_read_root "$state_file") != off ]]; then
	phase26_log "DAMON_CONTEXT_BUSY"
	exit 82
fi
phase26_run_root sh -c \
	"printf '1\\n' > '$damon_admin/kdamonds/nr_kdamonds'; \
	 printf '1\\n' > '$damon_admin/kdamonds/0/contexts/nr_contexts'; \
	 printf 'vaddr\\n' > '$damon_admin/kdamonds/0/contexts/0/operations'; \
	 printf '1\\n' > '$damon_admin/kdamonds/0/contexts/0/targets/nr_targets'"

mem_total=$(awk '/^MemTotal:/ {print $2 * 1024}' /proc/meminfo)
memory_max=$((mem_total * 15 / 100))
((memory_max > 1073741824)) && memory_max=1073741824
workload_bytes=$((mem_total / 10))
((workload_bytes > 536870912)) && workload_bytes=536870912
limit70=$((memory_max * 70 / 100))
((workload_bytes > limit70)) && workload_bytes=$limit70
workload_mib=$((workload_bytes / 1048576))

phase26_log "RUN phase26_memory_workload.py total_mib=$workload_mib duration=$((duration + 30))"
python3 "$PHASE26_WORK_TREE/tools/parp/phase26_memory_workload.py" \
	--total-mib "$workload_mib" --duration "$((duration + 30))" \
	>"$PHASE26_OUTPUT_ROOT/level3a/workload.jsonl" 2>&1 &
workload_pid=$!
PHASE26_METRICS_DURATION=$duration "$PHASE26_SCRIPT_DIR/phase26_collect_metrics.sh" \
	--output-root "$PHASE26_OUTPUT_ROOT" &
metrics_pid=$!

phase26_run_root sh -c \
	"printf '%s\\n' '$workload_pid' > '$damon_admin/kdamonds/0/contexts/0/targets/0/pid_target'; \
	 : > '$trace_root/trace'; printf '16384\\n' > '$trace_root/buffer_size_kb'; \
	 printf '1\\n' > '$trace_root/events/parp/enable'; \
	 if test -e '$trace_root/events/damon/damon_aggregated/enable'; then printf '1\\n' > '$trace_root/events/damon/damon_aggregated/enable'; fi; \
	 printf '1\\n' > '$trace_root/tracing_on'; \
	 printf 'on\\n' > '$damon_admin/kdamonds/0/state'"
damon_started=1

cgroup2=0
if [[ -n $cgroup_root ]]; then
	cgroup2=1
	phase26_run_root sh -c \
		"grep -qw memory '$cgroup_root/cgroup.subtree_control' || printf '+memory\\n' > '$cgroup_root/cgroup.subtree_control'; \
		 mkdir -p '$(dirname "$cgroup")'; \
		 grep -qw memory '$(dirname "$cgroup")/cgroup.subtree_control' || printf '+memory\\n' > '$(dirname "$cgroup")/cgroup.subtree_control'"
	phase26_run_root mkdir -p "$cgroup"
	cgroup_created=1
	phase26_run_root sh -c \
		"printf '%s\\n' '$memory_max' > '$cgroup/memory.max'; printf '%s\\n' '$workload_pid' > '$cgroup/cgroup.procs'"
	domain_id=$(stat -c %i "$cgroup")
	phase26_run_root env PYTHONPATH="$PHASE26_WORK_TREE/tools/parp" python3 \
		"$PHASE26_WORK_TREE/tools/parp/phase26_prior_submit.py" --count 5
	phase26_state_set level3a_observe_status '"RUNNING_OBSERVE_COLLECTION"'
	phase26_log "domain_id=$domain_id workload_pid=$workload_pid memory_max=$memory_max"
else
	phase26_state_set level3a_observe_status '"LEVEL3A_TARGET_MEMCG_RECLAIM_GATED_CGROUP_MODE"'
	phase26_log "LEVEL3A_TARGET_MEMCG_RECLAIM_GATED_CGROUP_MODE; continuing raw DAMON collection"
fi

start=$SECONDS
decision=0
apps=(9001 9002 9003 9004)
while ((SECONDS - start < duration)); do
	if ((cgroup2)); then
		app=${apps[decision % ${#apps[@]}]}
		phase26_run_root sh -c \
			"printf '%s %s 600000 %s 9\\n' '$domain_id' '$app' '$((decision + 1))' > '$parp_dir/app_bind'"
		if ((decision > 0 && decision % 5 == 0)); then
			phase26_run_root env PYTHONPATH="$PHASE26_WORK_TREE/tools/parp" python3 \
				"$PHASE26_WORK_TREE/tools/parp/phase26_prior_submit.py" --count 1
		fi
		phase26_run_root sh -c "printf '4M\\n' > '$cgroup/memory.reclaim'"
		printf '{"source":"RUNTIME_LEVEL3A","decision":%d,"app_id":%d,"domain_id":%d,"wall_time":"%s"}\n' \
			"$decision" "$app" "$domain_id" "$(date --iso-8601=seconds)" \
			>>"$PHASE26_OUTPUT_ROOT/cgroup/memory_reclaim_log.jsonl"
		decision=$((decision + 1))
	fi
	sleep 5
done

kill "$workload_pid" 2>/dev/null || true
wait "$workload_pid" 2>/dev/null || true
workload_pid=""
wait "$metrics_pid" 2>/dev/null || true
metrics_pid=""
phase26_run_root sh -c "printf 'off\\n' > '$damon_admin/kdamonds/0/state'; printf '0\\n' > '$trace_root/tracing_on'"
damon_started=0
phase26_run_root install -m 0644 "$trace_root/trace" \
	"$PHASE26_OUTPUT_ROOT/traces/level3a_trace.raw"
python3 "$PHASE26_WORK_TREE/tools/parp/damon_collect.py" \
	--decode-trace "$PHASE26_OUTPUT_ROOT/traces/level3a_trace.raw" \
	--output "$PHASE26_OUTPUT_ROOT/damon/raw_regions.jsonl" \
	>"$PHASE26_OUTPUT_ROOT/damon/decode_summary.json"
grep 'parp_scan_budget_decision:' "$PHASE26_OUTPUT_ROOT/traces/level3a_trace.raw" \
	>"$PHASE26_OUTPUT_ROOT/traces/parp_scan_budget.raw" || true
grep 'parp_region_evidence:' "$PHASE26_OUTPUT_ROOT/traces/level3a_trace.raw" \
	>"$PHASE26_OUTPUT_ROOT/traces/parp_damon.raw" || true
grep 'damon_aggregated:' "$PHASE26_OUTPUT_ROOT/traces/level3a_trace.raw" \
	>"$PHASE26_OUTPUT_ROOT/traces/reclaim.raw" || true
phase26_require_observe "$parp_dir"
if ((cgroup2)); then
	phase26_state_set level3a_observe_status '"COLLECTED_PENDING_ANALYSIS"'
fi
trap - EXIT INT TERM
cleanup
phase26_finish
