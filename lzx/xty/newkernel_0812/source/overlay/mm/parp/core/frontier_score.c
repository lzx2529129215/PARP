// SPDX-License-Identifier: GPL-2.0
/* Fixed-cost PARP scoring at the native MGLRU reclaim frontier. */
#include <linux/math64.h>
#include <linux/memcontrol.h>
#include <linux/mm.h>
#include <linux/mm_inline.h>
#include <linux/mutex.h>
#include <linux/page_ext.h>
#include <linux/parp.h>
#include <linux/rcupdate.h>
#include <linux/random.h>
#include <linux/siphash.h>
#include <linux/swap.h>
#include <trace/events/parp.h>

#include "../internal.h"
#include "../adapter/adapter.h"

#define PARP_FRONTIER_Q15_ONE	32767U
#define PARP_FRONTIER_BATCH_PAGES 128UL
#define PARP_FRONTIER_EPOCH_PAGES 512UL
#define PARP_FRONTIER_PRESSURE_PRIORITY 2
#define PARP_FRONTIER_TTL_NS	(50ULL * NSEC_PER_MSEC)

struct parp_folio_ext {
	atomic64_t last_access_ns;
	atomic64_t generation_enter_ns;
	atomic64_t last_selected_epoch;
	atomic_t previous_interval_ms;
	atomic_t access_ema_q8;
	atomic_t reuse_interval_ema_ms;
	atomic_t inactive_count;
	atomic_t generation;
};

struct parp_frontier_stats parp_frontier_stats;
DEFINE_STATIC_KEY_FALSE(parp_frontier_enabled);

static DEFINE_MUTEX(parp_frontier_mode_lock);
static enum parp_frontier_mode parp_frontier_mode;
static atomic64_t parp_frontier_batch_id = ATOMIC64_INIT(0);
static siphash_key_t parp_frontier_cookie_key __read_mostly;

#define PARP_COMMON_EDGES { \
	{ 10, 100, 500, 2000, 10000 }, \
	{ 10, 100, 500, 2000, 10000 }, \
	{ 8, 32, 96, 160, 224 }, \
	{ 10, 100, 500, 2000, 10000 }, \
	{ 0, 1, 2, 4, 8 }, \
	{ 0, 1, 2, 3, 5 }, \
	{ 10, 100, 500, 2000, 10000 }, \
	{ 4096, 8192, 16384, 24576, 30000 } \
}

