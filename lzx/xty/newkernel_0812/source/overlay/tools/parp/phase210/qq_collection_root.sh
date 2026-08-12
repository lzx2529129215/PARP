#!/usr/bin/env bash
set -euo pipefail

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
tree=$(cd -- "$script_dir/../../.." && pwd)
project=
cursor=$tree
while [[ $cursor != / ]]; do
	if [[ -d $cursor/MGLRU-test/v4-parp/work && -d $cursor/automation && -d $cursor/outputs ]]; then
		project=$cursor
		break
	fi
	cursor=$(dirname -- "$cursor")
done
[[ -n $project ]] || { printf 'ERROR: PROJECT_ROOT not found\n' >&2; exit 70; }

output=${1:-}
mode=${2:-formal}
[[ -n $output && $output == "$project"/outputs/parp_phase210_app_specific_ab_* ]] || {
	printf 'Usage: sudo bash %s PROJECT_OUTPUT_ROOT [formal|positive-support-pilot]\n' "$0" >&2
	exit 64
}
[[ $mode == formal || $mode == positive-support-pilot ]] || {
	printf 'ERROR: unsupported mode=%s\n' "$mode" >&2
	exit 64
}
[[ -d $output && -f $output/final/FINAL_REPORT.json ]] || { printf 'ERROR: invalid Phase2.10 output\n' >&2; exit 65; }
[[ $EUID -eq 0 ]] || { printf 'ERROR: run with sudo bash\n' >&2; exit 77; }

run_user=${SUDO_USER:-}
[[ -n $run_user && $run_user != root ]] || { printf 'ERROR: SUDO_USER must be desktop user\n' >&2; exit 78; }
run_uid=$(id -u "$run_user")
run_gid=$(id -g "$run_user")
run_home=$(getent passwd "$run_user" | cut -d: -f6)
runtime_dir=/run/user/$run_uid
display=:0
xauthority=$runtime_dir/gdm/Xauthority
trace_root=/sys/kernel/tracing
debug_root=/sys/kernel/debug
damon=/sys/kernel/mm/damon/admin
cgroup_root=$(findmnt -n -t cgroup2 -o TARGET | head -1)
automation=$project/automation/run_automation.sh
login_gate=$tree/tools/parp/phase210/qq_login_gate.py
positive_auditor=$tree/tools/parp/phase210/positive_support_audit.py
base_profile=$output/fixtures/qq_authenticated_profile
credential_file=$project/../../password_qq.txt
if [[ $mode == positive-support-pilot ]]; then
	run_id=$(date +%Y%m%d_%H%M%S)
	builder=$tree/tools/parp/phase210/build_qq_positive_support.py
	collection=$output/qq_positive_support_pilot/$run_id
	sessions=(qq_positive_support_pilot)
else
	builder=$tree/tools/parp/phase210/build_qq_collection.py
	collection=$output/qq_collection
	sessions=(qq_train_01 qq_validation_01)
fi
log_file=$collection/root_collection.log
active_trace=
trace_reader=
sampler_pid=
automation_pid=
automation_pgid=
active_scope=
damon_created=0
active_domain_id=
active_app_id=
active_target_cgroup=
active_control_cgroup=
active_scope_cgroup=
qq_app_id=
declare -a memory_changed=()

mkdir -p "$collection"/{config,raw,validation,cleanup,state}
touch "$log_file"
chown -R "$run_uid:$run_gid" "$collection"

log()
{
	printf '%s %s\n' "$(date --iso-8601=seconds)" "$*" | tee -a "$log_file"
}

mono_ns()
{
	python3 - <<'PY'
import time
print(time.clock_gettime_ns(time.CLOCK_MONOTONIC))
PY
}

run_as_user()
{
	runuser -u "$run_user" -- env \
		HOME="$run_home" USER="$run_user" LOGNAME="$run_user" \
		DISPLAY="$display" XAUTHORITY="$xauthority" \
		XDG_RUNTIME_DIR="$runtime_dir" DBUS_SESSION_BUS_ADDRESS="unix:path=$runtime_dir/bus" \
		XDG_SESSION_TYPE=x11 GDK_BACKEND=x11 "$@"
}

stop_damon()
{
	if ((damon_created)) && [[ -e $damon/kdamonds/0/state ]]; then
		printf 'off\n' >"$damon/kdamonds/0/state" 2>/dev/null || true
		for _ in $(seq 1 50); do
			[[ $(tr -d '[:space:]' <"$damon/kdamonds/0/state" 2>/dev/null || true) == off ]] && break
			sleep 0.1
		done
		printf '0\n' >"$damon/kdamonds/nr_kdamonds" 2>/dev/null || true
		damon_created=0
	fi
}

