// SPDX-License-Identifier: GPL-2.0
#include <linux/math64.h>
#include <linux/moduleparam.h>
#include <linux/overflow.h>
#include "../internal.h"

#define PARP_Q15_SCALE		32768U
#define PARP_SCAN_GUARD_FAILURES 3U

static unsigned int foreground_multiplier_q15 = 16384;
static unsigned int high_probability_multiplier_q15 = 19661;
static unsigned int medium_probability_multiplier_q15 = 26214;
static unsigned int low_probability_multiplier_q15 = 39321;
static unsigned int minimum_multiplier_q15 = 16384;
static unsigned int maximum_multiplier_q15 = 49152;
static unsigned int high_probability_threshold_q15 = 24576;
static unsigned int medium_probability_threshold_q15 = 12288;
static unsigned long minimum_scan_units = 1;
static unsigned long maximum_extra_scan_units = 4096;
static enum parp_scan_budget_mode scan_budget_mode =
	PARP_SCAN_BUDGET_OBSERVE;
static u64 scan_budget_apply_domain;

module_param(foreground_multiplier_q15, uint, 0644);
module_param(high_probability_multiplier_q15, uint, 0644);
module_param(medium_probability_multiplier_q15, uint, 0644);
module_param(low_probability_multiplier_q15, uint, 0644);
module_param(minimum_multiplier_q15, uint, 0644);
module_param(maximum_multiplier_q15, uint, 0644);
module_param(high_probability_threshold_q15, uint, 0644);
module_param(medium_probability_threshold_q15, uint, 0644);
module_param(minimum_scan_units, ulong, 0644);
module_param(maximum_extra_scan_units, ulong, 0644);

struct parp_scan_guard {
	u64 domain_id;
	u32 generation;
	u32 failures;
	bool tripped;
};

static DEFINE_SPINLOCK(parp_scan_guard_lock);
static struct parp_scan_guard parp_scan_guards[PARP_MAX_DOMAINS];

struct parp_scan_budget_stats parp_scan_budget_stats;

enum parp_scan_budget_mode parp_get_scan_budget_mode(void)
{
	return READ_ONCE(scan_budget_mode);
}

int parp_set_scan_budget_mode(enum parp_scan_budget_mode mode)
{
	if (mode < PARP_SCAN_BUDGET_DISABLED ||
	    mode > PARP_SCAN_BUDGET_APPLY)
		return -EINVAL;
	if (mode == PARP_SCAN_BUDGET_APPLY &&
	    !parp_get_scan_budget_apply_domain())
		return -EPERM;
	WRITE_ONCE(scan_budget_mode, mode);
	return 0;
}

u64 parp_get_scan_budget_apply_domain(void)
{
	return READ_ONCE(scan_budget_apply_domain);
}

int parp_set_scan_budget_apply_domain(u64 domain_id)
{
	if (parp_get_scan_budget_mode() == PARP_SCAN_BUDGET_APPLY)
		return -EBUSY;
	WRITE_ONCE(scan_budget_apply_domain, domain_id);
	return 0;
}

static bool parp_scan_config_valid(void)
{
	return minimum_multiplier_q15 <= maximum_multiplier_q15 &&
	       maximum_multiplier_q15 <= 2 * PARP_Q15_SCALE &&
	       foreground_multiplier_q15 <= maximum_multiplier_q15 &&
	       high_probability_multiplier_q15 <= maximum_multiplier_q15 &&
	       medium_probability_multiplier_q15 <= maximum_multiplier_q15 &&
	       low_probability_multiplier_q15 <= maximum_multiplier_q15 &&
	       high_probability_threshold_q15 <= PARP_Q15_ONE &&
	       medium_probability_threshold_q15 <=
			high_probability_threshold_q15;
}

static u64 parp_scale_scan_units(u64 native, u32 multiplier)
{
	u64 scaled;
	u64 remainder;

	/* mul_u64_u32_div() requires its quotient to fit in u64. */
	if (multiplier > PARP_Q15_SCALE &&
	    native > mul_u64_u32_div(U64_MAX, PARP_Q15_SCALE, multiplier))
		return U64_MAX; /* #lzx */
	scaled = mul_u64_u32_div(native, multiplier, PARP_Q15_SCALE);
	remainder = ((native & (PARP_Q15_SCALE - 1)) * multiplier) &
		(PARP_Q15_SCALE - 1);
	if (remainder >= PARP_Q15_SCALE / 2 && scaled != U64_MAX)
		scaled++;
	if (multiplier > PARP_Q15_SCALE && scaled < native)
		return U64_MAX;
	return scaled;
}

