#!/bin/sh
set -eu

tree=${1:-.}
bad=0
base=${2:-upstream-v6.17.13-baseline}
for area in mm/parp/core mm/parp/model mm/parp/mapping; do
	if grep -R -n -E 'struct (folio|page|lruvec|scan_control|mem_cgroup|mm_struct|vm_area_struct|damon_region)' "$tree/$area"; then
		bad=1
	fi
done
if grep -R -n -E 'CONTINUE|REENTRY|suggestion_mask|workload_hint|dual_markov' \
	"$tree/mm/parp" "$tree/include/linux/parp.h"; then
	bad=1
fi
if grep -R -n -E 'kmalloc|kzalloc|vmalloc|filp_open|kernel_read|readahead' \
	"$tree/mm/parp/adapter/mglru_adapter.c"; then
	bad=1
fi
if grep -R -n -E 'find_vma|mmap_read_lock|rmap_walk|folio_lock|kmalloc|kzalloc|vmalloc' \
	"$tree/mm/parp/adapter/mglru_adapter.c" \
	"$tree/mm/parp/adapter/file_adapter.c" \
	"$tree/mm/parp/adapter/anon_adapter.c"; then
	bad=1
fi
if git -C "$tree" diff --unified=0 "$base"..HEAD -- \
	include/linux/mm_types.h include/linux/mmzone.h \
	include/linux/memcontrol.h include/linux/page-flags.h \
	| grep -E '^[+].*(struct (page|folio|lruvec|mem_cgroup|scan_control)|PG_[A-Za-z0-9_]+)'; then
	bad=1
fi
native=$(git -C "$tree" diff --name-only "$base" -- |
	grep -Ev '^(include/linux/parp.h|include/trace/events/parp.h|mm/parp/|tools/parp/)' ||
	true)
if [ "$native" != "mm/Kconfig
mm/Makefile
mm/damon/core.c
mm/vmscan.c" ]; then
	printf '%s\n' "$native"
	bad=1
fi
damon_added=$(git -C "$tree" diff --unified=0 "$base" -- mm/damon/core.c |
	grep -E '^\+[^+]' || true)
case "$damon_added" in
*'#include <linux/parp.h>'*'parp_damon_aggregate(c, t, r);'*) ;;
*) printf '%s\n' "$damon_added"; bad=1 ;;
esac
if ! grep -q 'decision->applied_action = decision->original_action' \
	"$tree/mm/parp/adapter/mglru_adapter.c"; then
	bad=1
fi
test "$bad" -eq 0