static const struct parp_frontier_model parp_frontier_models[] = {
	{
		.app_id = 0,
		.model_version = PARP_FRONTIER_MODEL_VERSION,
		.feature_schema_version = PARP_FRONTIER_SCHEMA_VERSION,
		.threshold = 96,
		.bin_edges = PARP_COMMON_EDGES,
		.weights = {
			{ 38, 29, 18, 7, -9, -21 },
			{ 24, 18, 11, 3, -8, -17 },
			{ -18, -7, 3, 15, 26, 37 },
			{ 22, 16, 9, 1, -9, -19 },
			{ 22, 13, 5, -5, -15, -25 },
			{ 19, 11, 3, -5, -14, -23 },
			{ 17, 11, 5, -2, -10, -19 },
			{ -22, -13, -4, 8, 21, 34 },
		},
	},
	{
		.app_id = 1,
		.model_version = PARP_FRONTIER_MODEL_VERSION,
		.feature_schema_version = PARP_FRONTIER_SCHEMA_VERSION,
		.threshold = 94,
		.bin_edges = PARP_COMMON_EDGES,
		.weights = {
			{ 42, 32, 20, 8, -8, -22 },
			{ 26, 20, 12, 4, -8, -18 },
			{ -20, -8, 4, 16, 28, 40 },
			{ 24, 18, 10, 2, -8, -18 },
			{ 24, 14, 6, -4, -14, -26 },
			{ 20, 12, 4, -4, -12, -20 },
			{ 18, 12, 6, 0, -8, -16 },
			{ -24, -14, -4, 8, 20, 32 },
		},
	},
	{
		.app_id = 2,
		.model_version = PARP_FRONTIER_MODEL_VERSION,
		.feature_schema_version = PARP_FRONTIER_SCHEMA_VERSION,
		.threshold = 100,
		.bin_edges = PARP_COMMON_EDGES,
		.weights = {
			{ 36, 28, 18, 8, -6, -18 },
			{ 20, 16, 10, 4, -6, -14 },
			{ -16, -6, 4, 14, 24, 34 },
			{ 18, 14, 8, 2, -8, -16 },
			{ 20, 12, 4, -4, -12, -22 },
			{ 18, 10, 2, -6, -14, -22 },
			{ 16, 10, 4, -2, -10, -18 },
			{ -30, -18, -6, 10, 26, 42 },
		},
	},
	{
		.app_id = 3,
		.model_version = PARP_FRONTIER_MODEL_VERSION,
		.feature_schema_version = PARP_FRONTIER_SCHEMA_VERSION,
		.threshold = 88,
		.bin_edges = PARP_COMMON_EDGES,
		.weights = {
			{ 46, 34, 20, 6, -12, -28 },
			{ 30, 22, 12, 2, -10, -22 },
			{ -22, -10, 2, 16, 30, 44 },
			{ 28, 20, 10, 0, -12, -24 },
			{ 26, 16, 6, -6, -18, -30 },
			{ 22, 12, 2, -8, -18, -28 },
			{ 20, 12, 4, -4, -14, -24 },
			{ -18, -10, -2, 6, 16, 26 },
		},
	},
};

static const struct parp_frontier_model __rcu *parp_active_models[] = {
	RCU_INITIALIZER(&parp_frontier_models[0]),
	RCU_INITIALIZER(&parp_frontier_models[1]),
	RCU_INITIALIZER(&parp_frontier_models[2]),
	RCU_INITIALIZER(&parp_frontier_models[3]),
};

static bool __init parp_frontier_page_ext_needed(void)
{
	return true;
}

static int __init parp_frontier_init(void)
{
	get_random_bytes(&parp_frontier_cookie_key,
			 sizeof(parp_frontier_cookie_key));
	return 0;
}
subsys_initcall(parp_frontier_init);

struct page_ext_operations parp_frontier_page_ext_ops = {
	.size = sizeof(struct parp_folio_ext),
	.need = parp_frontier_page_ext_needed,
};

static struct parp_folio_ext *parp_folio_ext_get(struct folio *folio,
						  struct page_ext **page_ext)
{
	*page_ext = page_ext_get(&folio->page);
	if (!*page_ext)
		return NULL;
	return page_ext_data(*page_ext, &parp_frontier_page_ext_ops);
}

static u32 parp_u64_to_ms(u64 ns)
{
	return min_t(u64, div_u64(ns, NSEC_PER_MSEC), U32_MAX);
}

static u32 parp_atomic_saturating_inc(atomic_t *value)
{
	int old;
	int new;

	do {
		old = atomic_read(value);
		if (old == INT_MAX)
			return old;
		new = old + 1;
	} while (atomic_cmpxchg(value, old, new) != old);
	return new;
}

static u32 parp_atomic_ema(atomic_t *value, u32 sample)
{
	int old;
	int new;

	sample = min_t(u32, sample, INT_MAX);
	do {
		old = max(atomic_read(value), 0);
		new = old ? old + ((int)sample - old) / 4 : sample;
	} while (atomic_cmpxchg(value, old, new) != old);
	return new;
}

static void parp_frontier_account_score_time(u64 duration_ns)
{
	s64 old = atomic64_read(&parp_frontier_stats.score_time_ns_max);

	atomic64_add(duration_ns, &parp_frontier_stats.score_time_ns_total);
	while (duration_ns > old) {
		s64 observed = atomic64_cmpxchg(
			&parp_frontier_stats.score_time_ns_max, old, duration_ns);

		if (observed == old)
			break;
		old = observed;
	}
}