static void parp_native_decision(const struct parp_scan_budget_input *input,
				 struct parp_scan_budget_decision *decision,
				 enum parp_scan_budget_reason reason)
{
	decision->valid = false;
	decision->native_nr_to_scan = input->native_nr_to_scan;
	decision->proposed_nr_to_scan = input->native_nr_to_scan;
	decision->applied_nr_to_scan = input->native_nr_to_scan;
	decision->multiplier_q15 = PARP_Q15_SCALE;
	decision->reason = reason;
}

static enum parp_scan_budget_reason
parp_validate_scan_input(const struct parp_scan_budget_input *input)
{
	if (parp_get_scan_budget_mode() == PARP_SCAN_BUDGET_DISABLED)
		return PARP_SCAN_REASON_DISABLED;
	if (input->reclaim_scope != PARP_RECLAIM_SCOPE_TARGET_MEMCG &&
	    input->reclaim_scope != PARP_RECLAIM_SCOPE_PROACTIVE_MEMCG)
		return PARP_SCAN_REASON_NOT_TARGET_MEMCG;
	if (!(input->flags & PARP_SCAN_INPUT_CIRCUIT_OK))
		return PARP_SCAN_REASON_CIRCUIT_BREAKER;
	if (!(input->flags & PARP_SCAN_INPUT_BIND_PRESENT))
		return PARP_SCAN_REASON_NO_BIND;
	if (!(input->flags & PARP_SCAN_INPUT_BIND_VALID) ||
	    input->now_ns >= input->bind_expiry_ns)
		return PARP_SCAN_REASON_STALE_BIND;
	if (!(input->flags & PARP_SCAN_INPUT_PRIOR_PRESENT))
		return PARP_SCAN_REASON_NO_PRIOR;
	if (!(input->flags & PARP_SCAN_INPUT_PRIOR_VALID) ||
	    input->now_ns >= input->prediction_expiry_ns)
		return PARP_SCAN_REASON_EXPIRED_PRIOR;
	if (!(input->flags & PARP_SCAN_INPUT_GENERATION_VALID))
		return PARP_SCAN_REASON_STALE_GENERATION;
	if (!(input->flags & PARP_SCAN_INPUT_MODEL_COMPATIBLE))
		return PARP_SCAN_REASON_MODEL_VERSION;
	if (!parp_scan_config_valid())
		return PARP_SCAN_REASON_CIRCUIT_BREAKER;
	return PARP_SCAN_REASON_NATIVE;
}

int parp_compute_scan_budget(const struct parp_scan_budget_input *input,
			     struct parp_scan_budget_decision *decision)
{
	enum parp_scan_budget_reason invalid;
	u32 multiplier;
	u64 max_units;