stop_active_scope()
{
	# These root-owned children must disappear before systemd can finish the
	# transient scope stop job.  Kill only the isolated test scope if a member
	# survived the automation close action, then remove leaves bottom-up.
	if [[ -n $active_target_cgroup && -d $active_target_cgroup &&
	      -n $(cat "$active_target_cgroup/cgroup.procs" 2>/dev/null) && -n $active_scope ]]; then
		run_as_user systemctl --user kill --kill-whom=all --signal=SIGKILL "$active_scope" >/dev/null 2>&1 || true
	fi
	for _ in $(seq 1 50); do
		[[ -n $active_target_cgroup && -d $active_target_cgroup ]] && rmdir "$active_target_cgroup" 2>/dev/null || true
		[[ -n $active_control_cgroup && -d $active_control_cgroup ]] && rmdir "$active_control_cgroup" 2>/dev/null || true
		[[ -z $active_target_cgroup || ! -d $active_target_cgroup ]] &&
			[[ -z $active_control_cgroup || ! -d $active_control_cgroup ]] && break
		sleep 0.1
	done
	if [[ -n $active_scope_cgroup && -d $active_scope_cgroup ]]; then
		printf '%s\n' -memory >"$active_scope_cgroup/cgroup.subtree_control" 2>/dev/null || true
	fi
	active_target_cgroup=
	active_control_cgroup=
	active_scope_cgroup=
	if [[ -n $active_scope ]]; then
		run_as_user systemctl --user stop --no-block "$active_scope" >/dev/null 2>&1
		for _ in $(seq 1 100); do
			state=$(run_as_user systemctl --user show "$active_scope" -p ActiveState --value 2>/dev/null || true)
			[[ $state != active && $state != deactivating ]] && break
			sleep 0.1
		done
		state=$(run_as_user systemctl --user show "$active_scope" -p ActiveState --value 2>/dev/null || true)
		if [[ $state == active || $state == deactivating ]]; then
			run_as_user systemctl --user kill --kill-whom=all --signal=SIGKILL "$active_scope" >/dev/null 2>&1
			run_as_user systemctl --user stop --no-block "$active_scope" >/dev/null 2>&1
		fi
		run_as_user systemctl --user reset-failed "$active_scope" >/dev/null 2>&1 || true
	fi
	active_scope=
}