void __parp_frontier_note_access(struct folio *folio, int generation)
{
	struct parp_folio_ext *ext;
	struct page_ext *page_ext;
	u64 now = ktime_get_mono_fast_ns();
	u64 previous;
	u32 interval;

	ext = parp_folio_ext_get(folio, &page_ext);
	if (!ext)
		return;
	previous = atomic64_xchg(&ext->last_access_ns, now);
	if (previous && now > previous) {
		interval = parp_u64_to_ms(now - previous);
		atomic_set(&ext->previous_interval_ms, interval);
		parp_atomic_ema(&ext->reuse_interval_ema_ms, interval);
	}
	parp_atomic_ema(&ext->access_ema_q8, 256);
	atomic_set(&ext->inactive_count, 0);
	if (atomic_xchg(&ext->generation, generation) != generation ||
	    !atomic64_read(&ext->generation_enter_ns))
		atomic64_set(&ext->generation_enter_ns, now);
	page_ext_put(page_ext);
}

static const struct parp_frontier_model *parp_frontier_model(u32 app_id)
{
	if (app_id > 3)
		app_id = PARP_FRONTIER_GENERIC_APP;
	return rcu_dereference(parp_active_models[app_id]);
}

static bool parp_frontier_model_valid(const struct parp_frontier_model *model)
{
	return model &&
	       model->model_version == PARP_FRONTIER_MODEL_VERSION &&
	       model->feature_schema_version == PARP_FRONTIER_SCHEMA_VERSION;
}

static s32 parp_frontier_score(const struct parp_frontier_model *model,
			       const s64 values[PARP_FRONTIER_FEATURES])
{
	s32 score = 0;
	int feature;

	for (feature = 0; feature < PARP_FRONTIER_FEATURES; feature++) {
		int bin = 0;

		while (bin < PARP_FRONTIER_BINS - 1 &&
		       values[feature] > model->bin_edges[feature][bin])
			bin++;
		score += model->weights[feature][bin];
	}
	return score;
}

s32 parp_frontier_score_values(u32 app_id,
		const s64 values[PARP_FRONTIER_FEATURES], s32 *threshold)
{
	const struct parp_frontier_model *model;
	s32 score = S32_MIN;

	rcu_read_lock();
	model = parp_frontier_model(app_id);
	if (parp_frontier_model_valid(model)) {
		*threshold = model->threshold;
		score = parp_frontier_score(model, values);
	}
	rcu_read_unlock();
	return score;
}

static unsigned long parp_effective_pages(unsigned long pages, u32 eta_q15)
{
	return min_t(u64, mul_u64_u32_div(pages, eta_q15,
					  PARP_FRONTIER_Q15_ONE), ULONG_MAX);
}

bool parp_frontier_select(const unsigned long *capacities,
		unsigned int nr_capacities, unsigned long demand, u32 eta_q15,
		unsigned int *frontier, unsigned long *headroom)
{
	unsigned long cumulative = 0;
	unsigned int i;

	if (!demand || !eta_q15 || eta_q15 > PARP_FRONTIER_Q15_ONE)
		return false;
	for (i = 0; i < nr_capacities; i++) {
		unsigned long effective =
			parp_effective_pages(capacities[i], eta_q15);
		unsigned long remaining = demand - min(demand, cumulative);

		if (effective >= remaining) {
			*frontier = i;
			*headroom = effective - remaining;
			return true;
		}
		cumulative += min(effective, ULONG_MAX - cumulative);
	}
	return false;
}

unsigned long parp_frontier_budget_min(unsigned long headroom,
		unsigned long app_remaining, unsigned long batch_remaining,
		unsigned long epoch_remaining)
{
	return min(min(headroom, app_remaining),
		   min(batch_remaining, epoch_remaining));
}

