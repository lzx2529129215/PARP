// SPDX-License-Identifier: GPL-2.0
#include <linux/memcontrol.h>
#include <linux/mm.h>
#include <linux/parp.h>
#include <trace/events/parp.h>
#include "../internal.h"
#include "adapter.h"

static atomic64_t parp_budget_sequence = ATOMIC64_INIT(0);

static enum parp_reclaim_scope
parp_reclaim_scope(struct mem_cgroup *target_memcg, bool global_kswapd,
		   bool proactive)
{
	if (!target_memcg || mem_cgroup_is_root(target_memcg))
		return global_kswapd ? PARP_RECLAIM_SCOPE_GLOBAL_KSWAPD :
			PARP_RECLAIM_SCOPE_GLOBAL_DIRECT;
	if (proactive)
		return PARP_RECLAIM_SCOPE_PROACTIVE_MEMCG;
	/* Linux 6.17.13 does not retain a distinct memory.high origin in sc. */
	return PARP_RECLAIM_SCOPE_TARGET_MEMCG;
}

static enum parp_pressure_level parp_pressure_from_priority(int priority)
{
	if (priority <= 1)
		return PARP_PRESSURE_EMERGENCY;
	if (priority <= 4)
		return PARP_PRESSURE_HIGH;
	if (priority <= 8)
		return PARP_PRESSURE_ELEVATED;
	return PARP_PRESSURE_NORMAL;
}

bool parp_adapter_prepare_scan_budget(struct lruvec *lruvec,
				      struct mem_cgroup *target_memcg,
				      bool global_kswapd, bool proactive,
				      int reclaim_priority,
				      unsigned long native_nr_to_scan,
				      struct parp_scan_budget_decision *decision)
{
	struct parp_scan_budget_input input = {
		.reclaim_scope = parp_reclaim_scope(target_memcg, global_kswapd,
						     proactive),
		.pressure = parp_pressure_from_priority(reclaim_priority),
		.native_nr_to_scan = native_nr_to_scan,
		.reclaim_priority = reclaim_priority,
		.now_ns = ktime_get_mono_fast_ns(),
	};
	bool context_valid;
	u64 prediction_age_ms = 0;

	if (target_memcg && !mem_cgroup_is_root(target_memcg))
		input.domain_id = parp_memcg_domain_id(target_memcg);
	parp_snapshot_fill_scan_budget_input(input.domain_id, input.now_ns,
					     &input);
	context_valid = (input.flags & (PARP_SCAN_INPUT_BIND_VALID |
			 PARP_SCAN_INPUT_PRIOR_VALID |
			 PARP_SCAN_INPUT_GENERATION_VALID |
			 PARP_SCAN_INPUT_MODEL_COMPATIBLE)) ==
			(PARP_SCAN_INPUT_BIND_VALID |
			 PARP_SCAN_INPUT_PRIOR_VALID |
			 PARP_SCAN_INPUT_GENERATION_VALID |
			 PARP_SCAN_INPUT_MODEL_COMPATIBLE);
	if (parp_scan_budget_guard(input.domain_id,
				   input.prediction_generation, context_valid))
		input.flags |= PARP_SCAN_INPUT_CIRCUIT_OK;
	parp_compute_scan_budget(&input, decision);
	if (decision->reason == PARP_SCAN_REASON_CIRCUIT_BREAKER &&
	    (input.flags & PARP_SCAN_INPUT_CIRCUIT_OK))
		parp_scan_budget_guard(input.domain_id,
				       input.prediction_generation, false);
	decision->budget_sequence = atomic64_inc_return(&parp_budget_sequence);
	parp_scan_budget_account(&input, decision);
	if (input.prediction_timestamp_ns &&
	    input.now_ns >= input.prediction_timestamp_ns)
		prediction_age_ms = div_u64(input.now_ns -
			input.prediction_timestamp_ns, NSEC_PER_MSEC);
	trace_parp_scan_budget_decision(&(struct parp_scan_budget_trace) {
		.timestamp_ns = input.now_ns,
		.budget_sequence = decision->budget_sequence,
		.reclaim_scope = input.reclaim_scope,
		.domain_id = input.domain_id,
		.app_id = input.app_id,
		.foreground = input.foreground,
		.app_score_q15 = input.app_use_score_q15,
		.app_rank = input.app_rank,
		.prediction_generation = input.prediction_generation,
		.model_version = input.model_version,
		.prediction_age_ms = prediction_age_ms,
		.native_nr_to_scan = decision->native_nr_to_scan,
		.proposed_nr_to_scan = decision->proposed_nr_to_scan,
		.applied_nr_to_scan = decision->applied_nr_to_scan,
		.multiplier_q15 = decision->multiplier_q15,
		.pressure_level = input.pressure,
		.reclaim_priority = input.reclaim_priority,
		.reason = decision->reason,
		.mode = parp_get_scan_budget_mode(),
	});
	return decision->valid;
}

unsigned long parp_adapter_apply_scan_budget(
				const struct parp_scan_budget_decision *decision)
{
	return min_t(u64, decision->applied_nr_to_scan, ULONG_MAX);
}