	if (!input || !decision)
		return -EINVAL;
	memset(decision, 0, sizeof(*decision));
	invalid = parp_validate_scan_input(input);
	if (invalid != PARP_SCAN_REASON_NATIVE) {
		parp_native_decision(input, decision, invalid);
		return 0;
	}
	if (!input->native_nr_to_scan) {
		parp_native_decision(input, decision, PARP_SCAN_REASON_NATIVE);
		decision->valid = true;
		return 0;
	}
	if (input->pressure == PARP_PRESSURE_EMERGENCY) {
		parp_native_decision(input, decision,
				     PARP_SCAN_REASON_PRESSURE_BYPASS);
		return 0;
	}
	if (input->foreground) {
		multiplier = foreground_multiplier_q15;
		decision->reason = PARP_SCAN_REASON_FOREGROUND;
	} else if (input->app_use_score_q15 >=
		   high_probability_threshold_q15) {
		multiplier = high_probability_multiplier_q15;
		decision->reason = PARP_SCAN_REASON_HIGH_PRIOR;
	} else if (input->app_use_score_q15 >=
		   medium_probability_threshold_q15) {
		multiplier = medium_probability_multiplier_q15;
		decision->reason = PARP_SCAN_REASON_MEDIUM_PRIOR;
	} else {
		multiplier = low_probability_multiplier_q15;
		decision->reason = PARP_SCAN_REASON_LOW_PRIOR;
	}
	multiplier = clamp(multiplier, minimum_multiplier_q15,
			   maximum_multiplier_q15);
	if (input->pressure == PARP_PRESSURE_ELEVATED)
		multiplier = (multiplier + PARP_Q15_SCALE + 1) / 2;
	else if (input->pressure == PARP_PRESSURE_HIGH)
		multiplier = max(multiplier, 3 * PARP_Q15_SCALE / 4);
	decision->valid = true;
	decision->native_nr_to_scan = input->native_nr_to_scan;
	decision->multiplier_q15 = multiplier;
	decision->confidence_q15 = PARP_Q15_ONE;
	decision->proposed_nr_to_scan =
		parp_scale_scan_units(input->native_nr_to_scan, multiplier);
	if (decision->proposed_nr_to_scan < minimum_scan_units) {
		decision->proposed_nr_to_scan =
			min_t(u64, input->native_nr_to_scan, minimum_scan_units);
		decision->reason_flags |= BIT(PARP_SCAN_REASON_CLAMP_MIN);
	}
	if (check_add_overflow(input->native_nr_to_scan,
			       (u64)maximum_extra_scan_units, &max_units))
		max_units = U64_MAX;
	if (decision->proposed_nr_to_scan > max_units) {
		decision->proposed_nr_to_scan = max_units;
		decision->reason_flags |= BIT(PARP_SCAN_REASON_CLAMP_MAX);
	}
	decision->applied_nr_to_scan = input->native_nr_to_scan;
	if (parp_get_scan_budget_mode() == PARP_SCAN_BUDGET_APPLY) {
		if (input->domain_id == parp_get_scan_budget_apply_domain())
			decision->applied_nr_to_scan =
				decision->proposed_nr_to_scan;
		else
			decision->reason_flags |=
				BIT(PARP_SCAN_REASON_APPLY_DOMAIN);
	}
	return 0;
}

static struct parp_scan_guard *parp_scan_guard_find(u64 domain_id)
{
	struct parp_scan_guard *free = NULL;
	unsigned int i;

	for (i = 0; i < ARRAY_SIZE(parp_scan_guards); i++) {
		if (parp_scan_guards[i].domain_id == domain_id)
			return &parp_scan_guards[i];
		if (!parp_scan_guards[i].domain_id && !free)
			free = &parp_scan_guards[i];
	}
	return free;
}

bool parp_scan_budget_guard(u64 domain_id, u32 generation, bool valid)
{
	struct parp_scan_guard *guard;
	bool allowed = false;

	if (!domain_id)
		return false;
	spin_lock(&parp_scan_guard_lock);
	guard = parp_scan_guard_find(domain_id);
	if (!guard)
		goto out;
	if (!guard->domain_id)
		guard->domain_id = domain_id;
	if (valid && generation > guard->generation) {
		guard->generation = generation;
		guard->failures = 0;
		guard->tripped = false;
	} else if (!valid && !guard->tripped) {
		guard->failures++;
		if (guard->failures >= PARP_SCAN_GUARD_FAILURES) {
			guard->tripped = true;
			atomic64_inc(&parp_scan_budget_stats.circuit_breaker_count);
		}
	}
	allowed = !guard->tripped;
out:
	spin_unlock(&parp_scan_guard_lock);
	return allowed;
}

void parp_scan_budget_guard_clear(u64 domain_id)
{
	struct parp_scan_guard *guard;

	spin_lock(&parp_scan_guard_lock);
	guard = parp_scan_guard_find(domain_id);
	if (guard && guard->domain_id == domain_id)
		memset(guard, 0, sizeof(*guard));
	spin_unlock(&parp_scan_guard_lock);
}

unsigned int parp_scan_budget_guard_snapshot(
		struct parp_scan_guard_view *views, unsigned int max_views)
{
	unsigned int i, count = 0;

	spin_lock(&parp_scan_guard_lock);
	for (i = 0; i < ARRAY_SIZE(parp_scan_guards) && count < max_views; i++) {
		if (!parp_scan_guards[i].domain_id)
			continue;
		views[count++] = (struct parp_scan_guard_view) {
			.domain_id = parp_scan_guards[i].domain_id,
			.generation = parp_scan_guards[i].generation,
			.failures = parp_scan_guards[i].failures,
			.tripped = parp_scan_guards[i].tripped,
		};
	}
	spin_unlock(&parp_scan_guard_lock);
	return count;
}

