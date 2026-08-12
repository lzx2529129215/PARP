// SPDX-License-Identifier: GPL-2.0
#include "../internal.h"

bool parp_file_key_equal(const struct parp_file_region_key *a,
			 const struct parp_file_region_key *b)
{
	return !memcmp(a, b, sizeof(*a));
}

u32 parp_backing_classify(bool has_file, bool shmem, bool deleted,
			  bool executable)
{
	if (shmem)
		return PARP_BACKING_SHMEM;
	if (!has_file)
		return PARP_BACKING_UNKNOWN;
	if (deleted)
		return PARP_BACKING_DELETED_FILE;
	if (executable)
		return PARP_BACKING_EXECUTABLE;
	return PARP_BACKING_REGULAR_FILE;
}
