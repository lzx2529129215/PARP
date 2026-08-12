// SPDX-License-Identifier: GPL-2.0
#include <linux/overflow.h>
#include "../internal.h"

bool parp_align_interval(u64 start, u64 end, u64 page_size,
			 u64 *aligned_start, u64 *aligned_end)
{
	u64 tail;

	if (!page_size || !is_power_of_2(page_size) || start >= end)
		return false;
	*aligned_start = round_down(start, page_size);
	if (check_add_overflow(end, page_size - 1, &tail))
		return false;
	*aligned_end = round_down(tail, page_size);
	return *aligned_start < *aligned_end;
}

bool parp_file_range_from_vma(u64 vm_start, u64 vm_pgoff,
			      u64 start, u64 end, u64 page_size,
			      u64 *file_page_start, u32 *nr_pages)
{
	u64 relative_pages, pages, result;

	if (!page_size || !is_power_of_2(page_size) || start < vm_start ||
	    start >= end || start % page_size || end % page_size)
		return false;
	relative_pages = (start - vm_start) / page_size;
	pages = (end - start) / page_size;
	if (!pages || pages > U32_MAX ||
	    check_add_overflow(vm_pgoff, relative_pages, &result))
		return false;
	*file_page_start = result;
	*nr_pages = pages;
	return true;
}

bool parp_anon_range_from_vma(u64 vm_start, u64 start, u64 end,
			      u64 page_size, u32 *relative_start_pages,
			      u32 *nr_pages)
{
	u64 relative, pages;

	if (!page_size || !is_power_of_2(page_size) || start < vm_start ||
	    start >= end || start % page_size || end % page_size)
		return false;
	relative = (start - vm_start) / page_size;
	pages = (end - start) / page_size;
	if (relative > U32_MAX || !pages || pages > U32_MAX)
		return false;
	*relative_start_pages = relative;
	*nr_pages = pages;
	return true;
}

u64 parp_vma_signature(u32 anon_class, u64 semantic_flags,
		       u64 length_pages, u32 process_role, u64 name_hash)
{
	u64 value = 0xcbf29ce484222325ULL;
	u64 fields[] = { anon_class, semantic_flags, ilog2(length_pages | 1),
			 process_role, name_hash };
	unsigned int i;

	for (i = 0; i < ARRAY_SIZE(fields); i++) {
		value ^= fields[i];
		value *= 0x100000001b3ULL;
		value ^= value >> 32;
	}
	return value ?: 1;
}

bool parp_segments_conserve(const u64 *starts, const u64 *ends,
			    unsigned int nr_segments, u64 original_start,
			    u64 original_end)
{
	u64 cursor = original_start;
	unsigned int i;

	if (!nr_segments || original_start >= original_end)
		return false;
	for (i = 0; i < nr_segments; i++) {
		if (starts[i] != cursor || starts[i] >= ends[i])
			return false;
		cursor = ends[i];
	}
	return cursor == original_end;
}
