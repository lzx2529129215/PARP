/* SPDX-License-Identifier: GPL-2.0 */
#ifndef _MM_PARP_ADAPTER_H
#define _MM_PARP_ADAPTER_H

#include "../internal.h"

struct folio;
struct mem_cgroup;
struct parp_reclaim_ctx;

u64 parp_memcg_domain_id(struct mem_cgroup *memcg);

bool parp_file_sample_from_folio(struct parp_reclaim_ctx *ctx,
				 struct folio *folio,
				 struct parp_page_sample *sample);
bool parp_anon_sample_from_domain(struct parp_reclaim_ctx *ctx,
				  struct parp_page_sample *sample);

#endif
