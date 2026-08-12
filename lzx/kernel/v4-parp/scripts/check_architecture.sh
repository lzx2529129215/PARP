#!/bin/sh
set -eu

tree=${1:-.}
base=${2:-upstream-v6.17.13-baseline}
bad=0

fail()
{
	printf 'architecture check: %s\n' "$1" >&2
	bad=1
}

# The prediction engine, model, and mapping layers remain independent of
# native VM objects.  effective_tier.c, frontier_score.c, and watermark.c are
# explicit kernel-policy integration modules and are checked separately.
for area in \
	mm/parp/core/budget.c \
	mm/parp/core/domain.c \
	mm/parp/core/engine.c \
	mm/parp/core/evidence.c \
	mm/parp/core/fallback.c \
	mm/parp/core/policy.c \
	mm/parp/core/scan_budget.c \
	mm/parp/core/snapshot.c \
	mm/parp/core/stats.c \
	mm/parp/model \
	mm/parp/mapping; do
	if grep -R -n -E \
		'struct (folio|page|lruvec|scan_control|mem_cgroup|mm_struct|vm_area_struct|damon_region)' \
		"$tree/$area"; then
		fail "native VM type leaked into engine-neutral area: $area"
	fi
done

if grep -R -n -E \
	'CONTINUE|REENTRY|suggestion_mask|workload_hint|dual_markov' \
	"$tree/mm/parp" "$tree/include/linux/parp.h"; then
	fail 'retired legacy policy token is present'
fi

# Reclaim adapters execute on sensitive paths: no allocation, file I/O, VMA
# walk, reverse-map walk, or folio lock acquisition is allowed there.
for adapter in mglru_adapter.c file_adapter.c anon_adapter.c; do
	if grep -n -E \
		'kmalloc|kzalloc|kcalloc|vmalloc|filp_open|kernel_read|readahead|find_vma|mmap_read_lock|rmap_walk|folio_lock' \
		"$tree/mm/parp/adapter/$adapter"; then
		fail "blocking or allocating operation in hot-path adapter: $adapter"
	fi
done

# PARP metadata may extend lru_gen_folio and mem_cgroup only through the
# audited, configuration-guarded feature state.  It must not enlarge the
# fundamental page/folio objects or allocate a new page flag.
if git -C "$tree" diff --unified=0 "$base" -- \
	include/linux/mm_types.h include/linux/page-flags.h |
	grep -E '^[+].*(struct (page|folio)|PG_[A-Za-z0-9_]+)'; then
	fail 'struct page/folio or page flags were extended'
fi
for required in \
	'CONFIG_PARP_FRONTIER_SCORE' \
	'parp_frontier\[ANON_AND_FILE\]' \
	'CONFIG_PARP_EFFECTIVE_TIER' \
	'parp_effective_tier\[ANON_AND_FILE\]'; do
	if ! grep -q "$required" "$tree/include/linux/mmzone.h"; then
		fail "missing guarded lru_gen_folio state: $required"
	fi
done
if ! grep -q 'struct parp_tier2_memcg parp_tier2;' \
	"$tree/include/linux/memcontrol.h"; then
	fail 'missing audited PARP memcg state'
fi

# Keep the native-kernel integration surface explicit.  Documentation,
# PARP-owned code, and user-space tools are excluded from this allowlist.
native=$(git -C "$tree" diff --name-only "$base" -- |
	grep -Ev '^(Documentation/admin-guide/mm/|docs/|include/linux/parp\.h$|include/linux/parp_tier2\.h$|include/trace/events/parp\.h$|mm/parp/|tools/parp/)' |
	sort || true)
expected_native='fs/proc/task_mmu.c
include/linux/memcontrol.h
include/linux/mm_inline.h
include/linux/mmzone.h
include/linux/swap.h
mm/Kconfig
mm/Makefile
mm/damon/core.c
mm/damon/ops-common.c
mm/damon/paddr.c
mm/filemap.c
mm/gup.c
mm/huge_memory.c
mm/ksm.c
mm/madvise.c
mm/memcontrol.c
mm/memory.c
mm/page_alloc.c
mm/page_ext.c
mm/page_idle.c
mm/rmap.c
mm/shmem.c
mm/swap.c
mm/vmscan.c'
if [ "$native" != "$expected_native" ]; then
	printf 'architecture check: unexpected native-kernel integration surface\n' >&2
	printf '%s\n' "$native" >&2
	bad=1
fi

# Preserve the required observation and fail-safe hooks at the integration
# boundary.  These checks deliberately test behavior-bearing call sites rather
# than assuming the original v4 file list.
if ! grep -q 'parp_damon_aggregate(c, t, r);' "$tree/mm/damon/core.c"; then
	fail 'DAMON aggregation hook is missing'
fi
if ! grep -q 'parp_effective_tier_note_access' \
	"$tree/mm/damon/ops-common.c"; then
	fail 'DAMON effective-tier access hook is missing'
fi
if ! grep -q 'folio_mark_accessed_source' "$tree/mm/damon/paddr.c"; then
	fail 'DAMON physical-address access source hook is missing'
fi
if ! grep -q 'decision->applied_action = decision->original_action' \
	"$tree/mm/parp/adapter/mglru_adapter.c"; then
	fail 'native MGLRU fallback assignment is missing'
fi

if [ "$bad" -ne 0 ]; then
	exit 1
fi
printf 'PARP architecture checks passed (%s).\n' "$base"
