// SPDX-License-Identifier: GPL-2.0
#include "../internal.h"

bool parp_anon_key_valid(const struct parp_anon_region_key *key, u64 epoch)
{
	return key->foreground_epoch_id == epoch && key->mm_cookie;
}