bool parp_frontier_context_valid(u64 now_ns, u64 valid_until_ns,
		unsigned long source_seq, unsigned long expected_source_seq)
{
	return valid_until_ns && now_ns < valid_until_ns &&
	       source_seq == expected_source_seq;
}

static bool parp_choose_frontier(struct lru_gen_folio *lrugen, int type,
				 int reclaim_idx, unsigned long demand,
				 u32 eta_q15, unsigned long *frontier,
				 unsigned long *headroom)
{
	unsigned long capacities[MAX_NR_GENS] = { 0 };
	unsigned long last = lrugen->max_seq - MIN_NR_GENS;
	unsigned long seq;
	unsigned int selected;
	unsigned int nr = 0;

	for (seq = lrugen->min_seq[type]; seq <= last; seq++) {
		int gen = lru_gen_from_seq(seq);
		int zone;

		for (zone = 0; zone <= reclaim_idx; zone++)
			capacities[nr] += min_t(unsigned long,
				max(READ_ONCE(lrugen->nr_pages[gen][type][zone]), 0L),
				ULONG_MAX - capacities[nr]);
		nr++;
	}
	if (!parp_frontier_select(capacities, nr, demand, eta_q15, &selected,
				  headroom))
		return false;
	*frontier = lrugen->min_seq[type] + selected;
	return true;
}

static void parp_frontier_prepare_reason(enum parp_frontier_reason reason)
{
	switch (reason) {
	case PARP_FRONTIER_PRESSURE_BYPASS:
		atomic64_inc(&parp_frontier_stats.pressure_bypass);
		break;
	case PARP_FRONTIER_NO_APP_CONTEXT:
		atomic64_inc(&parp_frontier_stats.no_context);
		break;
	case PARP_FRONTIER_NO_EFFICIENCY:
		atomic64_inc(&parp_frontier_stats.no_efficiency);
		break;
	case PARP_FRONTIER_NO_CAPACITY:
		atomic64_inc(&parp_frontier_stats.no_capacity);
		break;
	default:
		break;
	}
}

