#!/bin/sh
# SPDX-License-Identifier: GPL-2.0
set -eu

base=${1:-63d1eedfd5da3e226e05de0799c329451b1f2df7}
core=mm/parp/core/scan_budget.c

reject()
{
	pattern=$1
	shift
	if grep -rnE "$pattern" "$@"; then
		echo "forbidden pattern: $pattern" >&2
		exit 1
	fi
}

reject 'scan_control|lruvec|mem_cgroup' "$core"
reject 'kmalloc|kzalloc|vmalloc|netlink|vfs_|kernel_(read|write)|rmap_walk' \
	mm/parp/adapter/scan_budget_adapter.c "$core"
reject 'CONTINUE|REENTRY|workload[_ -]?hint|suggestion[_ -]?mask' \
	--exclude=check_phase25_architecture.sh mm/parp include/linux/parp.h tools/parp

native_diff=$(git diff --name-only "$base"..HEAD -- \
	mm/vmscan.c mm/memcontrol.c mm/damon/core.c)
structure_diff=$(git diff --name-only "$base"..HEAD -- \
	include/linux/mm_types.h include/linux/mmzone.h include/linux/memcontrol.h)
test "$native_diff" = "mm/vmscan.c"
test -z "$structure_diff"
grep -q 'PARP_SCAN_BUDGET_OBSERVE' "$core"
grep -q 'PARP_RECLAIM_SCOPE_TARGET_MEMCG' "$core"
grep -q 'decision->applied_nr_to_scan' mm/parp/adapter/scan_budget_adapter.c
echo "PASS: Phase 2.5 architecture constraints"
