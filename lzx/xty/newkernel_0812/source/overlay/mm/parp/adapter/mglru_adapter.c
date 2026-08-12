// SPDX-License-Identifier: GPL-2.0
#include <linux/mm.h>
#include <linux/memcontrol.h>
#include <linux/parp.h>
#include <linux/swap.h>
#include <trace/events/parp.h>
#include "../../internal.h"
#include "../internal.h"
#include "adapter.h"

void parp_adapter_prepare(struct parp_reclaim_ctx *ctx,
			  struct lruvec *lruvec, struct scan_control *sc)
{
	struct mem_cgroup *memcg = lruvec_memcg(lruvec);

	memset(ctx, 0, sizeof(*ctx));
	ctx->mode = parp_get_mode();
	atomic64_inc(&parp_stats.prepare);
	if (ctx->mode == PARP_MODE_DISABLED)
		return;
	ctx->domain_id = parp_memcg_domain_id(memcg);
	ctx->snapshot = parp_snapshot_acquire();
	ctx->rcu_held = true;
}

struct parp_decision
parp_adapter_score_folio(struct parp_reclaim_ctx *ctx, struct folio *folio,
			 int type, int generation)
{
	struct parp_page_sample sample = {
		.type = type ? PARP_PAGE_FILE : PARP_PAGE_ANON,
		.domain_id = ctx->domain_id,
		.generation = generation,
		.resident = true,
		.dirty = folio_test_dirty(folio),
		.writeback = folio_test_writeback(folio),
		.unevictable = !folio_evictable(folio),
		/*
		 * Access windows are filled asynchronously by the DAMON adapter.
		 * Until a matching side-table entry exists, native fallback is
		 * mandatory.
		 */
		.evidence_valid = false,
	};
	struct parp_decision decision;
	bool matched = false;

	if (ctx->mode == PARP_MODE_DISABLED) {
		decision = (struct parp_decision) {
			.original_action = PARP_ACTION_NATIVE,
			.proposed_action = PARP_ACTION_NATIVE,
			.applied_action = PARP_ACTION_NATIVE,
			.fallback = PARP_FALLBACK_DISABLED,
		};
	} else {
		if (sample.type == PARP_PAGE_FILE)
			matched = parp_file_sample_from_folio(ctx, folio, &sample);
		else
			matched = parp_anon_sample_from_domain(ctx, &sample);
		if (!matched)
			sample.evidence_valid = false;
		if (parp_get_evidence_mode() == PARP_EVIDENCE_ONLY) {
			decision = (struct parp_decision) {
				.original_action = PARP_ACTION_NATIVE,
				.proposed_action = PARP_ACTION_NATIVE,
				.applied_action = PARP_ACTION_NATIVE,
				.fallback = PARP_FALLBACK_EVIDENCE_ONLY,
			};
		} else {
			decision = parp_engine_score(ctx->snapshot, &sample);
		}
	}
	parp_stats_account(&decision);
	trace_parp_decision(ctx->domain_id, ctx->mode, sample.type,
			    decision.original_action, decision.proposed_action,
			    decision.applied_action, decision.fallback,
			    decision.score_q15);
	return decision;
}

void parp_adapter_apply_decision(struct parp_reclaim_ctx *ctx,
				 struct folio *folio,
				 struct parp_decision *decision)
{
	/* Observe-only and the initial APPLY placeholder both remain native. */
	decision->applied_action = decision->original_action;
}

void parp_adapter_finish(struct parp_reclaim_ctx *ctx,
			 unsigned long scanned, unsigned long isolated)
{
	if (ctx->rcu_held)
		parp_snapshot_release();
	ctx->rcu_held = false;
	atomic64_inc(&parp_stats.finish);
}