void __parp_frontier_prepare(struct parp_frontier_scan_ctx *ctx,
		struct lruvec *lruvec, bool target_memcg, int type,
		int reclaim_priority, int reclaim_idx,
		unsigned long remaining_demand)
{
	struct lru_gen_folio *lrugen = &lruvec->lrugen;
	typeof(lrugen->parp_frontier[0]) *state =
		&lrugen->parp_frontier[type];
	struct parp_app_context app;
	const struct parp_frontier_model *model;
	u64 now = ktime_get_mono_fast_ns();
	u64 domain_id;

	memset(ctx, 0, sizeof(*ctx));
	ctx->mode = READ_ONCE(parp_frontier_mode);
	ctx->type = type;
	ctx->batch_id = atomic64_inc_return(&parp_frontier_batch_id);
	ctx->reason = PARP_FRONTIER_DISABLED;
	atomic64_inc(&parp_frontier_stats.prepare);
	if (!target_memcg) {
		ctx->reason = PARP_FRONTIER_NOT_TARGET_MEMCG;
		return;
	}
	domain_id = parp_memcg_domain_id(lruvec_memcg(lruvec));
	ctx->domain_id = domain_id;
	ctx->nid = lruvec_pgdat(lruvec)->node_id;
	if (reclaim_priority <= PARP_FRONTIER_PRESSURE_PRIORITY) {
		ctx->reason = PARP_FRONTIER_PRESSURE_BYPASS;
		goto bypass;
	}
	if (!remaining_demand) {
		ctx->reason = PARP_FRONTIER_NO_DEMAND;
		return;
	}
	if (!domain_id || !parp_context_lookup(domain_id, now, &app)) {
		ctx->reason = PARP_FRONTIER_NO_APP_CONTEXT;
		goto bypass;
	}
	if (!state->efficiency_samples) {
		ctx->reason = PARP_FRONTIER_NO_EFFICIENCY;
		goto bypass;
	}
	rcu_read_lock();
	model = parp_frontier_model(app.app_id);
	if (!parp_frontier_model_valid(model)) {
		rcu_read_unlock();
		ctx->reason = PARP_FRONTIER_MODEL_INVALID;
		return;
	}
	ctx->threshold = model->threshold;
	ctx->model_version = model->model_version;
	rcu_read_unlock();
	ctx->app_id = app.app_id;
	ctx->app_reentry_q15 = app.app_prior_q15;
	ctx->foreground_epoch_id = app.foreground_epoch_id;
	ctx->source_seq = lrugen->min_seq[type];
	ctx->source_generation = lru_gen_from_seq(ctx->source_seq);
	ctx->remaining_demand = remaining_demand;
	ctx->efficiency_q15 = state->efficiency_q15;
	if (!parp_choose_frontier(lrugen, type, reclaim_idx, remaining_demand,
				  ctx->efficiency_q15, &ctx->frontier_seq,
				  &ctx->frontier_headroom)) {
		ctx->reason = PARP_FRONTIER_NO_CAPACITY;
		goto bypass;
	}
	if (!state->epoch_id || state->source_seq != ctx->source_seq ||
	    state->foreground_epoch_id != app.foreground_epoch_id ||
	    state->model_version != ctx->model_version) {
		state->source_seq = ctx->source_seq;
		state->foreground_epoch_id = app.foreground_epoch_id;
		state->model_version = ctx->model_version;
		state->epoch_pages = 0;
		if (!++state->epoch_id)
			state->epoch_id++;
	}
	ctx->epoch_id = state->epoch_id;
	ctx->valid_until_ns = now + PARP_FRONTIER_TTL_NS;
	state->valid_until_ns = ctx->valid_until_ns;
	state->frontier_seq = ctx->frontier_seq;
	state->frontier_headroom = ctx->frontier_headroom;
	ctx->batch_budget = PARP_FRONTIER_BATCH_PAGES;
	ctx->epoch_budget = PARP_FRONTIER_EPOCH_PAGES;
	ctx->app_budget = max_t(unsigned long, 1,
		parp_effective_pages(ctx->batch_budget, app.app_prior_q15));
	ctx->reason = PARP_FRONTIER_SCORE;
	ctx->valid = true;
	return;
bypass:
	parp_frontier_prepare_reason(ctx->reason);
}

static void parp_frontier_emit(const struct parp_frontier_scan_ctx *ctx,
		struct folio *folio, unsigned long pages, s32 score,
		u64 score_duration_ns, enum parp_frontier_reason reason,
		bool would_promote)
{
	struct parp_frontier_trace event = {
		.timestamp_ns = ktime_get_mono_fast_ns(),
		.domain_id = ctx->domain_id,
		.epoch_id = ctx->epoch_id,
		.batch_id = ctx->batch_id,
		.foreground_epoch_id = ctx->foreground_epoch_id,
		.folio_cookie = siphash_2u64(folio_pfn(folio), ctx->epoch_id,
					     &parp_frontier_cookie_key),
		.source_seq = ctx->source_seq,
		.frontier_seq = ctx->frontier_seq,
		.remaining_demand = ctx->remaining_demand,
		.frontier_headroom = ctx->frontier_headroom,
		.app_budget = ctx->app_budget,
		.batch_budget = ctx->batch_budget,
		.epoch_budget = ctx->epoch_budget,
		.folio_pages = pages,
		.score_duration_ns = score_duration_ns,
		.score = score,
		.threshold = ctx->threshold,
		.model_version = ctx->model_version,
		.efficiency_q15 = ctx->efficiency_q15,
		.app_id = ctx->app_id,
		.nid = ctx->nid,
		.source_generation = ctx->source_generation,
		.feature_schema_version = PARP_FRONTIER_SCHEMA_VERSION,
		.page_type = ctx->type,
		.mode = ctx->mode,
		.reason = reason,
		.would_promote = would_promote,
		.applied = false,
	};

	if (trace_parp_frontier_decision_enabled()) {
		atomic64_inc(&parp_frontier_stats.trace_events);
		trace_parp_frontier_decision(&event);
	}
}