void parp_scan_budget_guard_reset_all_for_test(void)
{
	spin_lock(&parp_scan_guard_lock);
	memset(parp_scan_guards, 0, sizeof(parp_scan_guards));
	spin_unlock(&parp_scan_guard_lock);
}

void parp_scan_budget_account(const struct parp_scan_budget_input *input,
			      const struct parp_scan_budget_decision *decision)
{
	atomic64_inc(&parp_scan_budget_stats.scan_budget_queries);
	if (input->reclaim_scope == PARP_RECLAIM_SCOPE_TARGET_MEMCG ||
	    input->reclaim_scope == PARP_RECLAIM_SCOPE_PROACTIVE_MEMCG)
		atomic64_inc(&parp_scan_budget_stats.target_memcg_queries);
	else if (input->reclaim_scope == PARP_RECLAIM_SCOPE_GLOBAL_KSWAPD)
		atomic64_inc(&parp_scan_budget_stats.global_kswapd_bypass);
	else if (input->reclaim_scope == PARP_RECLAIM_SCOPE_GLOBAL_DIRECT)
		atomic64_inc(&parp_scan_budget_stats.global_direct_bypass);
	else
		atomic64_inc(&parp_scan_budget_stats.unknown_scope_bypass);
	switch (decision->reason) {
	case PARP_SCAN_REASON_FOREGROUND:
		atomic64_inc(&parp_scan_budget_stats.foreground_decisions);
		break;
	case PARP_SCAN_REASON_HIGH_PRIOR:
		atomic64_inc(&parp_scan_budget_stats.high_prior_decisions);
		break;
	case PARP_SCAN_REASON_MEDIUM_PRIOR:
		atomic64_inc(&parp_scan_budget_stats.medium_prior_decisions);
		break;
	case PARP_SCAN_REASON_LOW_PRIOR:
		atomic64_inc(&parp_scan_budget_stats.low_prior_decisions);
		break;
	case PARP_SCAN_REASON_NO_BIND:
		atomic64_inc(&parp_scan_budget_stats.no_appbind);
		break;
	case PARP_SCAN_REASON_STALE_BIND:
		atomic64_inc(&parp_scan_budget_stats.stale_bind);
		break;
	case PARP_SCAN_REASON_NO_PRIOR:
		atomic64_inc(&parp_scan_budget_stats.no_prior);
		break;
	case PARP_SCAN_REASON_EXPIRED_PRIOR:
		atomic64_inc(&parp_scan_budget_stats.expired_prior);
		break;
	case PARP_SCAN_REASON_STALE_GENERATION:
		atomic64_inc(&parp_scan_budget_stats.stale_generation);
		break;
	case PARP_SCAN_REASON_MODEL_VERSION:
		atomic64_inc(&parp_scan_budget_stats.model_version_mismatch);
		break;
	case PARP_SCAN_REASON_PRESSURE_BYPASS:
		atomic64_inc(&parp_scan_budget_stats.pressure_bypass);
		break;
	default:
		break;
	}
	if (decision->reason_flags & BIT(PARP_SCAN_REASON_CLAMP_MIN))
		atomic64_inc(&parp_scan_budget_stats.clamp_min);
	if (decision->reason_flags & BIT(PARP_SCAN_REASON_CLAMP_MAX))
		atomic64_inc(&parp_scan_budget_stats.clamp_max);
	if (decision->reason_flags & BIT(PARP_SCAN_REASON_APPLY_DOMAIN))
		atomic64_inc(&parp_scan_budget_stats.apply_domain_bypass);
	if (parp_get_scan_budget_mode() == PARP_SCAN_BUDGET_APPLY &&
	    decision->applied_nr_to_scan != decision->native_nr_to_scan)
		atomic64_inc(&parp_scan_budget_stats.apply_count);
	else
		atomic64_inc(&parp_scan_budget_stats.observe_count);
	atomic64_add(decision->native_nr_to_scan,
		     &parp_scan_budget_stats.native_units_total);
	atomic64_add(decision->proposed_nr_to_scan,
		     &parp_scan_budget_stats.proposed_units_total);
	atomic64_add(decision->applied_nr_to_scan,
		     &parp_scan_budget_stats.applied_units_total);
}
