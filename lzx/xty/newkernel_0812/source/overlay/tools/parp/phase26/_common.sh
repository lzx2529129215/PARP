#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0
set -euo pipefail

PHASE26_SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
PHASE26_WORK_TREE=$(cd "$PHASE26_SCRIPT_DIR/../../.." && pwd)
PHASE26_PROJECT_ROOT=$(cd "$PHASE26_WORK_TREE/../../../.." && pwd)
PHASE26_DRY_RUN=0
PHASE26_OUTPUT_ROOT=""
PHASE26_EXTRA_ARGS=()

phase26_init()
{
	while (($#)); do
		case "$1" in
		--dry-run)
			PHASE26_DRY_RUN=1
			shift
			;;
		--output-root)
			test $# -ge 2 || { echo "--output-root requires a path" >&2; return 2; }
			PHASE26_OUTPUT_ROOT=$2
			shift 2
			;;
		--)
			shift
			PHASE26_EXTRA_ARGS+=("$@")
			break
			;;
		*)
			PHASE26_EXTRA_ARGS+=("$1")
			shift
			;;
		esac
	done
	if [[ -z "$PHASE26_OUTPUT_ROOT" ]]; then
		PHASE26_OUTPUT_ROOT=$(readlink -f \
			"$PHASE26_PROJECT_ROOT/outputs/parp_live_validation_current")
	fi
	test -d "$PHASE26_OUTPUT_ROOT/state" || {
		echo "invalid Phase 2.6 output root: $PHASE26_OUTPUT_ROOT" >&2
		return 2
	}
	PHASE26_STATE="$PHASE26_OUTPUT_ROOT/state/phase26_state.json"
	PHASE26_LOG_DIR="$PHASE26_OUTPUT_ROOT/command_logs"
	mkdir -p "$PHASE26_LOG_DIR"
	PHASE26_LOG="$PHASE26_LOG_DIR/$(basename "$0" .sh).log"
	printf '%s START script=%s dry_run=%s output=%s\n' \
		"$(date --iso-8601=seconds)" "$0" "$PHASE26_DRY_RUN" \
		"$PHASE26_OUTPUT_ROOT" >>"$PHASE26_LOG"
}

phase26_log()
{
	printf '%s %s\n' "$(date --iso-8601=seconds)" "$*" | tee -a "$PHASE26_LOG"
}

phase26_run()
{
	local rc
	local rendered
	printf -v rendered '%q ' "$@"
	phase26_log "RUN ${rendered% }"
	if ((PHASE26_DRY_RUN)); then
		phase26_log "DRY_RUN rc=0"
		return 0
	fi
	set +e
	"$@" >>"$PHASE26_LOG" 2>&1
	rc=$?
	set -e
	phase26_log "EXIT rc=$rc"
	return "$rc"
}

phase26_run_root()
{
	if ((PHASE26_DRY_RUN)); then
		phase26_run sudo "$@"
		return
	fi
	if [[ ! -t 0 ]]; then
		phase26_log "SUDO_INTERACTIVE_REQUIRED command=$*"
		return 77
	fi
	phase26_run sudo "$@"
}

phase26_read_root()
{
	local path=$1
	local value rc
	if ((PHASE26_DRY_RUN)); then
		printf 'DRY_RUN'
		return 0
	fi
	if [[ ! -t 0 ]]; then
		printf '%s SUDO_INTERACTIVE_REQUIRED command=cat %q\n' \
			"$(date --iso-8601=seconds)" "$path" >>"$PHASE26_LOG"
		return 77
	fi
	printf '%s RUN sudo cat %q\n' "$(date --iso-8601=seconds)" \
		"$path" >>"$PHASE26_LOG"
	set +e
	value=$(sudo cat "$path")
	rc=$?
	set -e
	printf '%s EXIT rc=%s\n' "$(date --iso-8601=seconds)" "$rc" \
		>>"$PHASE26_LOG"
	((rc == 0)) || return "$rc"
	printf '%s' "$value"
}

phase26_state_get()
{
	python3 "$PHASE26_SCRIPT_DIR/state_tool.py" get "$PHASE26_STATE" "$1"
}

phase26_state_set()
{
	python3 "$PHASE26_SCRIPT_DIR/state_tool.py" set "$PHASE26_STATE" "$1" "$2"
}

phase26_runtime_release()
{
	local bootfix_state
	bootfix_state=$(readlink -f \
		"$PHASE26_PROJECT_ROOT/outputs/parp_phase26_bootfix_current/state/bootfix_state.json" \
		2>/dev/null || true)
	python3 "$PHASE26_SCRIPT_DIR/runtime_identity.py" \
		"$PHASE26_STATE" "${bootfix_state:-/nonexistent}" \
		"$(uname -r)" "$PHASE26_WORK_TREE"
}

phase26_find_cgroup2_memory_root()
{
	local candidates=()
	local root
	if (($#)); then
		candidates=("$@")
	else
		mapfile -t candidates < <(findmnt -rn -t cgroup2 -o TARGET)
	fi
	for root in "${candidates[@]}"; do
		[[ -e "$root/memory.reclaim" ]] || continue
		[[ -r "$root/cgroup.controllers" ]] || continue
		grep -qw memory "$root/cgroup.controllers" || continue
		printf '%s\n' "$root"
		return 0
	done
	return 1
}

phase26_require_snapshot_marker()
{
	local user_home
	user_home=$(getent passwd "$(id -u)" | cut -d: -f6)
	test -f "$user_home/PARP_VMWARE_SNAPSHOT_CONFIRMED" || {
		phase26_log "BLOCKED_VMWARE_SNAPSHOT_CONFIRMATION"
		return 78
	}
}

phase26_require_observe()
{
	local parp_dir=${1:-/sys/kernel/debug/parp}
	((PHASE26_DRY_RUN)) && return 0
	test "$(phase26_read_root "$parp_dir/scan_budget_mode")" = 1
	test "$(phase26_read_root "$parp_dir/mode")" = 1
	test "$(phase26_read_root "$parp_dir/evidence_mode")" = 0
}

phase26_finish()
{
	phase26_log "DONE"
}