cleanup_runtime()
{
	set +e
	stop_damon
	[[ -n $sampler_pid ]] && kill "$sampler_pid" 2>/dev/null
	[[ -n $trace_reader ]] && kill "$trace_reader" 2>/dev/null
	if [[ -n $automation_pgid ]]; then
		kill -TERM -- "-$automation_pgid" 2>/dev/null
		for _ in $(seq 1 20); do
			kill -0 -- "-$automation_pgid" 2>/dev/null || break
			sleep 0.1
		done
		kill -KILL -- "-$automation_pgid" 2>/dev/null
	elif [[ -n $automation_pid ]]; then
		kill "$automation_pid" 2>/dev/null
	fi
	stop_active_scope
	if [[ -n $active_trace && -d $active_trace ]]; then
		printf '0\n' >"$active_trace/events/parp/parp_region_evidence/enable" 2>/dev/null
		[[ -e $active_trace/events/damon/damon_aggregated/enable ]] && printf '0\n' >"$active_trace/events/damon/damon_aggregated/enable" 2>/dev/null
		rmdir "$active_trace" 2>/dev/null
	fi
	if [[ -n $active_domain_id && -n $active_app_id && -n ${parp:-} && -w $parp/app_bind ]]; then
		printf '%s %s 1 999 27\n' "$active_domain_id" "$active_app_id" >"$parp/app_bind" 2>/dev/null || true
	fi
	for ((index=${#memory_changed[@]}-1; index>=0; index--)); do
		path=${memory_changed[$index]}
		printf '%s\n' -memory >"$path/cgroup.subtree_control" 2>/dev/null || true
	done
}
trap cleanup_runtime EXIT INT TERM

log "PREFLIGHT euid=$EUID run_user=$run_user output=$output mode=$mode"
[[ -x /opt/QQ/qq && -x $automation && -f $builder && -f $login_gate && -f $positive_auditor ]] || { log 'ERROR: required executable missing'; exit 80; }
[[ -d $base_profile/config && -d $base_profile/chromium ]] || { log 'ERROR: isolated QQ account profile missing'; exit 81; }
run_as_user test -r "$credential_file" || { log 'ERROR: protected QQ credential file unavailable'; exit 81; }
[[ -S /tmp/.X11-unix/X0 && -r $xauthority ]] || { log 'ERROR: desktop X11 bridge unavailable'; exit 82; }
mountpoint -q "$trace_root" || mount -t tracefs tracefs "$trace_root"
mountpoint -q "$debug_root" || mount -t debugfs debugfs "$debug_root"
parp=
for candidate in "$debug_root/sched/parp" "$debug_root/parp"; do
	[[ -d $candidate ]] && parp=$candidate && break
done
[[ -n $parp && -r $parp/scan_budget_stats && -r $parp/evidence_stats ]] || { log 'ERROR: PARP debugfs unavailable'; exit 83; }
[[ -d $damon && $(tr -d '[:space:]' <"$damon/kdamonds/nr_kdamonds") == 0 ]] || { log 'ERROR: DAMON unavailable or busy'; exit 84; }
[[ -d $cgroup_root && -r $cgroup_root/cgroup.controllers ]] || { log 'ERROR: cgroup2 unavailable'; exit 85; }
log "PREFLIGHT kernel=$(uname -r) tracefs=$trace_root parp=$parp damon_idle=true cgroup2=$cgroup_root"

mode_before=$(tr -d '[:space:]' <"$parp/mode")
evidence_before=$(tr -d '[:space:]' <"$parp/evidence_mode")
scan_before=$(tr -d '[:space:]' <"$parp/scan_budget_mode")
apply_domain_before=$(tr -d '[:space:]' <"$parp/scan_budget_apply_domain")
[[ $mode_before == 1 && $evidence_before == 0 && $scan_before == 1 && $apply_domain_before == 0 ]] || {
	log "ERROR: unsafe PARP mode mode=$mode_before evidence=$evidence_before scan=$scan_before apply_domain=$apply_domain_before"
	exit 78
}
qq_app_id=$(python3 - "$project/runtime_monitor/config/runtime_app_scope.json" <<'PY'
import json, sys
apps=json.load(open(sys.argv[1]))['apps']
print(next(row['app_id'] for row in apps if row['app_key']=='QQ'))
PY
)
[[ $qq_app_id =~ ^[0-9]+$ ]] || { log 'ERROR: QQ app_id unresolved'; exit 79; }
log "PREFLIGHT observe_modes=true mode=$mode_before evidence=$evidence_before scan=$scan_before apply_domain=$apply_domain_before qq_app_id=$qq_app_id"

enable_memory_path()
{
	local path=$1
	local is_root=$2
	grep -qw memory "$path/cgroup.subtree_control" && return 0
	grep -qw memory "$path/cgroup.controllers" || { log "ERROR: memory controller unavailable path=$path"; return 1; }
	if [[ $is_root != true && -n $(cat "$path/cgroup.procs" 2>/dev/null) ]]; then
		log "ERROR: internal cgroup has direct processes path=$path"
		return 1
	fi
	printf '%s\n' +memory >"$path/cgroup.subtree_control"
	grep -qw memory "$path/cgroup.subtree_control" || return 1
	memory_changed+=("$path")
	printf 'enabled_memory %s\n' "$path" >>"$collection/state/memory_controller_changes.txt"
}

memory_chain=(
	"$cgroup_root"
	"$cgroup_root/user.slice"
	"$cgroup_root/user.slice/user-$run_uid.slice"
	"$cgroup_root/user.slice/user-$run_uid.slice/user@$run_uid.service"
	"$cgroup_root/user.slice/user-$run_uid.slice/user@$run_uid.service/huawei.slice"
	"$cgroup_root/user.slice/user-$run_uid.slice/user@$run_uid.service/huawei.slice/huawei-test.slice"
)
: >"$collection/state/memory_controller_changes.txt"
for index in "${!memory_chain[@]}"; do
	path=${memory_chain[$index]}
	[[ -d $path ]] || { log "ERROR: cgroup chain path missing path=$path"; exit 85; }
	if ((index == 0)); then root_flag=true; else root_flag=false; fi
	enable_memory_path "$path" "$root_flag"
done
log "PREFLIGHT memory_controller_enabled=true changed_paths=${#memory_changed[@]} restore_on_exit=true"

run_as_user python3 "$builder" --output "$collection/config" >>"$log_file"
for session in "${sessions[@]}"; do
	scenario=$collection/config/$session.json
	run_as_user python3 "$project/automation/app_automation.py" "$scenario" \
		--session-id preflight --scenario-id phase210_preflight --display "$display" \
		--xauthority "$xauthority" --trace-output "$collection/validation/dry_run.csv" \
		--var QQ_PROFILE="$base_profile" --var COLLECTOR_READY=/nonexistent \
		--var AUTOMATION_DONE=/nonexistent --var COLLECTOR_DONE=/nonexistent --dry-run \
		>>"$log_file"
done
log "PREFLIGHT scenario_dry_run=true privacy=authorized_account no_send=true credentials_logged=false mode=$mode"

prepare_profile()
{
	local profile=$1
	if [[ ! -d $profile ]]; then
		cp -a "$base_profile" "$profile"
		chown -R "$run_uid:$run_gid" "$profile"
		chmod -R go-rwx "$profile"
	fi
}

list_phase210_qq_pids()
{
	local profile=$1
	local expected_pgid=$2
	local proc pid uid cmdline exe stat_rest state ppid pgrp
	for proc in /proc/[0-9]*; do
		pid=${proc#/proc/}
		uid=$(stat -c %u "$proc" 2>/dev/null || true)
		[[ $uid == "$run_uid" ]] || continue
		cmdline=$(tr '\0' ' ' <"$proc/cmdline" 2>/dev/null || true)
		exe=$(readlink -f "$proc/exe" 2>/dev/null || true)
		stat_rest=$(sed 's/^[0-9][0-9]* (.*) //' "$proc/stat" 2>/dev/null || true)
		read -r state ppid pgrp _ <<<"$stat_rest"
		if [[ ($exe == /opt/QQ/qq && ($cmdline == *"$profile"* || $pgrp == "$expected_pgid")) ||
		      ($exe == /opt/QQ/chrome_crashpad_handler && $cmdline == *"$profile"*) ]]; then
			printf '%s\n' "$pid"
		fi
	done | sort -nu
}

phase210_pid_is_safe()
{
	local pid=$1
	local profile=$2
	local expected_pgid=$3
	local cmdline exe stat_rest state ppid pgrp
	[[ $(stat -c %u "/proc/$pid" 2>/dev/null || true) == "$run_uid" ]] || return 1
	cmdline=$(tr '\0' ' ' <"/proc/$pid/cmdline" 2>/dev/null || true)
	exe=$(readlink -f "/proc/$pid/exe" 2>/dev/null || true)
	stat_rest=$(sed 's/^[0-9][0-9]* (.*) //' "/proc/$pid/stat" 2>/dev/null || true)
	read -r state ppid pgrp _ <<<"$stat_rest"
	[[ ($exe == /opt/QQ/qq && ($cmdline == *"$profile"* || $pgrp == "$expected_pgid")) ||
	   ($exe == /opt/QQ/chrome_crashpad_handler && $cmdline == *"$profile"*) ]]
}

collect_session()
{
	local session=$1
	local scenario=$2
	local profile=$3
	local session_dir=$collection/raw/$session
	local gates=$session_dir/gates
	local ready=$gates/collector_ready
	local automation_done=$gates/automation_done
	local collector_done=$gates/collector_done
	local trace_name=parp_phase210_${session}_$$
	local raw_trace=$session_dir/parp_region_evidence.raw
	local automation_trace=$session_dir/automation_trace.csv
	local scope_name=phase210-$session
	local unit=automation-${scope_name}.scope
	local scope_control_group scope_cgroup_path control_cgroup_path control_group cgroup_path domain_id apply_before apply_after file_regions anon_regions lost
	local qq_count profile_match cmdline context_ready prior_echo initial_generation
	local prior_generation prior_timestamp_ns prior_refresh_seconds
	local collection_start_ns collection_end_ns pid current_cgroup stable escaped
	local early_stop=0 support_positive=0 support_pairwise=0 pilot_snapshot pilot_audit
	local pilot_gate_start_seconds=0 pilot_gate_checked=0
	local -a pids discovered_pids member_pids direct_pids

	[[ ! -f $session_dir/state/session.json ]] || { log "SESSION_SKIP completed=$session"; return; }
	if [[ -d $session_dir ]]; then
		failed=$collection/failed_attempts/${session}_$(date +%Y%m%d_%H%M%S)
		mkdir -p "$failed"
		mv "$session_dir" "$failed/session"
		[[ -d $profile ]] && mv "$profile" "$failed/profile"
		log "SESSION_ARCHIVE_INCOMPLETE session=$session path=$failed"
	fi
	prepare_profile "$profile"
	mkdir -p "$session_dir"/{gates,state,runtime,trace}
	chown -R "$run_uid:$run_gid" "$session_dir"
	log "SESSION_START session=$session unit=$unit"

	setsid runuser -u "$run_user" -- env \
		HOME="$run_home" USER="$run_user" LOGNAME="$run_user" \
		DISPLAY="$display" XAUTHORITY="$xauthority" XDG_RUNTIME_DIR="$runtime_dir" \
		DBUS_SESSION_BUS_ADDRESS="unix:path=$runtime_dir/bus" XDG_SESSION_TYPE=x11 GDK_BACKEND=x11 \
		bash "$automation" --scenario "$scenario" --display "$display" --xauthority "$xauthority" \
		--trace-output "$automation_trace" --session-id "$session" --scenario-id "phase210_$session" \
		--test-slice huawei-test.slice --var "QQ_PROFILE=$profile" --var "SCOPE_NAME=$scope_name" \
		--var "COLLECTOR_READY=$ready" --var "AUTOMATION_DONE=$automation_done" --var "COLLECTOR_DONE=$collector_done" \
		>"$session_dir/automation.log" 2>&1 &
	automation_pid=$!
	automation_pgid=$automation_pid
	active_scope=$unit

	scope_control_group=
	for _ in $(seq 1 90); do
		scope_control_group=$(run_as_user systemctl --user show "$unit" -p ControlGroup --value 2>/dev/null || true)
		[[ -n $scope_control_group && -d $cgroup_root$scope_control_group ]] && break
		kill -0 "$automation_pid"
		sleep 1
	done
	scope_cgroup_path=$cgroup_root$scope_control_group
	[[ -d $scope_cgroup_path && -r $scope_cgroup_path/memory.current ]] || { log "ERROR: dedicated memory cgroup missing session=$session"; exit 86; }
	active_scope_cgroup=$scope_cgroup_path
	control_cgroup_path=$scope_cgroup_path/parp-control
	control_group=$scope_control_group/parp-qq-targets
	cgroup_path=$cgroup_root$control_group
	mkdir "$control_cgroup_path" "$cgroup_path"
	active_control_cgroup=$control_cgroup_path
	active_target_cgroup=$cgroup_path
	mapfile -t direct_pids < <(cat "$scope_cgroup_path/cgroup.procs" | sort -nu)
	for pid in "${direct_pids[@]}"; do
		printf '%s\n' "$pid" >"$control_cgroup_path/cgroup.procs"
	done
	printf '%s\n' +memory >"$scope_cgroup_path/cgroup.subtree_control"
	[[ -r $cgroup_path/memory.current ]] || { log "ERROR: QQ target leaf lacks memory controller session=$session"; exit 96; }

	# The selected account is intentionally logged in afresh for every session.
	# Credentials stay inside the unprivileged helper and reach xdotool only on
	# stdin.  The helper records dimensions/status but never values or UI text.
	if ! run_as_user python3 "$login_gate" --profile "$profile" \
		--credential-file "$credential_file" \
		--evidence "$session_dir/state/authentication_gate.json" >>"$log_file"; then
		log "ERROR: authenticated QQ main UI gate failed session=$session"
		exit 97
	fi
	log "AUTHENTICATION_VALIDATED session=$session credentials_logged=false message_sent=false"

	# Electron may ask the desktop session to move QQ into an app-*.scope after
	# launch.  Reattach only processes belonging to this isolated profile (or
	# QQ executables in this automation process group) to a root-owned leaf and
	# require ten consecutive stable checks before enabling DAMON.
	stable=0
	for _ in $(seq 1 90); do
		mapfile -t discovered_pids < <(list_phase210_qq_pids "$profile" "$automation_pgid")
		if ((${#discovered_pids[@]} == 0)); then
			kill -0 "$automation_pid"
			sleep 1
			continue
		fi
		escaped=0
		for pid in "${discovered_pids[@]}"; do
			[[ -r /proc/$pid/cgroup ]] || continue
			current_cgroup=$(sed -n 's/^0:://p' "/proc/$pid/cgroup" 2>/dev/null || true)
			[[ -n $current_cgroup ]] || continue
			if [[ $current_cgroup != "$control_group" ]]; then
				if ! printf '%s\n' "$pid" >"$cgroup_path/cgroup.procs" 2>/dev/null; then
					[[ -d /proc/$pid ]] || continue
					log "ERROR: failed to attach live QQ target pid=$pid"
					exit 95
				fi
				escaped=1
			fi
		done
		if ((escaped)); then stable=0; else stable=$((stable + 1)); fi
		((stable >= 10)) && break
		kill -0 "$automation_pid"
		sleep 1
	done
	((stable >= 10)) || { log "ERROR: QQ target cgroup did not stabilize session=$session"; exit 95; }
	mapfile -t pids < <(cat "$cgroup_path/cgroup.procs" | sort -nu)
	((${#pids[@]} > 0)) || { log "ERROR: no QQ target pids session=$session"; exit 87; }
	qq_count=0
	profile_match=0
	for pid in "${pids[@]}"; do
		phase210_pid_is_safe "$pid" "$profile" "$automation_pgid" || { log "ERROR: non-test process in QQ target cgroup pid=$pid"; exit 95; }
		[[ $(readlink -f "/proc/$pid/exe" 2>/dev/null || true) == /opt/QQ/qq ]] || continue
		qq_count=$((qq_count + 1))
		cmdline=$(tr '\0' ' ' <"/proc/$pid/cmdline" 2>/dev/null || true)
		[[ $cmdline == *"--user-data-dir=$profile/chromium"* ]] && profile_match=1
	done
	((qq_count > 0 && profile_match == 1)) || { log "ERROR: isolated QQ main process missing qq_count=$qq_count profile_match=$profile_match"; exit 88; }
	log "TARGET_VALIDATED session=$session cgroup=$control_group qq_processes=$qq_count profile_main_match=$profile_match stable_checks=$stable"
	domain_id=$(stat -c %i "$cgroup_path")
	active_domain_id=$domain_id
	active_app_id=$qq_app_id
	cat "$parp/scan_budget_stats" >"$session_dir/runtime/scan_budget_stats_before.txt"
	cat "$parp/evidence_stats" >"$session_dir/runtime/evidence_stats_before.txt"
	apply_before=$(awk '/^apply_count /{print $2}' "$session_dir/runtime/scan_budget_stats_before.txt")

	active_trace=$trace_root/instances/$trace_name
	mkdir "$active_trace"
	printf '0\n' >"$active_trace/tracing_on"
	printf '0\n' >"$active_trace/events/enable"
	printf '65536\n' >"$active_trace/buffer_size_kb"
	printf 'mono\n' >"$active_trace/trace_clock"
	printf '1\n' >"$active_trace/events/parp/parp_region_evidence/enable"
	[[ -e $active_trace/events/damon/damon_aggregated/enable ]] && printf '1\n' >"$active_trace/events/damon/damon_aggregated/enable"
	: >"$active_trace/trace"
	stdbuf -oL cat "$active_trace/trace_pipe" >"$raw_trace" &
	trace_reader=$!
	printf '1\n' >"$active_trace/tracing_on"

	printf '%s %s 10800000 1 27\n' "$domain_id" "$qq_app_id" >"$parp/app_bind"
	prior_echo=$(cat "$parp/app_prior_batch")
	initial_generation=$(printf '%s\n' "$prior_echo" | sed -n 's/.*generation=\([0-9][0-9]*\).*/\1/p')
	[[ -n $initial_generation ]] || initial_generation=0
	prior_generation=$((initial_generation + 1))
	prior_timestamp_ns=$(mono_ns)
	printf '1 27 %s %s 60000000000 %s 1 %s 16384 1 1 1 0\n' \
		"$prior_generation" "$prior_timestamp_ns" "$((prior_timestamp_ns + 600000000000))" "$qq_app_id" \
		>"$parp/app_prior_batch"
	prior_refresh_seconds=$SECONDS

	printf '1\n' >"$damon/kdamonds/nr_kdamonds"
	damon_created=1
	printf '1\n' >"$damon/kdamonds/0/contexts/nr_contexts"
	printf 'vaddr\n' >"$damon/kdamonds/0/contexts/0/operations"
	printf '5000\n' >"$damon/kdamonds/0/contexts/0/monitoring_attrs/intervals/sample_us"
	printf '1000000\n' >"$damon/kdamonds/0/contexts/0/monitoring_attrs/intervals/aggr_us"
	printf '60000000\n' >"$damon/kdamonds/0/contexts/0/monitoring_attrs/intervals/update_us"
	printf '10\n' >"$damon/kdamonds/0/contexts/0/monitoring_attrs/nr_regions/min"
	printf '1000\n' >"$damon/kdamonds/0/contexts/0/monitoring_attrs/nr_regions/max"
	printf '%s\n' "${#pids[@]}" >"$damon/kdamonds/0/contexts/0/targets/nr_targets"
	for index in "${!pids[@]}"; do
		printf '%s\n' "${pids[$index]}" >"$damon/kdamonds/0/contexts/0/targets/$index/pid_target"
	done
	printf 'on\n' >"$damon/kdamonds/0/state"
	collection_start_ns=$(date +%s%N)
	context_ready=0
	for _ in $(seq 1 300); do
		if grep -q 'parp_region_evidence:' "$raw_trace" 2>/dev/null; then
			context_ready=1
			break
		fi
		kill -0 "$automation_pid" || { log "ERROR: automation exited during evidence gate session=$session"; exit 89; }
		sleep 0.1
	done
	if ((context_ready != 1)); then
		cat "$parp/evidence_stats" >"$session_dir/runtime/evidence_stats_context_gate.txt" 2>/dev/null || true
		cat "$parp/scan_budget_stats" >"$session_dir/runtime/scan_budget_stats_context_gate.txt" 2>/dev/null || true
		cat "$parp/app_prior_batch" >"$session_dir/runtime/app_prior_batch_context_gate.txt" 2>/dev/null || true
		{
			printf 'control_group=%s\n' "$control_group"
			printf 'cgroup_inode=%s\n' "$domain_id"
			printf 'memory_current=%s\n' "$(cat "$cgroup_path/memory.current" 2>/dev/null || echo unavailable)"
			printf 'target_count=%s\n' "${#pids[@]}"
			printf 'damon_trace_lines=%s\n' "$(grep -c 'damon_aggregated:' "$raw_trace" 2>/dev/null || true)"
			printf 'parp_trace_lines=%s\n' "$(grep -c 'parp_region_evidence:' "$raw_trace" 2>/dev/null || true)"
		} >"$session_dir/runtime/context_gate_diagnostics.txt"
		log "ERROR: no parp_region_evidence after 30s session=$session domain=$domain_id targets=${#pids[@]}"
		exit 94
	fi
	touch "$ready"
	chown "$run_uid:$run_gid" "$ready"
	pilot_gate_start_seconds=$SECONDS
	log "COLLECTOR_READY session=$session domain=$domain_id targets=${#pids[@]}"

	for elapsed in $(seq 1 3000); do
		[[ -e $automation_done ]] && break
		kill -0 "$automation_pid" || { log "ERROR: automation exited session=$session"; exit 89; }
		if ((elapsed % 5 == 0)); then
			[[ -r $cgroup_path/cgroup.procs ]] || { log "ERROR: automation cgroup exited session=$session"; exit 89; }
			mapfile -t discovered_pids < <(list_phase210_qq_pids "$profile" "$automation_pgid")
			for pid in "${discovered_pids[@]}"; do
				[[ -r /proc/$pid/cgroup ]] || continue
				current_cgroup=$(sed -n 's/^0:://p' "/proc/$pid/cgroup" 2>/dev/null || true)
				[[ -n $current_cgroup ]] || continue
				[[ $current_cgroup == "$control_group" ]] || { log "ERROR: QQ target escaped dedicated cgroup pid=$pid cgroup=$current_cgroup"; exit 95; }
			done
			mapfile -t member_pids < <(cat "$cgroup_path/cgroup.procs" | sort -nu)
			for pid in "${member_pids[@]}"; do
				[[ -d /proc/$pid ]] || continue
				phase210_pid_is_safe "$pid" "$profile" "$automation_pgid" || { log "ERROR: non-test process entered QQ target cgroup pid=$pid"; exit 95; }
			done
		fi
		if [[ $mode == positive-support-pilot && $pilot_gate_checked -eq 0 &&
		      $((SECONDS - pilot_gate_start_seconds)) -ge 300 ]]; then
			pilot_gate_checked=1
			pilot_snapshot=$session_dir/trace/positive_support_300s.filtered
			pilot_audit=$session_dir/state/positive_support_300s.json
			grep 'parp_region_evidence:' "$raw_trace" >"$pilot_snapshot" || true
			run_as_user python3 "$positive_auditor" --trace "$pilot_snapshot" \
				--session "$session" --output "$pilot_audit" >>"$log_file"
			read -r support_positive support_pairwise < <(python3 - "$pilot_audit" <<'PY'
import json, sys
row=json.load(open(sys.argv[1]))
print(row['positive_candidates_60s'], row['pairwise_evaluable_decisions_60s'])
PY
)
			log "PILOT_300S_SUPPORT positive_candidates=$support_positive pairwise_decisions=$support_pairwise"
			if ((support_positive == 0)); then
				early_stop=1
				log 'PILOT_EARLY_STOP reason=no_positive_candidate_at_300s'
				kill -TERM -- "-$automation_pgid" 2>/dev/null || true
				break
			fi
		fi
		if ((SECONDS - prior_refresh_seconds >= 480)); then
			prior_generation=$((prior_generation + 1))
			prior_timestamp_ns=$(mono_ns)
			printf '1 27 %s %s 60000000000 %s 1 %s 16384 1 1 1 0\n' \
				"$prior_generation" "$prior_timestamp_ns" "$((prior_timestamp_ns + 600000000000))" "$qq_app_id" \
				>"$parp/app_prior_batch"
			prior_refresh_seconds=$SECONDS
			log "PRIOR_REFRESH session=$session generation=$prior_generation"
		fi
		((elapsed % 300 == 0)) && log "SESSION_PROGRESS session=$session elapsed=$elapsed"
		sleep 1
	done
	if ((early_stop == 0)); then
		[[ -e $automation_done ]] || { log "ERROR: automation timeout session=$session"; exit 90; }
	fi
	collection_end_ns=$(date +%s%N)
	stop_damon
	printf '0\n' >"$active_trace/tracing_on"
	printf '0\n' >"$active_trace/events/parp/parp_region_evidence/enable"
	kill "$trace_reader" 2>/dev/null || true
	wait "$trace_reader" 2>/dev/null || true
	trace_reader=
	grep 'parp_region_evidence:' "$raw_trace" >"$session_dir/trace/parp_region_evidence.filtered" || true
	file_regions=$(grep -Ec 'parp_region_evidence:.* type=0 ' "$session_dir/trace/parp_region_evidence.filtered" || true)
	anon_regions=$(grep -Ec 'parp_region_evidence:.* type=1 ' "$session_dir/trace/parp_region_evidence.filtered" || true)
	lost=$(awk -F: '/entries-in-buffer\/entries-written/{gsub(/ /,"",$2); split($2,a,"/"); lost+=a[2]-a[1]} END{print lost+0}' "$active_trace/per_cpu/cpu"*/stats)
	cat "$active_trace/per_cpu/cpu"*/stats >"$session_dir/trace/per_cpu_stats.txt"
	cat "$parp/scan_budget_stats" >"$session_dir/runtime/scan_budget_stats_after.txt"
	cat "$parp/evidence_stats" >"$session_dir/runtime/evidence_stats_after.txt"
	apply_after=$(awk '/^apply_count /{print $2}' "$session_dir/runtime/scan_budget_stats_after.txt")
	((apply_after == apply_before)) || { log "ERROR: Apply changed during Observe collection"; exit 91; }
	((file_regions > 0 || anon_regions > 0)) || { log "ERROR: no PARP evidence session=$session"; exit 92; }
	((lost == 0)) || { log "ERROR: trace lost=$lost session=$session"; exit 93; }
	if [[ $mode == positive-support-pilot ]]; then
		pilot_audit=$session_dir/state/positive_support_final.json
		run_as_user python3 "$positive_auditor" --trace "$session_dir/trace/parp_region_evidence.filtered" \
			--session "$session" --output "$pilot_audit" >>"$log_file"
		read -r support_positive support_pairwise < <(python3 - "$pilot_audit" <<'PY'
import json, sys
row=json.load(open(sys.argv[1]))
print(row['positive_candidates_60s'], row['pairwise_evaluable_decisions_60s'])
PY
)
		log "PILOT_FINAL_SUPPORT positive_candidates=$support_positive pairwise_decisions=$support_pairwise"
	fi
	printf '%s %s 1 999 27\n' "$domain_id" "$qq_app_id" >"$parp/app_bind"
	active_domain_id=
	active_app_id=

	python3 - "$session_dir/state/session.json" "$session" "$domain_id" "$control_group" "$collection_start_ns" "$collection_end_ns" "$file_regions" "$anon_regions" "$lost" "${#pids[@]}" "$mode" "$early_stop" "$support_positive" "$support_pairwise" <<'PY'
import json, os, platform, sys
path, session, domain, cgroup, start, end, file_regions, anon_regions, lost, targets, mode, early_stop, positives, pairwise=sys.argv[1:]
payload={"schema_version":1,"session_id":session,"app":"QQ","app_id":2,
 "split":"pilot" if mode == "positive-support-pilot" else ("train" if "train" in session else "validation"),"domain_id":int(domain),
 "cgroup_path":cgroup,"collection_start_ns":int(start),"collection_end_ns":int(end),
 "duration_seconds":(int(end)-int(start))/1e9,"file_regions":int(file_regions),
 "anon_regions":int(anon_regions),"trace_lost":int(lost),"target_count":int(targets),
 "kernel_release":platform.release(),"observe_only":True,"apply":False,
 "privacy":"AUTHORIZED_QQ_ACCOUNT_READ_ONLY_NO_SEND","authenticated_ui":True,"ab_eligible":False,
 "positive_support_pilot":mode == "positive-support-pilot","early_stop_no_positive_at_300s":bool(int(early_stop)),
 "positive_candidates_60s":int(positives),"pairwise_evaluable_decisions_60s":int(pairwise)}
tmp=path+'.tmp'
with open(tmp,'w') as stream: json.dump(payload,stream,indent=2,sort_keys=True); stream.write('\n')
os.replace(tmp,path)
PY
	if ((early_stop == 0)); then
		touch "$collector_done"
		chown "$run_uid:$run_gid" "$collector_done"
		wait "$automation_pid"
	else
		wait "$automation_pid" 2>/dev/null || true
	fi
	automation_pid=
	automation_pgid=
	stop_active_scope
	rmdir "$active_trace"
	active_trace=
	chown -R "$run_uid:$run_gid" "$session_dir"
	log "SESSION_COMPLETE session=$session file_regions=$file_regions anon_regions=$anon_regions trace_lost=$lost apply_delta=0"
}

profiles=$collection/profiles
mkdir -p "$profiles"
for session in "${sessions[@]}"; do
	collect_session "$session" "$collection/config/$session.json" "$profiles/$session"
	[[ $session == "${sessions[-1]}" ]] || sleep 30
done

if [[ $mode == positive-support-pilot ]]; then
	python3 - "$collection/state/collection.json" "$collection" <<'PY'
import json, pathlib, platform, sys, time
path=pathlib.Path(sys.argv[1]); root=pathlib.Path(sys.argv[2])
session=json.load((root/'raw/qq_positive_support_pilot/state/session.json').open())
support=json.load((root/'raw/qq_positive_support_pilot/state/positive_support_final.json').open())
status='PARP_PHASE210_POSITIVE_SUPPORT_PILOT_PASSED' if support['passed'] else 'PARP_PHASE210_POSITIVE_SUPPORT_PILOT_INSUFFICIENT'
payload={"status":status,"timestamp_ns":time.time_ns(),"kernel_release":platform.release(),
 "sessions":[session],"positive_support":support,"root_used":True,"cgroup_limits_changed":False,
 "pressure":False,"apply":False,"message_sent":False}
path.write_text(json.dumps(payload,indent=2,sort_keys=True)+'\n')
PY
else
	python3 - "$collection/state/collection.json" "$collection" <<'PY'
import json, pathlib, platform, sys, time
path=pathlib.Path(sys.argv[1]); root=pathlib.Path(sys.argv[2]); sessions=[]
for name in ('qq_train_01','qq_validation_01'):
 sessions.append(json.load((root/'raw'/name/'state/session.json').open()))
payload={"status":"PARP_PHASE210_QQ_COLLECTION_COMPLETE","timestamp_ns":time.time_ns(),
 "kernel_release":platform.release(),"sessions":sessions,"root_used":True,
 "cgroup_limits_changed":False,"pressure":False,"apply":False}
path.write_text(json.dumps(payload,indent=2,sort_keys=True)+'\n')
PY
fi
chown -R "$run_uid:$run_gid" "$collection"
trap - EXIT INT TERM
cleanup_runtime
printf '{"damon_stopped":true,"trace_removed":true,"scopes_stopped":true,"cgroup_limits_changed":false,"apply":false}\n' >"$collection/cleanup/cleanup.json"
chown -R "$run_uid:$run_gid" "$collection"
if [[ $mode == positive-support-pilot ]]; then
	status=$(python3 - "$collection/state/collection.json" <<'PY'
import json, sys
print(json.load(open(sys.argv[1]))['status'])
PY
)
	log "$status"
else
	log 'PARP_PHASE210_QQ_COLLECTION_COMPLETE'
fi
printf 'output=%s\n' "$collection"