static bool parp_frontier_features(struct parp_frontier_scan_ctx *ctx,
		struct lruvec *lruvec, struct folio *folio,
		s64 values[PARP_FRONTIER_FEATURES],
		struct parp_folio_ext **result, struct page_ext **page_ext)
{
	struct parp_folio_ext *ext;
	u64 entered;
	u64 last_access;
	u64 now = ktime_get_mono_fast_ns();
	u32 inactive;

	ext = parp_folio_ext_get(folio, page_ext);
	if (!ext)
		return false;
	last_access = atomic64_read(&ext->last_access_ns);
	entered = atomic64_read(&ext->generation_enter_ns);
	if (!last_access || !entered || now < last_access || now < entered) {
		page_ext_put(*page_ext);
		return false;
	}
	inactive = parp_atomic_saturating_inc(&ext->inactive_count);
	values[0] = parp_u64_to_ms(now - last_access);
	values[1] = max(atomic_read(&ext->previous_interval_ms), 0);
	values[2] = parp_atomic_ema(&ext->access_ema_q8, 0);
	values[3] = max(atomic_read(&ext->reuse_interval_ema_ms), 0);
	values[4] = inactive;
	values[5] = min_t(unsigned long,
		lruvec->lrugen.max_seq - ctx->source_seq, S32_MAX);
	values[6] = parp_u64_to_ms(now - entered);
	values[7] = ctx->app_reentry_q15;
	*result = ext;
	return true;
}

void __parp_frontier_consider(struct parp_frontier_scan_ctx *ctx,
		struct lruvec *lruvec, struct folio *folio)
{
	struct lru_gen_folio *lrugen = &lruvec->lrugen;
	typeof(lrugen->parp_frontier[0]) *state =
		&lrugen->parp_frontier[ctx->type];
	const struct parp_frontier_model *model;
	struct parp_folio_ext *ext;
	struct page_ext *page_ext;
	s64 values[PARP_FRONTIER_FEATURES];
	unsigned long pages = folio_nr_pages(folio);
	unsigned long allowed;
	s32 score = 0;
	u64 score_duration_ns = 0;
	u64 score_started_ns;
	u64 now;
	enum parp_frontier_reason reason;
	bool would_promote = false;

