// SPDX-License-Identifier: GPL-2.0
#include <linux/cgroup.h>
#include <linux/memcontrol.h>
#include "../internal.h"
#include "adapter.h"

u64 parp_memcg_domain_id(struct mem_cgroup *memcg)
{
	if (!memcg || mem_cgroup_is_root(memcg))
		return 0;
	return cgroup_ino(memcg->css.cgroup);
}
