// SPDX-License-Identifier: GPL-2.0
#include "adapter.h"

bool parp_anon_sample_from_domain(struct parp_reclaim_ctx *ctx,
				  struct parp_page_sample *sample)
{
	struct parp_domain_anon_evidence evidence;
	struct parp_app_context context;
	u64 now = ktime_get_mono_fast_ns();

	if (!parp_evidence_lookup_anon_domain(ctx->domain_id, &evidence) ||
	    !evidence.observed_pages)
		return false;
	if (!parp_context_lookup(ctx->domain_id, now, &context) ||
	    context.app_id != evidence.app_id ||
	    context.bind_generation != evidence.bind_generation ||
	    context.model_version != evidence.model_version)
		return false;
	sample->accesses_10s = min_t(u64, evidence.active_pages_10s, U32_MAX);
	sample->accesses_30s = min_t(u64, evidence.active_pages_30s, U32_MAX);
	sample->accesses_60s = min_t(u64, evidence.active_pages_60s, U32_MAX);
	sample->active_ratio_q15 = min_t(u64,
		div64_u64(evidence.active_pages_30s * PARP_Q15_ONE,
			  evidence.observed_pages), PARP_Q15_ONE);
	sample->evidence_valid = evidence.confidence_q15 != 0;
	return sample->evidence_valid;
}