	atomic64_inc(&parp_frontier_stats.candidates);
	if (!ctx->valid) {
		atomic64_add(pages, &parp_frontier_stats.native_bypass_pages);
		if (ctx->reason == PARP_FRONTIER_NO_CAPACITY)
			atomic64_add(pages,
				     &parp_frontier_stats.frontier_bypass_pages);
		else if (ctx->reason == PARP_FRONTIER_MODEL_INVALID)
			atomic64_add(pages,
				     &parp_frontier_stats.invalid_model_pages);
		parp_frontier_emit(ctx, folio, pages, 0, 0, ctx->reason, false);
		return;
	}
	now = ktime_get_mono_fast_ns();
	if (!parp_frontier_context_valid(now, ctx->valid_until_ns,
					 ctx->source_seq,
					 lrugen->min_seq[ctx->type])) {
		atomic64_inc(&parp_frontier_stats.expired);
		atomic64_add(pages, &parp_frontier_stats.native_bypass_pages);
		atomic64_add(pages, &parp_frontier_stats.frontier_bypass_pages);
		parp_frontier_emit(ctx, folio, pages, score, 0,
				   PARP_FRONTIER_EXPIRED, false);
		return;
	}
	if (ctx->source_seq != ctx->frontier_seq) {
		atomic64_inc(&parp_frontier_stats.not_frontier);
		atomic64_add(pages, &parp_frontier_stats.native_bypass_pages);
		atomic64_add(pages, &parp_frontier_stats.frontier_bypass_pages);
		parp_frontier_emit(ctx, folio, pages, score, 0,
				   PARP_FRONTIER_NOT_FRONTIER, false);
		return;
	}
	if (!parp_frontier_features(ctx, lruvec, folio, values, &ext,
				    &page_ext)) {
		atomic64_inc(&parp_frontier_stats.metadata_missing);
		atomic64_add(pages, &parp_frontier_stats.native_bypass_pages);
		parp_frontier_emit(ctx, folio, pages, score, 0,
				   PARP_FRONTIER_METADATA_MISSING, false);
		return;
	}
	rcu_read_lock();
	score_started_ns = ktime_get_mono_fast_ns();
	model = parp_frontier_model(ctx->app_id);
	if (!parp_frontier_model_valid(model)) {
		atomic64_add(pages, &parp_frontier_stats.invalid_model_pages);
		atomic64_add(pages, &parp_frontier_stats.native_bypass_pages);
		reason = PARP_FRONTIER_MODEL_INVALID;
		goto unlock;
	}
	score = parp_frontier_score(model, values);
	score_duration_ns = ktime_get_mono_fast_ns() - score_started_ns;
	parp_frontier_account_score_time(score_duration_ns);
	atomic64_inc(&parp_frontier_stats.scores);
	if (score < model->threshold) {
		atomic64_inc(&parp_frontier_stats.below_threshold);
		atomic64_add(pages, &parp_frontier_stats.native_bypass_pages);
		reason = PARP_FRONTIER_BELOW_THRESHOLD;
		goto unlock;
	}
	if (atomic64_read(&ext->last_selected_epoch) == ctx->epoch_id) {
		atomic64_inc(&parp_frontier_stats.repeat_epoch);
		atomic64_add(pages, &parp_frontier_stats.native_bypass_pages);
		reason = PARP_FRONTIER_REPEAT_EPOCH;
		goto unlock;
	}
	allowed = parp_frontier_budget_min(ctx->frontier_headroom,
		ctx->app_budget - min(ctx->app_used, ctx->app_budget),
		ctx->batch_budget - min(ctx->batch_used, ctx->batch_budget),
		ctx->epoch_budget - min(state->epoch_pages, ctx->epoch_budget));
	if (pages > allowed) {
		atomic64_inc(&parp_frontier_stats.budget_reject);
		atomic64_add(pages, &parp_frontier_stats.native_bypass_pages);
		atomic64_add(pages, &parp_frontier_stats.budget_bypass_pages);
		reason = PARP_FRONTIER_BUDGET;
		goto unlock;
	}
	atomic64_set(&ext->last_selected_epoch, ctx->epoch_id);
	ctx->frontier_headroom -= pages;
	ctx->batch_used += pages;
	ctx->app_used += pages;
	state->epoch_pages += pages;
	atomic64_inc(&parp_frontier_stats.would_promote);
	atomic64_add(pages, &parp_frontier_stats.would_promote_pages);
	would_promote = true;
	reason = PARP_FRONTIER_SCORE;
unlock:
	rcu_read_unlock();
	page_ext_put(page_ext);
	parp_frontier_emit(ctx, folio, pages, score, score_duration_ns, reason,
			   would_promote);
}

void parp_frontier_feedback(struct lruvec *lruvec, int type,
		unsigned long isolated, unsigned long reclaimed)
{
	typeof(lruvec->lrugen.parp_frontier[0]) *state =
		&lruvec->lrugen.parp_frontier[type];
	u32 sample;

	if (!static_branch_unlikely(&parp_frontier_enabled) || !isolated)
		return;
	sample = min_t(u64, mul_u64_u32_div(reclaimed,
			PARP_FRONTIER_Q15_ONE, isolated), PARP_FRONTIER_Q15_ONE);
	if (!state->efficiency_samples)
		state->efficiency_q15 = sample;
	else
		state->efficiency_q15 += ((s32)sample -
					 state->efficiency_q15) / 4;
	state->efficiency_samples++;
	atomic64_inc(&parp_frontier_stats.feedback_samples);
}

enum parp_frontier_mode parp_frontier_get_mode(void)
{
	return READ_ONCE(parp_frontier_mode);
}

int parp_frontier_set_mode(enum parp_frontier_mode mode)
{
	enum parp_frontier_mode old;

	if (mode == PARP_FRONTIER_APPLY)
		return -EOPNOTSUPP;
	if (mode != PARP_FRONTIER_OFF && mode != PARP_FRONTIER_SHADOW)
		return -EINVAL;
	mutex_lock(&parp_frontier_mode_lock);
	old = parp_frontier_mode;
	if (old == mode)
		goto out;
	WRITE_ONCE(parp_frontier_mode, mode);
	if (mode == PARP_FRONTIER_SHADOW)
		static_branch_enable(&parp_frontier_enabled);
	else
		static_branch_disable(&parp_frontier_enabled);
out:
	mutex_unlock(&parp_frontier_mode_lock);
	return 0;
}

ssize_t parp_frontier_format_stats(char *buf, size_t size)
{
	return scnprintf(buf, size,
		"mode %u\nprepare %lld\ncandidates %lld\nscores %lld\n"
		"would_promote %lld\nwould_promote_pages %lld\napplied %lld\n"
		"actual_promote_pages %lld\nnative_bypass_pages %lld\n"
		"budget_bypass_pages %lld\nfrontier_bypass_pages %lld\n"
		"invalid_model_pages %lld\nmetadata_missing %lld\n"
		"no_context %lld\nno_efficiency %lld\nno_capacity %lld\n"
		"not_frontier %lld\nbelow_threshold %lld\nbudget_reject %lld\n"
		"repeat_epoch %lld\npressure_bypass %lld\nexpired %lld\n"
		"feedback_samples %lld\ntrace_events %lld\n"
		"score_time_ns_total %lld\nscore_time_ns_max %lld\n"
		"apply_compiled 0\n",
		parp_frontier_get_mode(),
		atomic64_read(&parp_frontier_stats.prepare),
		atomic64_read(&parp_frontier_stats.candidates),
		atomic64_read(&parp_frontier_stats.scores),
		atomic64_read(&parp_frontier_stats.would_promote),
		atomic64_read(&parp_frontier_stats.would_promote_pages),
		atomic64_read(&parp_frontier_stats.applied),
		atomic64_read(&parp_frontier_stats.actual_promote_pages),
		atomic64_read(&parp_frontier_stats.native_bypass_pages),
		atomic64_read(&parp_frontier_stats.budget_bypass_pages),
		atomic64_read(&parp_frontier_stats.frontier_bypass_pages),
		atomic64_read(&parp_frontier_stats.invalid_model_pages),
		atomic64_read(&parp_frontier_stats.metadata_missing),
		atomic64_read(&parp_frontier_stats.no_context),
		atomic64_read(&parp_frontier_stats.no_efficiency),
		atomic64_read(&parp_frontier_stats.no_capacity),
		atomic64_read(&parp_frontier_stats.not_frontier),
		atomic64_read(&parp_frontier_stats.below_threshold),
		atomic64_read(&parp_frontier_stats.budget_reject),
		atomic64_read(&parp_frontier_stats.repeat_epoch),
		atomic64_read(&parp_frontier_stats.pressure_bypass),
		atomic64_read(&parp_frontier_stats.expired),
		atomic64_read(&parp_frontier_stats.feedback_samples),
		atomic64_read(&parp_frontier_stats.trace_events),
		atomic64_read(&parp_frontier_stats.score_time_ns_total),
		atomic64_read(&parp_frontier_stats.score_time_ns_max));
}
