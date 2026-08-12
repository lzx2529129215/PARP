// SPDX-License-Identifier: GPL-2.0
/* Bounded GLOBAL reuse scoring at the native MGLRU tier-protection gate. */
#include <linux/bitfield.h>
#include <linux/build_bug.h>
#include <linux/kernel.h>
#include <linux/math64.h>
#include <linux/memcontrol.h>
#include <linux/mm.h>
#include <linux/mm_inline.h>
#include <linux/mutex.h>
#include <linux/page_ext.h>
#include <linux/parp.h>
#include <linux/random.h>
#include <linux/rcupdate.h>
#include <linux/siphash.h>
#include <linux/string.h>
#include <trace/events/parp.h>

#define PARP_TIER_SCHEMA_VERSION	1U
#define PARP_TIER_STATE_TRIES		4
#define PARP_TIER_LATENCY_BUCKETS	32
#define PARP_TIER_EPOCH_MASK		GENMASK(11, 0)
#define PARP_TIER_DOWNGRADE_SHIFT	12
#define PARP_TIER_STATE_EPOCH_SHIFT	24
#define PARP_TIER_STATE_EPOCH_MASK	GENMASK(31, 24)

#define PARP_STATE_ACCESS_MASK		GENMASK(7, 0)
#define PARP_STATE_CANDIDATE_SHIFT	8
#define PARP_STATE_CANDIDATE_MASK	GENMASK(13, 8)
#define PARP_STATE_GENERATION_SHIFT	14
#define PARP_STATE_GENERATION_MASK	GENMASK(15, 14)
#define PARP_STATE_ACTION_SHIFT		16
#define PARP_STATE_ACTION_MASK		GENMASK(18, 16)
#define PARP_STATE_INITIALIZED		BIT(19)
#define PARP_STATE_UNCERTAIN		BIT(20)
#define PARP_STATE_SEQ_SHIFT		21
#define PARP_STATE_SEQ_MASK		GENMASK(31, 21)

#define PARP_DEFAULT_UPGRADE_BATCH_PAGES	2UL
#define PARP_DEFAULT_UPGRADE_EPOCH_PAGES	8UL
#define PARP_DEFAULT_UPGRADE_RATIO	100U /* 1% in 1/10000 units */
#define PARP_DEFAULT_DOWNGRADE_BATCH_PAGES	1UL
#define PARP_DEFAULT_DOWNGRADE_EPOCH_PAGES	4UL
#define PARP_DEFAULT_DOWNGRADE_RATIO	50U /* 0.5% in 1/10000 units */
#define PARP_SEVERE_PRESSURE_PRIORITY	2
#define PARP_NO_PROGRESS_LIMIT		3

/* Exactly 24 bytes per base page; score and native tier are never persisted. */
struct parp_reuse_page_ext {
	u32 last_access_ms;
	u16 previous_interval_ms;
	u16 reuse_interval_ema_ms;
	u32 generation_enter_ms;
	u32 lifetime_epoch;
	u32 state;
	u32 decision_epochs;
};

struct parp_global_reuse_model {
	u32 model_version;
	u32 feature_schema_version;
	s32 bias;
	s64 bin_edges[PARP_TIER_FEATURES][PARP_TIER_BINS - 1];
	s16 weights[PARP_TIER_FEATURES][PARP_TIER_BINS];
};

struct parp_tier_runtime_config {
	u32 sequence;
	struct parp_tier_policy policy;
	unsigned long upgrade_batch_pages;
	unsigned long upgrade_epoch_pages;
	unsigned long downgrade_batch_pages;
	unsigned long downgrade_epoch_pages;
	u32 upgrade_ratio_permyriad;
	u32 downgrade_ratio_permyriad;
	u64 experiment_id;
	u64 session_id;
};

struct parp_tier_stats {
	atomic64_t candidates;
	atomic64_t candidate_pages;
	atomic64_t scores;
	atomic64_t metadata_missing;
	atomic64_t state_unstable;
	atomic64_t model_invalid;
	atomic64_t action_pages[5];
	atomic64_t bypass[PARP_TIER_BYPASS_NR];
	atomic64_t access_events[PARP_ACCESS_EVENT_NR];
	atomic64_t outcomes[4];
	atomic64_t policy_promotions;
	atomic64_t native_tier_promotions;
	atomic64_t native_generation_moves;
	atomic64_t score_time_ns_total;
	atomic64_t score_time_ns_max;
	atomic64_t decision_time_ns_total;
	atomic64_t decision_time_ns_max;
	atomic64_t lock_time_ns_total;
	atomic64_t lock_time_ns_max;
	atomic64_t score_hist[PARP_TIER_LATENCY_BUCKETS];
	atomic64_t decision_hist[PARP_TIER_LATENCY_BUCKETS];
	atomic64_t lock_hist[PARP_TIER_LATENCY_BUCKETS];
	atomic64_t trace_decisions;
	atomic64_t trace_accesses;
	atomic64_t trace_outcomes;
	atomic64_t trace_batches;
	atomic64_t trace_locks;
};

static const struct parp_global_reuse_model parp_global_model = {
	.model_version = PARP_TIER_MODEL_VERSION,
	.feature_schema_version = PARP_TIER_SCHEMA_VERSION,
	.bias = 0,
	.bin_edges = {
		{ 10, 100, 500, 2000, 10000 },
		{ 10, 100, 500, 2000, 10000 },
		{ 10, 100, 500, 2000, 10000 },
		{ 0, 1, 2, 4, 8 },
		{ 10, 100, 500, 2000, 10000 },
		{ 8, 32, 96, 160, 224 },
	},
	.weights = {
		{ 64, 48, 24, 0, -32, -64 },
		{ 32, 24, 12, 0, -12, -24 },
		{ 32, 24, 12, 0, -12, -24 },
		{ 24, 12, 0, -12, -24, -36 },
		{ 16, 12, 8, 0, -8, -16 },
		{ -24, -12, 0, 12, 24, 36 },
	},
};

static struct parp_tier_runtime_config parp_tier_config = {
	.sequence = 0,
	.policy = {
		.cold_threshold = -48,
		.hot_threshold_1 = 48,
		.hot_threshold_2 = 96,
		.hot_threshold_3 = 144,
		.max_upgrade_tiers = 2,
		.max_downgrade_tiers = 1,
		.require_two_cold = true,
	},
	.upgrade_batch_pages = PARP_DEFAULT_UPGRADE_BATCH_PAGES,
	.upgrade_epoch_pages = PARP_DEFAULT_UPGRADE_EPOCH_PAGES,
	.downgrade_batch_pages = PARP_DEFAULT_DOWNGRADE_BATCH_PAGES,
	.downgrade_epoch_pages = PARP_DEFAULT_DOWNGRADE_EPOCH_PAGES,
	.upgrade_ratio_permyriad = PARP_DEFAULT_UPGRADE_RATIO,
	.downgrade_ratio_permyriad = PARP_DEFAULT_DOWNGRADE_RATIO,
};

static struct parp_tier_stats parp_tier_stats;
DEFINE_STATIC_KEY_FALSE(parp_effective_tier_enabled);
DEFINE_STATIC_KEY_FALSE(parp_effective_tier_lock_observe);
static DEFINE_MUTEX(parp_tier_mode_lock);
static DEFINE_MUTEX(parp_tier_config_lock);
static DEFINE_MUTEX(parp_tier_lock_observe_lock);
static enum parp_effective_tier_mode parp_tier_mode;
static atomic64_t parp_tier_batch_id = ATOMIC64_INIT(0);
static atomic64_t parp_tier_trace_sequence = ATOMIC64_INIT(0);
static siphash_key_t parp_tier_cookie_key __read_mostly;
static u8 parp_tier_state_epoch __read_mostly;
static atomic_t parp_tier_state_fault = ATOMIC_INIT(0);
static bool parp_tier_metadata_requested;
static bool parp_tier_metadata_ready; /* #lzx */

static int __init parp_effective_tier_reserve_setup(char *str)
{
	bool reserve;

	/* A malformed or omitted request must retain the no-allocation default. */
	if (!str || kstrtobool(str, &reserve))
		return 0;
	WRITE_ONCE(parp_tier_metadata_requested, reserve);
	return 0;
} /* #lzx */
early_param("parp_effective_tier_reserve", parp_effective_tier_reserve_setup);

static bool __init parp_effective_tier_page_ext_needed(void)
{
	return READ_ONCE(parp_tier_metadata_requested); /* #lzx */
}

static void __init parp_effective_tier_page_ext_init(void)
{
	WRITE_ONCE(parp_tier_metadata_ready, true);
} /* #lzx */

struct page_ext_operations parp_effective_tier_page_ext_ops = {
	.size = sizeof(struct parp_reuse_page_ext),
	.need = parp_effective_tier_page_ext_needed,
	.init = parp_effective_tier_page_ext_init, /* #lzx */
};

static int __init parp_effective_tier_init(void)
{
	BUILD_BUG_ON(sizeof(struct parp_reuse_page_ext) != 24);
	get_random_bytes(&parp_tier_cookie_key, sizeof(parp_tier_cookie_key));
	return 0;
}
subsys_initcall(parp_effective_tier_init);

static struct parp_reuse_page_ext *parp_reuse_ext_get(
		struct folio *folio, struct page_ext **page_ext)
{
	*page_ext = page_ext_get(&folio->page);
	if (!*page_ext)
		return NULL;
	return page_ext_data(*page_ext, &parp_effective_tier_page_ext_ops);
}

static struct parp_reuse_page_ext *parp_reuse_page_ext_get(
		struct page *page, struct page_ext **page_ext)
{
	*page_ext = page_ext_get(page);
	if (!*page_ext)
		return NULL;
	return page_ext_data(*page_ext, &parp_effective_tier_page_ext_ops);
}

static u32 parp_now_ms(void)
{
	return (u32)div_u64(ktime_get_mono_fast_ns(), NSEC_PER_MSEC);
}

static u32 parp_state_seq(u32 state)
{
	return FIELD_GET(PARP_STATE_SEQ_MASK, state);
}

static bool parp_state_write_begin(struct parp_reuse_page_ext *ext,
		u32 *stable_state)
{
	int attempt;

	for (attempt = 0; attempt < PARP_TIER_STATE_TRIES; attempt++) {
		u32 old = READ_ONCE(ext->state);
		u32 seq = parp_state_seq(old);
		u32 locked;

		if (seq & 1) {
			cpu_relax();
			continue;
		}
		locked = (old & ~PARP_STATE_SEQ_MASK) |
			FIELD_PREP(PARP_STATE_SEQ_MASK,
				   (seq + 1) & FIELD_MAX(PARP_STATE_SEQ_MASK));
		if (cmpxchg(&ext->state, old, locked) == old) {
			*stable_state = old;
			return true;
		}
		cpu_relax();
	}
	return false;
}

static void parp_state_write_end(struct parp_reuse_page_ext *ext,
		u32 old_state, u32 new_state)
{
	u32 seq = (parp_state_seq(old_state) + 2) &
		FIELD_MAX(PARP_STATE_SEQ_MASK);

	new_state &= ~PARP_STATE_SEQ_MASK;
	new_state |= FIELD_PREP(PARP_STATE_SEQ_MASK, seq);
	/* Publish payload updates before making the sequence even. */
	smp_store_release(&ext->state, new_state);
}

static void parp_state_mark_uncertain(struct parp_reuse_page_ext *ext)
{
	int attempt;

	for (attempt = 0; attempt < PARP_TIER_STATE_TRIES; attempt++) {
		u32 old = READ_ONCE(ext->state);

		if (parp_state_seq(old) & 1) {
			cpu_relax();
			continue;
		}
		if (old & PARP_STATE_UNCERTAIN)
			return;
		if (cmpxchg(&ext->state, old,
			    old | PARP_STATE_UNCERTAIN) == old)
			return;
		cpu_relax();
	}
}

static u16 parp_sat_u16(u32 value)
{
	return min_t(u32, value, U16_MAX);
}

static u16 parp_interval_ema(u16 old, u32 sample)
{
	s32 delta;
	u32 next;

	sample = min_t(u32, sample, U16_MAX);
	if (!old)
		return sample;
	delta = (s32)sample - old;
	next = old + delta / 4;
	return parp_sat_u16(next);
}

static u8 parp_access_ema_on_access(u8 old)
{
	u8 delta;

	if (!old)
		return U8_MAX;
	delta = (U8_MAX - old) / 4;
	return min_t(u16, old + max_t(u8, delta, 1), U8_MAX);
}

static u8 parp_access_ema_on_candidate(u8 old)
{
	return old - old / 4;
}

static u64 parp_folio_cookie(struct folio *folio, u32 lifetime_epoch)
{
	return siphash_2u64(folio_pfn(folio), lifetime_epoch,
			    &parp_tier_cookie_key);
}

static bool parp_effective_tier_identity(struct folio *folio,
		u32 *lifetime_epoch, u64 *cookie)
{
	struct parp_reuse_page_ext *ext;
	struct page_ext *page_ext;

	ext = parp_reuse_ext_get(folio, &page_ext);
	if (!ext) {
		*lifetime_epoch = 0;
		*cookie = 0;
		return false;
	}
	*lifetime_epoch = READ_ONCE(ext->lifetime_epoch);
	*cookie = parp_folio_cookie(folio, *lifetime_epoch);
	page_ext_put(page_ext);
	return true;
}

u64 parp_effective_tier_cookie(struct folio *folio)
{
	u32 lifetime_epoch;
	u64 cookie;

	parp_effective_tier_identity(folio, &lifetime_epoch, &cookie);
	return cookie;
}

static void parp_account_max(atomic64_t *maximum, u64 value)
{
	s64 old = atomic64_read(maximum);
	int attempt;

	for (attempt = 0; attempt < PARP_TIER_STATE_TRIES && value > old;
	     attempt++) {
		s64 seen = atomic64_cmpxchg(maximum, old, value);

		if (seen == old)
			break;
		old = seen;
	}
}

static void parp_account_latency(atomic64_t *total, atomic64_t *maximum,
		atomic64_t histogram[PARP_TIER_LATENCY_BUCKETS], u64 value)
{
	unsigned int bin = value ? fls64(value) - 1 : 0;

	bin = min_t(unsigned int, bin, PARP_TIER_LATENCY_BUCKETS - 1);
	atomic64_add(value, total);
	parp_account_max(maximum, value);
	atomic64_inc(&histogram[bin]);
}

static bool parp_runtime_config_read(struct parp_tier_runtime_config *result)
{
	u32 before = READ_ONCE(parp_tier_config.sequence);

	if (before & 1)
		return false;
	/* Order the config payload read after the initial sequence sample. */
	smp_rmb();
	*result = parp_tier_config;
	/* Complete payload reads before validating the sequence again. */
	smp_rmb();
	return before == READ_ONCE(parp_tier_config.sequence) &&
	       !(result->sequence & 1);
}

bool parp_access_event_is_real(enum parp_access_event event)
{
	return event == PARP_ACCESS_PTE_YOUNG ||
	       event == PARP_ACCESS_MARK_ACCESSED ||
	       event == PARP_ACCESS_FD_REFERENCE;
}

u32 parp_effective_tier_elapsed_ms(u32 now, u32 then, bool *valid)
{
	u32 elapsed = now - then;

	*valid = elapsed <= S32_MAX;
	return elapsed;
}

size_t parp_effective_tier_metadata_size(void)
{
	return sizeof(struct parp_reuse_page_ext);
}

void parp_effective_tier_page_alloc(struct page *page, unsigned int order)
{
	unsigned long nr_pages = 1UL << order;
	unsigned long i;

	for (i = 0; i < nr_pages; i++) {
		struct parp_reuse_page_ext *ext;
		struct page_ext *page_ext;
		u32 lifetime;

		ext = parp_reuse_page_ext_get(page + i, &page_ext);
		if (!ext)
			continue;
		lifetime = READ_ONCE(ext->lifetime_epoch) + 1;
		if (!lifetime)
			lifetime = 1;
		WRITE_ONCE(ext->last_access_ms, 0);
		WRITE_ONCE(ext->previous_interval_ms, 0);
		WRITE_ONCE(ext->reuse_interval_ema_ms, 0);
		WRITE_ONCE(ext->generation_enter_ms, 0);
		WRITE_ONCE(ext->decision_epochs, 0);
		WRITE_ONCE(ext->lifetime_epoch, lifetime);
		/* Publish the reset payload before exposing an even state. */
		smp_store_release(&ext->state, 0);
		page_ext_put(page_ext);
	}
}

void __parp_effective_tier_mglru_state_change(void)
{
	/* Require an explicit OFF->active transition before model reuse. */
	atomic_set(&parp_tier_state_fault, 1);
}

bool parp_effective_tier_candidate_access_trace_eligible(bool real_access,
		u8 candidate_count)
{
	return real_access && candidate_count; /* #lzx */
} /* #lzx */

static void parp_emit_access(struct folio *folio,
		struct parp_reuse_page_ext *ext, enum parp_access_event event,
		bool real_access, u8 candidate_count) /* #lzx */
{
	struct parp_effective_tier_access_trace trace;
	struct parp_tier_runtime_config config = { };

	if (!trace_parp_effective_tier_access_enabled() ||
	    !parp_effective_tier_candidate_access_trace_eligible(real_access,
			candidate_count)) /* #lzx */
		return;
	trace = (struct parp_effective_tier_access_trace) {
		.timestamp_ns = ktime_get_mono_fast_ns(),
		.folio_cookie = parp_folio_cookie(folio,
				READ_ONCE(ext->lifetime_epoch)),
		.lifetime_epoch = READ_ONCE(ext->lifetime_epoch),
		.generation = folio_lru_gen(folio),
		.page_type = folio_is_file_lru(folio),
		.event = event,
		.candidate_count = candidate_count, /* #lzx */
		.real_access = real_access,
	};
	if (parp_runtime_config_read(&config)) {
		trace.experiment_id = config.experiment_id;
		trace.session_id = config.session_id;
	}

	trace.trace_sequence = atomic64_inc_return(&parp_tier_trace_sequence);
	atomic64_inc(&parp_tier_stats.trace_accesses);
	trace_parp_effective_tier_access(&trace);
}

void __parp_effective_tier_note_access(struct folio *folio,
		enum parp_access_event event)
{
	struct parp_reuse_page_ext *ext;
	struct page_ext *page_ext;
	u32 old_state;
	u32 new_state;
	u32 decision_epochs;
	u32 now = parp_now_ms();
	u8 candidate_count = 0; /* #lzx */
	bool real_access = parp_access_event_is_real(event);

	if (event >= PARP_ACCESS_EVENT_NR)
		return;
	atomic64_inc(&parp_tier_stats.access_events[event]);
	ext = parp_reuse_ext_get(folio, &page_ext);
	if (!ext)
		return;
	if (!parp_state_write_begin(ext, &old_state)) {
		atomic64_inc(&parp_tier_stats.state_unstable);
		parp_state_mark_uncertain(ext);
		atomic_set(&parp_tier_state_fault, 1);
		goto emit;
	}
	new_state = old_state;
	decision_epochs = READ_ONCE(ext->decision_epochs);
	if (real_access) {
		bool initialized = old_state & PARP_STATE_INITIALIZED;
		u8 access_ema = FIELD_GET(PARP_STATE_ACCESS_MASK, old_state);
		candidate_count = FIELD_GET(PARP_STATE_CANDIDATE_MASK,
			old_state); /* #lzx */
		u32 previous = READ_ONCE(ext->last_access_ms);
		u8 current_epoch = READ_ONCE(parp_tier_state_epoch);
		bool new_run = FIELD_GET(PARP_TIER_STATE_EPOCH_MASK,
					 decision_epochs) != current_epoch;
		bool valid = false;
		u32 interval = 0;

		if (initialized &&
		    FIELD_GET(PARP_TIER_STATE_EPOCH_MASK,
			      decision_epochs) == READ_ONCE(parp_tier_state_epoch))
			interval = parp_effective_tier_elapsed_ms(now, previous,
							       &valid);
		if (valid) {
			WRITE_ONCE(ext->previous_interval_ms,
				   parp_sat_u16(interval));
			WRITE_ONCE(ext->reuse_interval_ema_ms,
				   parp_interval_ema(
					READ_ONCE(ext->reuse_interval_ema_ms),
					interval));
		} else {
			WRITE_ONCE(ext->previous_interval_ms, 0);
			WRITE_ONCE(ext->reuse_interval_ema_ms, 0);
			access_ema = 0;
		}
		WRITE_ONCE(ext->last_access_ms, now);
		new_state &= ~(PARP_STATE_ACCESS_MASK |
			       PARP_STATE_CANDIDATE_MASK |
			       PARP_STATE_UNCERTAIN);
		new_state |= parp_access_ema_on_access(access_ema);
		new_state |= PARP_STATE_INITIALIZED;
		if (new_run) {
			decision_epochs = FIELD_PREP(
				PARP_TIER_STATE_EPOCH_MASK, current_epoch);
			new_state &= ~PARP_STATE_ACTION_MASK;
		}
		WRITE_ONCE(ext->decision_epochs, decision_epochs);
	} else if (old_state & PARP_STATE_INITIALIZED) {
		new_state |= PARP_STATE_UNCERTAIN;
	}
	parp_state_write_end(ext, old_state, new_state);
emit:
	parp_emit_access(folio, ext, event, real_access, candidate_count); /* #lzx */
	page_ext_put(page_ext);
}

void __parp_effective_tier_note_move(struct folio *folio,
		enum parp_access_event event, int generation)
{
	struct parp_reuse_page_ext *ext;
	struct page_ext *page_ext;
	u32 old_state;
	u32 new_state;
	u8 old_generation;

	if (event < PARP_NATIVE_TIER_PROMOTION ||
	    event >= PARP_ACCESS_EVENT_NR || generation < 0 ||
	    generation > PARP_MAX_TIER)
		return;
	atomic64_inc(&parp_tier_stats.access_events[event]);
	if (event == PARP_POLICY_PROMOTION)
		atomic64_inc(&parp_tier_stats.policy_promotions);
	else if (event == PARP_NATIVE_TIER_PROMOTION)
		atomic64_inc(&parp_tier_stats.native_tier_promotions);
	else if (event == PARP_NATIVE_GENERATION_MOVE)
		atomic64_inc(&parp_tier_stats.native_generation_moves);
	ext = parp_reuse_ext_get(folio, &page_ext);
	if (!ext)
		return;
	if (!parp_state_write_begin(ext, &old_state)) {
		atomic64_inc(&parp_tier_stats.state_unstable);
		parp_state_mark_uncertain(ext);
		atomic_set(&parp_tier_state_fault, 1);
		goto emit;
	}
	old_generation = FIELD_GET(PARP_STATE_GENERATION_MASK, old_state);
	new_state = old_state & ~PARP_STATE_GENERATION_MASK;
	new_state |= FIELD_PREP(PARP_STATE_GENERATION_MASK, generation);
	if (!READ_ONCE(ext->generation_enter_ms) || old_generation != generation)
		WRITE_ONCE(ext->generation_enter_ms, parp_now_ms());
	parp_state_write_end(ext, old_state, new_state);
emit:
	parp_emit_access(folio, ext, event, false, 0); /* #lzx */
	page_ext_put(page_ext);
}

bool parp_effective_tier_state_snapshot(struct folio *folio,
		struct parp_tier_state_snapshot *snapshot)
{
	struct parp_reuse_page_ext *ext;
	struct page_ext *page_ext;
	u32 first;
	u32 second;
	u32 epochs;

	ext = parp_reuse_ext_get(folio, &page_ext);
	if (!ext)
		return false;
	/* Pair with the writer's release of an even sequence. */
	first = smp_load_acquire(&ext->state);
	if (parp_state_seq(first) & 1)
		goto unstable;
	snapshot->last_access_ms = READ_ONCE(ext->last_access_ms);
	snapshot->previous_interval_ms =
		READ_ONCE(ext->previous_interval_ms);
	snapshot->reuse_interval_ema_ms =
		READ_ONCE(ext->reuse_interval_ema_ms);
	snapshot->generation_enter_ms =
		READ_ONCE(ext->generation_enter_ms);
	snapshot->lifetime_epoch = READ_ONCE(ext->lifetime_epoch);
	epochs = READ_ONCE(ext->decision_epochs);
	/* Complete payload reads before validating the sequence again. */
	smp_rmb();
	second = READ_ONCE(ext->state);
	if (first != second || parp_state_seq(second) & 1)
		goto unstable;
	snapshot->last_upgrade_epoch = epochs & PARP_TIER_EPOCH_MASK;
	snapshot->last_downgrade_epoch =
		(epochs >> PARP_TIER_DOWNGRADE_SHIFT) & PARP_TIER_EPOCH_MASK;
	snapshot->state_epoch = FIELD_GET(PARP_TIER_STATE_EPOCH_MASK, epochs);
	snapshot->state_sequence = parp_state_seq(first);
	snapshot->access_ema_q8 = FIELD_GET(PARP_STATE_ACCESS_MASK, first);
	snapshot->consecutive_candidates =
		FIELD_GET(PARP_STATE_CANDIDATE_MASK, first);
	snapshot->generation = FIELD_GET(PARP_STATE_GENERATION_MASK, first);
	snapshot->pending_action = FIELD_GET(PARP_STATE_ACTION_MASK, first);
	snapshot->uncertain = first & PARP_STATE_UNCERTAIN;
	page_ext_put(page_ext);
	return first & PARP_STATE_INITIALIZED;
unstable:
	page_ext_put(page_ext);
	return false;
}

bool parp_effective_tier_score_values(
		const s64 values[PARP_TIER_FEATURES], s32 *score)
{
	s64 total = parp_global_model.bias;
	int feature;

	if (parp_global_model.model_version != PARP_TIER_MODEL_VERSION ||
	    parp_global_model.feature_schema_version !=
			PARP_TIER_SCHEMA_VERSION)
		return false;
	for (feature = 0; feature < PARP_TIER_FEATURES; feature++) {
		int bin = 0;

		if (values[feature] == S64_MIN)
			return false;
		while (bin < PARP_TIER_BINS - 1 &&
		       values[feature] >
			parp_global_model.bin_edges[feature][bin])
			bin++;
		total += parp_global_model.weights[feature][bin];
	}
	if (total < S32_MIN || total > S32_MAX)
		return false;
	*score = total;
	return true;
}

static bool parp_tier_policy_valid(const struct parp_tier_policy *policy)
{
	if (!policy || policy->max_upgrade_tiers < 1 ||
	    policy->max_upgrade_tiers > 3 ||
	    policy->max_downgrade_tiers != 1 ||
	    policy->cold_threshold >= policy->hot_threshold_1 ||
	    policy->hot_threshold_1 >= policy->hot_threshold_2 ||
	    policy->hot_threshold_2 >= policy->hot_threshold_3)
		return false;
	return true;
}

u8 parp_effective_tier_pressure_level(int reclaim_priority,
		bool no_progress, unsigned long nr_to_reclaim,
		unsigned long nr_reclaimed)
{
	/*
	 * Only scan_control snapshots and per-lruvec progress reach this helper.
	 * It runs while lru_lock is held, so PSI and cgroup state deliberately do
	 * not participate.  CRITICAL is exactly the existing Native-bypass domain.
	 */
	if (no_progress || reclaim_priority <= PARP_SEVERE_PRESSURE_PRIORITY)
		return PARP_PRESSURE_CRITICAL;
	if (reclaim_priority <= 4)
		return PARP_PRESSURE_HIGH;
	if (reclaim_priority <= 8 || nr_to_reclaim > nr_reclaimed)
		return PARP_PRESSURE_MEDIUM;
	return PARP_PRESSURE_LOW;
} /* #lzx */

static enum parp_tier_reclaim_context
parp_tier_reclaim_context(bool is_kswapd, bool is_memcg_reclaim,
			  bool proactive_reclaim)
{
	if (is_kswapd)
		return PARP_TIER_RECLAIM_KSWAPD;
	if (proactive_reclaim)
		return PARP_TIER_RECLAIM_PROACTIVE_MEMCG;
	if (is_memcg_reclaim)
		return PARP_TIER_RECLAIM_MEMCG;
	return PARP_TIER_RECLAIM_DIRECT;
} /* #lzx */

static void parp_pressure_policy_scales(enum parp_pressure_level level,
					u16 *up_q8, u16 *down_q8)
{
	/* Phase-F engineering matrix: trace-only and never an APPLY default. */
	switch (level) {
	case PARP_PRESSURE_LOW:
		*up_q8 = 256;
		*down_q8 = 128;
		break;
	case PARP_PRESSURE_MEDIUM:
		*up_q8 = 192;
		*down_q8 = 256;
		break;
	case PARP_PRESSURE_HIGH:
		*up_q8 = 64;
		*down_q8 = 256;
		break;
	case PARP_PRESSURE_CRITICAL:
	default:
		*up_q8 = 0;
		*down_q8 = 0;
		break;
	}
} /* #lzx */

static s32 parp_pressure_scale_delta_q8(s32 delta_q8, u16 scale_q8)
{
	return div_s64((s64)delta_q8 * scale_q8, PARP_TIER_SCALE);
} /* #lzx */

s32 parp_effective_tier_pressure_delta_q8(s32 delta_q8,
		u8 pressure_level_kernel)
{
	u16 up_q8;
	u16 down_q8;

	parp_pressure_policy_scales(pressure_level_kernel, &up_q8, &down_q8);
	return parp_pressure_scale_delta_q8(delta_q8,
		delta_q8 < 0 ? down_q8 : up_q8);
} /* #lzx */

static void parp_effective_tier_pressure_counterfactuals(
		const struct parp_tier_scan_ctx *ctx,
		struct parp_tier_decision *decision)
{
	s32 fixed_delta = 0;
	s32 pressure_delta;

	if (decision->model_valid && !decision->special_native_protect)
		fixed_delta = decision->raw_delta_tier_q8;
	/* Never synthesize a cold downgrade outside the ordinary boundary. */
	if (fixed_delta < 0 && decision->native_tier !=
		decision->native_tier_idx + 1)
		fixed_delta = 0;
	pressure_delta = parp_effective_tier_pressure_delta_q8(fixed_delta,
		ctx->pressure_level_kernel);

	decision->pressure_level_kernel = ctx->pressure_level_kernel;
	decision->reclaim_context = ctx->reclaim_context;
	decision->pressure_bypass_reason = ctx->pressure_bypass_reason;
	decision->pressure_policy_version = PARP_TIER_PRESSURE_POLICY_VERSION;
	decision->fixed_delta_q8 = fixed_delta;
	decision->binary_bypass_delta_q8 =
		ctx->pressure_level_kernel == PARP_PRESSURE_CRITICAL ? 0 :
		fixed_delta;
	decision->pressure_aware_delta_q8 = pressure_delta;
	decision->fixed_effective_protect = parp_effective_tier_q8(
		decision->native_tier, fixed_delta) >
		decision->native_tier_idx * PARP_TIER_SCALE;
	decision->pressure_aware_effective_protect = parp_effective_tier_q8(
		decision->native_tier, pressure_delta) >
		decision->native_tier_idx * PARP_TIER_SCALE;
} /* #lzx */

s32 parp_score_to_delta_q8(s32 score,
		const struct parp_tier_policy *policy)
{
	if (!parp_tier_policy_valid(policy))
		return 0;
	if (score <= policy->cold_threshold)
		return -PARP_TIER_SCALE;
	if (score >= policy->hot_threshold_3 &&
	    policy->max_upgrade_tiers == 3)
		return 3 * PARP_TIER_SCALE;
	if (score >= policy->hot_threshold_2)
		return min_t(u8, policy->max_upgrade_tiers, 2) *
			PARP_TIER_SCALE;
	if (score >= policy->hot_threshold_1)
		return PARP_TIER_SCALE;
	return 0;
}

s32 parp_effective_tier_q8(int native_tier, s32 delta_tier_q8)
{
	s64 effective;

	if (native_tier < 0 || native_tier > PARP_MAX_TIER)
		return 0;
	effective = (s64)native_tier * PARP_TIER_SCALE + delta_tier_q8;
	return clamp_t(s64, effective, 0,
		       PARP_MAX_TIER * PARP_TIER_SCALE);
}

void parp_effective_tier_classify(s32 score, bool model_valid,
		int native_tier, int tier_idx, bool special_native_protect,
		const struct parp_tier_policy *policy,
		struct parp_tier_decision *decision)
{
	bool effective_model_valid = model_valid &&
		parp_tier_policy_valid(policy);
	s32 delta = effective_model_valid ?
		parp_score_to_delta_q8(score, policy) : 0;

	memset(decision, 0, sizeof(*decision));
	decision->evaluated = true;
	decision->model_valid = effective_model_valid;
	decision->reuse_score = effective_model_valid ? score : 0;
	if (!effective_model_valid)
		decision->rank_score_bin = PARP_RANK_SCORE_INVALID;
	else if (score <= policy->cold_threshold)
		decision->rank_score_bin = PARP_RANK_SCORE_COLD;
	else if (score < policy->hot_threshold_1)
		decision->rank_score_bin = PARP_RANK_SCORE_NEUTRAL;
	else if (score < policy->hot_threshold_2)
		decision->rank_score_bin = PARP_RANK_SCORE_HOT_1;
	else if (score < policy->hot_threshold_3)
		decision->rank_score_bin = PARP_RANK_SCORE_HOT_2;
	else
		decision->rank_score_bin = PARP_RANK_SCORE_HOT_3;
	decision->native_tier = clamp(native_tier, 0, PARP_MAX_TIER);
	decision->native_tier_idx = clamp(tier_idx, 0, PARP_MAX_TIER);
	decision->special_native_protect = special_native_protect;
	decision->raw_delta_tier_q8 = delta;
	decision->delta_tier_q8 = delta;
	decision->effective_tier_q8 = parp_effective_tier_q8(
		decision->native_tier, delta);
	decision->native_protect =
		decision->native_tier > decision->native_tier_idx;
	decision->effective_protect = decision->effective_tier_q8 >
		decision->native_tier_idx * PARP_TIER_SCALE;
	if (effective_model_valid) {
		decision->cold_threshold = policy->cold_threshold;
		decision->hot_threshold_1 = policy->hot_threshold_1;
		decision->hot_threshold_2 = policy->hot_threshold_2;
		decision->hot_threshold_3 = policy->hot_threshold_3;
	}
	if (special_native_protect)
		decision->action = PARP_TIER_SPECIAL_NATIVE_PROTECT;
	else if (!decision->native_protect &&
		 decision->effective_protect)
		decision->action = PARP_TIER_PREDICTIVE_UPGRADE;
	else if (decision->native_protect &&
		 !decision->effective_protect)
		decision->action = PARP_TIER_PREDICTIVE_DOWNGRADE;
	else if (decision->native_protect)
		decision->action = PARP_TIER_KEEP_PROTECT;
	else
		decision->action = PARP_TIER_KEEP_RECLAIM;
	if (!effective_model_valid)
		decision->bypass = PARP_TIER_BYPASS_MODEL_INVALID;
}

bool parp_effective_tier_actual_protect(
		enum parp_effective_tier_mode mode,
		const struct parp_tier_decision *decision)
{
	if (decision->special_native_protect)
		return true;
	switch (mode) {
	case PARP_EFFECTIVE_TIER_PROTECT_ONLY:
		return decision->native_protect ||
		       (!decision->native_protect &&
			decision->effective_protect);
	case PARP_EFFECTIVE_TIER_BIDIRECTIONAL:
	case PARP_EFFECTIVE_TIER_RANDOM_MATCHED:
	case PARP_EFFECTIVE_TIER_RECENCY_BASELINE:
		return decision->effective_protect;
	case PARP_EFFECTIVE_TIER_OFF:
	case PARP_EFFECTIVE_TIER_SHADOW:
	default:
		return decision->native_protect;
	}
}

bool parp_effective_tier_budget_allows(unsigned long used,
		unsigned long candidates, unsigned long pages,
		unsigned long absolute_limit, u32 ratio_permyriad)
{
	u64 ratio_limit;

	if (!pages || !absolute_limit || !ratio_permyriad ||
	    ratio_permyriad > 10000 || used > ULONG_MAX - pages ||
	    used + pages > absolute_limit)
		return false;
	ratio_limit = mul_u64_u32_div(candidates, ratio_permyriad, 10000);
	if (candidates && !ratio_limit)
		ratio_limit = 1;
	return used + pages <= ratio_limit;
}

bool parp_effective_tier_random_claim(u64 random_value,
		unsigned long selected, unsigned long target,
		unsigned long seen, unsigned long eligible)
{
	u64 remainder;
	unsigned long remaining_target;
	unsigned long remaining_eligible;

	if (selected >= target)
		return false;
	if (seen >= eligible)
		return false;
	remaining_target = target - selected;
	remaining_eligible = eligible - seen;
	if (remaining_target >= remaining_eligible)
		return true;
	div64_u64_rem(random_value, remaining_eligible, &remainder);
	return remainder < remaining_target;
}

bool parp_effective_tier_upgrade_gate(bool severe_pressure,
		bool no_progress, enum parp_tier_bypass_reason *bypass)
{
	if (severe_pressure) {
		*bypass = PARP_TIER_BYPASS_PRESSURE;
		return false;
	}
	if (no_progress) {
		*bypass = PARP_TIER_BYPASS_NO_PROGRESS;
		return false;
	}
	*bypass = PARP_TIER_BYPASS_NONE;
	return true;
}

int parp_effective_tier_next_generation(int generation)
{
	if (generation < 0 || generation >= MAX_NR_GENS)
		return -EINVAL;
	return (generation + 1) % MAX_NR_GENS;
}

unsigned long parp_effective_tier_policy_flags(unsigned long old_flags,
		int new_generation)
{
	if (new_generation < 0 || new_generation >= MAX_NR_GENS)
		return old_flags;
	return (old_flags & ~LRU_GEN_MASK) |
		((new_generation + 1UL) << LRU_GEN_PGOFF);
}

static void parp_decision_native_fallback(struct parp_tier_decision *decision,
		enum parp_tier_bypass_reason bypass)
{
	decision->delta_tier_q8 = 0;
	decision->effective_tier_q8 =
		decision->native_tier * PARP_TIER_SCALE;
	decision->effective_protect = decision->native_protect;
	decision->bypass = bypass;
	if (decision->special_native_protect)
		decision->action = PARP_TIER_SPECIAL_NATIVE_PROTECT;
	else if (decision->native_protect)
		decision->action = PARP_TIER_KEEP_PROTECT;
	else
		decision->action = PARP_TIER_KEEP_RECLAIM;
}

static bool parp_candidate_features(struct folio *folio,
		s64 values[PARP_TIER_FEATURES],
		struct parp_tier_state_snapshot *snapshot)
{
	struct parp_reuse_page_ext *ext;
	struct page_ext *page_ext;
	u32 old_state;
	u32 new_state;
	u32 epochs;
	u32 now = parp_now_ms();
	u32 access_age;
	u32 generation_age;
	u8 candidates;
	u8 access_ema;
	bool access_valid;
	bool generation_valid;
	bool valid;

	ext = parp_reuse_ext_get(folio, &page_ext);
	if (!ext)
		return false;
	if (!parp_state_write_begin(ext, &old_state)) {
		snapshot->uncertain = true;
		page_ext_put(page_ext);
		return false;
	}
	epochs = READ_ONCE(ext->decision_epochs);
	access_age = parp_effective_tier_elapsed_ms(now,
			READ_ONCE(ext->last_access_ms), &access_valid);
	generation_age = parp_effective_tier_elapsed_ms(now,
			READ_ONCE(ext->generation_enter_ms), &generation_valid);
	candidates = FIELD_GET(PARP_STATE_CANDIDATE_MASK, old_state);
	candidates = min_t(u8, candidates + 1,
			   FIELD_MAX(PARP_STATE_CANDIDATE_MASK));
	access_ema = parp_access_ema_on_candidate(
		FIELD_GET(PARP_STATE_ACCESS_MASK, old_state));
	new_state = old_state & ~(PARP_STATE_ACCESS_MASK |
				PARP_STATE_CANDIDATE_MASK);
	new_state |= access_ema;
	new_state |= FIELD_PREP(PARP_STATE_CANDIDATE_MASK, candidates);
	valid = old_state & PARP_STATE_INITIALIZED;
	valid = valid && !(old_state & PARP_STATE_UNCERTAIN);
	valid = valid && access_valid && generation_valid &&
		READ_ONCE(ext->last_access_ms) &&
		READ_ONCE(ext->generation_enter_ms);
	valid = valid && FIELD_GET(PARP_TIER_STATE_EPOCH_MASK, epochs) ==
		READ_ONCE(parp_tier_state_epoch);
	values[0] = access_age;
	values[1] = READ_ONCE(ext->previous_interval_ms);
	values[2] = READ_ONCE(ext->reuse_interval_ema_ms);
	values[3] = candidates;
	values[4] = generation_age;
	values[5] = access_ema;
	snapshot->last_access_ms = READ_ONCE(ext->last_access_ms);
	snapshot->previous_interval_ms = values[1];
	snapshot->reuse_interval_ema_ms = values[2];
	snapshot->generation_enter_ms =
		READ_ONCE(ext->generation_enter_ms);
	snapshot->lifetime_epoch = READ_ONCE(ext->lifetime_epoch);
	snapshot->last_upgrade_epoch = epochs & PARP_TIER_EPOCH_MASK;
	snapshot->last_downgrade_epoch =
		(epochs >> PARP_TIER_DOWNGRADE_SHIFT) & PARP_TIER_EPOCH_MASK;
	snapshot->state_epoch = FIELD_GET(PARP_TIER_STATE_EPOCH_MASK, epochs);
	snapshot->state_sequence = (parp_state_seq(old_state) + 2) &
		FIELD_MAX(PARP_STATE_SEQ_MASK);
	snapshot->access_ema_q8 = access_ema;
	snapshot->consecutive_candidates = candidates;
	snapshot->generation = FIELD_GET(PARP_STATE_GENERATION_MASK, old_state);
	snapshot->pending_action = FIELD_GET(PARP_STATE_ACTION_MASK, old_state);
	snapshot->uncertain = old_state & PARP_STATE_UNCERTAIN;
	if (snapshot->generation != folio_lru_gen(folio)) {
		new_state |= PARP_STATE_UNCERTAIN;
		snapshot->uncertain = true;
		valid = false;
	}
	parp_state_write_end(ext, old_state, new_state);
	page_ext_put(page_ext);
	return valid;
}

bool parp_effective_tier_revalidate(struct folio *folio, u16 state_sequence)
{
	struct parp_reuse_page_ext *ext;
	struct page_ext *page_ext;
	u32 first;
	u32 second;
	u32 epochs;
	bool valid;

	ext = parp_reuse_ext_get(folio, &page_ext);
	if (!ext)
		return false;
	/* Pair with the writer's release of an even sequence. */
	first = smp_load_acquire(&ext->state);
	if (parp_state_seq(first) & 1)
		goto invalid;
	epochs = READ_ONCE(ext->decision_epochs);
	/* Complete the epoch read before validating the sequence again. */
	smp_rmb();
	second = READ_ONCE(ext->state);
	valid = first == second && !(parp_state_seq(second) & 1) &&
		parp_state_seq(second) == state_sequence &&
		!(second & PARP_STATE_UNCERTAIN) &&
		FIELD_GET(PARP_TIER_STATE_EPOCH_MASK, epochs) ==
		READ_ONCE(parp_tier_state_epoch);
	page_ext_put(page_ext);
	return valid;
invalid:
	page_ext_put(page_ext);
	return false;
}

bool parp_effective_tier_commit_revalidate(
		const struct parp_tier_scan_ctx *ctx, struct folio *folio,
		u16 state_sequence)
{
	struct parp_tier_runtime_config config;

	if (atomic_read(&parp_tier_state_fault) ||
	    READ_ONCE(parp_tier_mode) != ctx->mode ||
	    !parp_runtime_config_read(&config) ||
	    config.sequence != ctx->config_sequence)
		return false;
	return parp_effective_tier_revalidate(folio, state_sequence);
}

static bool parp_effective_tier_claim_epoch_sequence(struct folio *folio,
		bool upgrade, u16 epoch_tag, int expected_sequence,
		u16 *claimed_sequence, enum parp_tier_bypass_reason *bypass)
{
	struct parp_reuse_page_ext *ext;
	struct page_ext *page_ext;
	u32 old_state;
	u32 epochs;
	u32 previous;

	ext = parp_reuse_ext_get(folio, &page_ext);
	if (!ext) {
		*bypass = PARP_TIER_BYPASS_METADATA_MISSING;
		return false;
	}
	if (!parp_state_write_begin(ext, &old_state)) {
		*bypass = PARP_TIER_BYPASS_STATE_UNSTABLE;
		page_ext_put(page_ext);
		return false;
	}
	if (expected_sequence >= 0 &&
	    parp_state_seq(old_state) != expected_sequence) {
		*bypass = PARP_TIER_BYPASS_GENERATION_RACE;
		parp_state_write_end(ext, old_state, old_state);
		page_ext_put(page_ext);
		return false;
	}
	epochs = READ_ONCE(ext->decision_epochs);
	if (FIELD_GET(PARP_TIER_STATE_EPOCH_MASK, epochs) !=
	    READ_ONCE(parp_tier_state_epoch)) {
		*bypass = PARP_TIER_BYPASS_STATE_UNSTABLE;
		parp_state_write_end(ext, old_state, old_state);
		page_ext_put(page_ext);
		return false;
	}
	if (upgrade)
		previous = epochs & PARP_TIER_EPOCH_MASK;
	else
		previous = (epochs >> PARP_TIER_DOWNGRADE_SHIFT) &
			PARP_TIER_EPOCH_MASK;
	if (previous == epoch_tag) {
		*bypass = upgrade ? PARP_TIER_BYPASS_REPEAT_UPGRADE :
				    PARP_TIER_BYPASS_REPEAT_DOWNGRADE;
		parp_state_write_end(ext, old_state, old_state);
		page_ext_put(page_ext);
		return false;
	}
	if (upgrade) {
		epochs &= ~PARP_TIER_EPOCH_MASK;
		epochs |= epoch_tag;
	} else {
		epochs &= ~(PARP_TIER_EPOCH_MASK <<
			    PARP_TIER_DOWNGRADE_SHIFT);
		epochs |= (u32)epoch_tag << PARP_TIER_DOWNGRADE_SHIFT;
	}
	WRITE_ONCE(ext->decision_epochs, epochs);
	if (claimed_sequence)
		*claimed_sequence = (parp_state_seq(old_state) + 2) &
			FIELD_MAX(PARP_STATE_SEQ_MASK);
	parp_state_write_end(ext, old_state, old_state);
	page_ext_put(page_ext);
	return true;
}

bool parp_effective_tier_claim_epoch(struct folio *folio, bool upgrade,
		u16 epoch_tag, enum parp_tier_bypass_reason *bypass)
{
	return parp_effective_tier_claim_epoch_sequence(folio, upgrade,
			epoch_tag, -1, NULL, bypass);
}

static bool parp_effective_tier_release_epoch(struct folio *folio,
		bool upgrade, u16 epoch_tag)
{
	struct parp_reuse_page_ext *ext;
	struct page_ext *page_ext;
	u32 old_state;
	u32 epochs;
	u32 mask;
	u32 shift;

	ext = parp_reuse_ext_get(folio, &page_ext);
	if (!ext)
		return false;
	if (!parp_state_write_begin(ext, &old_state)) {
		atomic64_inc(&parp_tier_stats.state_unstable);
		parp_state_mark_uncertain(ext);
		atomic_set(&parp_tier_state_fault, 1);
		page_ext_put(page_ext);
		return false;
	}
	epochs = READ_ONCE(ext->decision_epochs);
	shift = upgrade ? 0 : PARP_TIER_DOWNGRADE_SHIFT;
	mask = PARP_TIER_EPOCH_MASK << shift;
	if (FIELD_GET(PARP_TIER_STATE_EPOCH_MASK, epochs) ==
	    READ_ONCE(parp_tier_state_epoch) &&
	    ((epochs & mask) >> shift) == epoch_tag) {
		epochs &= ~mask;
		WRITE_ONCE(ext->decision_epochs, epochs);
	}
	parp_state_write_end(ext, old_state, old_state);
	page_ext_put(page_ext);
	return true;
}

static bool parp_set_pending_action(struct folio *folio, u8 action)
{
	struct parp_reuse_page_ext *ext;
	struct page_ext *page_ext;
	u32 old_state;
	u32 new_state;

	ext = parp_reuse_ext_get(folio, &page_ext);
	if (!ext)
		return false;
	if (!parp_state_write_begin(ext, &old_state)) {
		parp_state_mark_uncertain(ext);
		page_ext_put(page_ext);
		return false;
	}
	new_state = old_state & ~PARP_STATE_ACTION_MASK;
	new_state |= FIELD_PREP(PARP_STATE_ACTION_MASK, action + 1);
	parp_state_write_end(ext, old_state, new_state);
	page_ext_put(page_ext);
	return true;
}

static unsigned long parp_saturating_add(unsigned long left,
		unsigned long right)
{
	return left + min(right, ULONG_MAX - left);
}

void __parp_effective_tier_prepare(struct parp_tier_scan_ctx *ctx,
		struct lruvec *lruvec, int type, int reclaim_priority,
		unsigned long nr_to_reclaim, unsigned long nr_reclaimed,
		bool is_kswapd, bool is_memcg_reclaim, bool proactive_reclaim)
{
	struct lru_gen_folio *lrugen = &lruvec->lrugen;
	typeof(lrugen->parp_effective_tier[0]) *state =
		&lrugen->parp_effective_tier[type];
	struct parp_tier_runtime_config config;
	u16 tag;

	memset(ctx, 0, sizeof(*ctx));
	rcu_read_lock();
	ctx->rcu_held = true;
	ctx->mode = READ_ONCE(parp_tier_mode);
	if (ctx->mode == PARP_EFFECTIVE_TIER_OFF ||
	    atomic_read(&parp_tier_state_fault) ||
	    !parp_runtime_config_read(&config)) {
		ctx->rcu_held = false;
		rcu_read_unlock();
		return;
	}
	ctx->enabled = true;
	ctx->type = type;
	ctx->nid = lruvec_pgdat(lruvec)->node_id;
	ctx->memcg_id = mem_cgroup_id(lruvec_memcg(lruvec));
	ctx->reclaim_priority = reclaim_priority;
	ctx->nr_to_reclaim = nr_to_reclaim;
	ctx->nr_reclaimed_before = nr_reclaimed;
	ctx->epoch_reclaimed_pages = nr_reclaimed;
	ctx->reclaim_context = parp_tier_reclaim_context(is_kswapd,
			is_memcg_reclaim, proactive_reclaim); /* #lzx */
	ctx->source_seq = lrugen->min_seq[type];
	ctx->batch_id = atomic64_inc_return(&parp_tier_batch_id);
	ctx->experiment_id = config.experiment_id;
	ctx->session_id = config.session_id;
	ctx->config_sequence = config.sequence;
	ctx->run_epoch = READ_ONCE(parp_tier_state_epoch);
	ctx->severe_pressure =
		reclaim_priority <= PARP_SEVERE_PRESSURE_PRIORITY;
	if (!state->epoch_id || state->source_seq != ctx->source_seq ||
	    state->state_epoch != READ_ONCE(parp_tier_state_epoch)) {
		state->source_seq = ctx->source_seq;
		state->state_epoch = READ_ONCE(parp_tier_state_epoch);
		state->candidate_pages = 0;
		state->upgrade_pages = 0;
		state->downgrade_pages = 0;
		state->no_progress_rounds = 0;
		if (!++state->epoch_id)
			state->epoch_id++;
	}
	tag = state->epoch_id & PARP_TIER_EPOCH_MASK;
	if (!tag) {
		state->epoch_id++;
		tag = state->epoch_id & PARP_TIER_EPOCH_MASK;
	}
	ctx->no_progress = state->no_progress_rounds >=
		PARP_NO_PROGRESS_LIMIT;
	ctx->consecutive_no_progress_batches = state->no_progress_rounds;
	ctx->epoch_id = state->epoch_id;
	ctx->epoch_tag = tag;
	ctx->pressure_level_kernel = parp_effective_tier_pressure_level(
		reclaim_priority, ctx->no_progress, nr_to_reclaim, nr_reclaimed);
	/* #lzx */
	ctx->pressure_bypass_reason =
		ctx->pressure_level_kernel == PARP_PRESSURE_CRITICAL ?
		(ctx->no_progress ? PARP_TIER_BYPASS_NO_PROGRESS :
		 PARP_TIER_BYPASS_PRESSURE) : PARP_TIER_BYPASS_NONE; /* #lzx */
}

s32 parp_effective_tier_recency_score(u32 access_age_ms,
		const struct parp_tier_policy *policy)
{
	if (!policy)
		return 0;
	if (access_age_ms <= 100)
		return policy->hot_threshold_2;
	if (access_age_ms <= 500)
		return policy->hot_threshold_1;
	if (access_age_ms >= 10000)
		return policy->cold_threshold;
	return 0;
}

static s32 parp_random_score(const struct parp_tier_runtime_config *config,
		struct folio *folio, const struct parp_tier_scan_ctx *ctx,
		int native_tier, int tier_idx, u32 lifetime_epoch)
{
	u64 random = siphash_2u64(parp_folio_cookie(folio, lifetime_epoch),
				   ctx->batch_id, &parp_tier_cookie_key);
	u32 bucket = do_div(random, 10000);

	if (native_tier == tier_idx + 1 &&
	    bucket < config->downgrade_ratio_permyriad)
		return config->policy.cold_threshold;
	if (native_tier <= tier_idx &&
	    bucket < config->upgrade_ratio_permyriad)
		return config->policy.hot_threshold_2;
	return 0;
}

static bool parp_effective_tier_action_applies(
		enum parp_effective_tier_mode mode, enum parp_tier_action action)
{
	if (action == PARP_TIER_PREDICTIVE_UPGRADE)
		return mode == PARP_EFFECTIVE_TIER_PROTECT_ONLY ||
		       mode == PARP_EFFECTIVE_TIER_BIDIRECTIONAL ||
		       mode == PARP_EFFECTIVE_TIER_RANDOM_MATCHED ||
		       mode == PARP_EFFECTIVE_TIER_RECENCY_BASELINE;
	if (action == PARP_TIER_PREDICTIVE_DOWNGRADE)
		return mode == PARP_EFFECTIVE_TIER_BIDIRECTIONAL ||
		       mode == PARP_EFFECTIVE_TIER_RANDOM_MATCHED ||
		       mode == PARP_EFFECTIVE_TIER_RECENCY_BASELINE;
	return false;
}

static void parp_effective_tier_cancel_reservation(
		struct parp_tier_scan_ctx *ctx, struct lruvec *lruvec,
		struct folio *folio, struct parp_tier_decision *decision)
{
	typeof(lruvec->lrugen.parp_effective_tier[0]) *state =
		&lruvec->lrugen.parp_effective_tier[ctx->type];
	unsigned long pages = decision->folio_nr_pages;

	if (!decision->reservation_active)
		return;
	if (!parp_effective_tier_release_epoch(folio,
			decision->reservation_upgrade, ctx->epoch_tag)) {
		atomic_set(&parp_tier_state_fault, 1);
		decision->reservation_active = false;
		return;
	}
	if (decision->reservation_upgrade) {
		if (ctx->upgrade_pages < pages ||
		    state->upgrade_pages < pages)
			goto invariant_fault;
		ctx->upgrade_pages -= pages;
		state->upgrade_pages -= pages;
	} else {
		if (ctx->downgrade_pages < pages ||
		    state->downgrade_pages < pages)
			goto invariant_fault;
		ctx->downgrade_pages -= pages;
		state->downgrade_pages -= pages;
	}
	decision->reservation_active = false;
	return;
invariant_fault:
	atomic_set(&parp_tier_state_fault, 1);
	decision->reservation_active = false;
}

void __parp_effective_tier_decide(struct parp_tier_scan_ctx *ctx,
		struct lruvec *lruvec, struct folio *folio, int native_tier,
		int tier_idx, bool special_native_protect,
		struct parp_tier_decision *decision)
{
	struct lru_gen_folio *lrugen = &lruvec->lrugen;
	typeof(lrugen->parp_effective_tier[0]) *state =
		&lrugen->parp_effective_tier[ctx->type];
	struct parp_tier_runtime_config config;
	struct parp_tier_state_snapshot snapshot = { };
	s64 values[PARP_TIER_FEATURES] = { };
	unsigned long pages = folio_nr_pages(folio);
	u64 decision_started = ktime_get_mono_fast_ns();
	u64 score_started;
	u64 score_duration = 0;
	s32 score = 0;
	bool metadata_valid = false;
	bool score_valid = false;
	bool allowed;
	bool applies;
	enum parp_tier_bypass_reason bypass = PARP_TIER_BYPASS_NONE;

	atomic64_inc(&parp_tier_stats.candidates);
	atomic64_add(pages, &parp_tier_stats.candidate_pages);
	ctx->candidate_pages = parp_saturating_add(ctx->candidate_pages, pages);
	if (ctx->mode != PARP_EFFECTIVE_TIER_SHADOW)
		state->candidate_pages = parp_saturating_add(
			state->candidate_pages, pages);
	if (atomic_read(&parp_tier_state_fault)) {
		parp_effective_tier_classify(0, false, native_tier, tier_idx,
				special_native_protect, NULL, decision);
		decision->bypass = PARP_TIER_BYPASS_STATE_UNSTABLE;
		goto copy_features;
	}
	if (!parp_runtime_config_read(&config) ||
	    config.sequence != ctx->config_sequence) {
		parp_effective_tier_classify(0, false, native_tier, tier_idx,
				special_native_protect, NULL, decision);
		decision->bypass = PARP_TIER_BYPASS_MODEL_INVALID;
		atomic64_inc(&parp_tier_stats.model_invalid);
		goto copy_features;
	}
	metadata_valid = parp_candidate_features(folio, values, &snapshot);
	if (!metadata_valid) {
		parp_effective_tier_classify(0, false, native_tier, tier_idx,
				special_native_protect, &config.policy, decision);
		decision->bypass = snapshot.uncertain ?
			PARP_TIER_BYPASS_STATE_UNSTABLE :
			PARP_TIER_BYPASS_METADATA_MISSING;
		if (snapshot.uncertain)
			atomic64_inc(&parp_tier_stats.state_unstable);
		else
			atomic64_inc(&parp_tier_stats.metadata_missing);
		goto copy_features;
	}
	score_started = ktime_get_mono_fast_ns();
	if (ctx->mode == PARP_EFFECTIVE_TIER_RECENCY_BASELINE) {
		score = parp_effective_tier_recency_score(values[0],
				&config.policy);
		score_valid = true;
	} else if (ctx->mode == PARP_EFFECTIVE_TIER_RANDOM_MATCHED) {
		score = parp_random_score(&config, folio, ctx, native_tier,
				tier_idx, snapshot.lifetime_epoch);
		score_valid = true;
	} else {
		score_valid = parp_effective_tier_score_values(values, &score);
	}
	score_duration = ktime_get_mono_fast_ns() - score_started;
	ctx->model_time_ns += score_duration;
	parp_account_latency(&parp_tier_stats.score_time_ns_total,
		&parp_tier_stats.score_time_ns_max,
		parp_tier_stats.score_hist, score_duration);
	if (score_valid)
		atomic64_inc(&parp_tier_stats.scores);
	else
		atomic64_inc(&parp_tier_stats.model_invalid);
	parp_effective_tier_classify(score, score_valid, native_tier,
			tier_idx, special_native_protect, &config.policy,
			decision);
	if (!score_valid)
		goto copy_features;
	if (special_native_protect)
		goto copy_features;
	if (decision->raw_delta_tier_q8 < 0 &&
	    native_tier != tier_idx + 1) {
		parp_decision_native_fallback(decision,
			native_tier >= tier_idx + 2 ?
			PARP_TIER_BYPASS_STRONG_NATIVE :
			PARP_TIER_BYPASS_NOT_BOUNDARY);
		goto copy_features;
	}
	if (decision->action == PARP_TIER_PREDICTIVE_DOWNGRADE &&
	    config.policy.require_two_cold &&
	    snapshot.consecutive_candidates < 2) {
		parp_decision_native_fallback(decision,
			PARP_TIER_BYPASS_STATE_UNSTABLE);
		goto copy_features;
	}
	if ((decision->action == PARP_TIER_PREDICTIVE_UPGRADE ||
	     decision->action == PARP_TIER_PREDICTIVE_DOWNGRADE) &&
	    !parp_effective_tier_upgrade_gate(ctx->severe_pressure,
			ctx->no_progress, &bypass)) {
		parp_decision_native_fallback(decision, bypass);
		goto copy_features;
	}
	if (decision->action == PARP_TIER_PREDICTIVE_UPGRADE) {
		applies = parp_effective_tier_action_applies(ctx->mode,
							decision->action);
		allowed = parp_effective_tier_budget_allows(
			ctx->upgrade_pages, ctx->candidate_pages, pages,
			config.upgrade_batch_pages,
			config.upgrade_ratio_permyriad) &&
			(!applies ||
			parp_effective_tier_budget_allows(
			state->upgrade_pages, state->candidate_pages, pages,
			config.upgrade_epoch_pages,
			config.upgrade_ratio_permyriad));
		if (!allowed) {
			parp_decision_native_fallback(decision,
				PARP_TIER_BYPASS_UPGRADE_BUDGET);
			goto copy_features;
		}
		if (applies &&
		    !parp_effective_tier_claim_epoch_sequence(folio, true,
				ctx->epoch_tag, snapshot.state_sequence,
				&snapshot.state_sequence, &bypass)) {
			parp_decision_native_fallback(decision, bypass);
			goto copy_features;
		}
		ctx->upgrade_pages += pages;
		if (applies) {
			state->upgrade_pages += pages;
			decision->reservation_active = true;
			decision->reservation_upgrade = true;
		}
	} else if (decision->action == PARP_TIER_PREDICTIVE_DOWNGRADE) {
		applies = parp_effective_tier_action_applies(ctx->mode,
							decision->action);
		allowed = parp_effective_tier_budget_allows(
			ctx->downgrade_pages, ctx->candidate_pages, pages,
			config.downgrade_batch_pages,
			config.downgrade_ratio_permyriad) &&
			(!applies ||
			parp_effective_tier_budget_allows(
			state->downgrade_pages, state->candidate_pages, pages,
			config.downgrade_epoch_pages,
			config.downgrade_ratio_permyriad));
		if (!allowed) {
			parp_decision_native_fallback(decision,
				PARP_TIER_BYPASS_DOWNGRADE_BUDGET);
			goto copy_features;
		}
		if (applies &&
		    !parp_effective_tier_claim_epoch_sequence(folio, false,
				ctx->epoch_tag, snapshot.state_sequence,
				&snapshot.state_sequence, &bypass)) {
			parp_decision_native_fallback(decision, bypass);
			goto copy_features;
		}
		ctx->downgrade_pages += pages;
		if (applies) {
			state->downgrade_pages += pages;
			decision->reservation_active = true;
			decision->reservation_upgrade = false;
		}
	}
copy_features:
	parp_effective_tier_pressure_counterfactuals(ctx, decision); /* #lzx */
	if (ctx->mode == PARP_EFFECTIVE_TIER_RECENCY_BASELINE)
		decision->scorer_kind = PARP_TIER_SCORER_RECENCY_BASELINE;
	else if (ctx->mode == PARP_EFFECTIVE_TIER_RANDOM_MATCHED)
		decision->scorer_kind = PARP_TIER_SCORER_RANDOM_BASELINE;
	else
		decision->scorer_kind = PARP_TIER_SCORER_PAIRWISE_LINEAR;
	memcpy(decision->features, values, sizeof(values));
	decision->features_valid = metadata_valid;
	decision->folio_nr_pages = pages;
	decision->generation_index = folio_lru_gen(folio);
	decision->page_state_sequence = snapshot.state_sequence;
	decision->score_duration_ns = score_duration;
	decision->actual_tier_protect = parp_effective_tier_actual_protect(
		ctx->mode, decision);
	decision->folio_nr_pages = pages;
	decision->decision_duration_ns =
		ktime_get_mono_fast_ns() - decision_started;
	parp_account_latency(&parp_tier_stats.decision_time_ns_total,
		&parp_tier_stats.decision_time_ns_max,
		parp_tier_stats.decision_hist,
		decision->decision_duration_ns);
}

void __parp_effective_tier_finish(struct parp_tier_scan_ctx *ctx,
		struct lruvec *lruvec, struct folio *folio,
		struct parp_tier_decision *decision, bool sort_result,
		bool isolate_attempted, bool isolate_result)
{
	bool reservation_committed;

	reservation_committed = decision->reservation_active &&
		((decision->reservation_upgrade && sort_result &&
		  decision->action == PARP_TIER_PREDICTIVE_UPGRADE) ||
		 (!decision->reservation_upgrade &&
		  decision->action == PARP_TIER_PREDICTIVE_DOWNGRADE));
	if (decision->reservation_active && !reservation_committed)
		parp_effective_tier_cancel_reservation(ctx, lruvec, folio,
						       decision);
	if (decision->action < ARRAY_SIZE(parp_tier_stats.action_pages))
		atomic64_add(decision->folio_nr_pages,
			     &parp_tier_stats.action_pages[decision->action]);
	if (decision->bypass < PARP_TIER_BYPASS_NR)
		atomic64_add(decision->folio_nr_pages,
			     &parp_tier_stats.bypass[decision->bypass]);

	if (isolate_result && !parp_set_pending_action(folio, decision->action)) {
		atomic64_inc(&parp_tier_stats.state_unstable);
		atomic_set(&parp_tier_state_fault, 1);
	}
	if (trace_parp_effective_tier_decision_enabled()) {
		struct parp_effective_tier_trace trace;
		u32 lifetime_epoch;
		u64 cookie;

		if (!parp_effective_tier_identity(folio, &lifetime_epoch,
				&cookie))
			atomic_set(&parp_tier_state_fault, 1);
		memset(&trace, 0, sizeof(trace));
		trace.timestamp_ns = ktime_get_mono_fast_ns();
		trace.experiment_id = ctx->experiment_id;
		trace.session_id = ctx->session_id;
		trace.folio_cookie = cookie;
		trace.memcg_id = ctx->memcg_id;
		trace.source_seq = ctx->source_seq;
		trace.batch_id = ctx->batch_id;
		trace.reclaim_epoch = ctx->epoch_id;
		trace.score_duration_ns = decision->score_duration_ns;
		trace.decision_duration_ns = decision->decision_duration_ns;
		memcpy(trace.features, decision->features,
		       sizeof(trace.features));
		trace.reuse_score = decision->reuse_score;
		trace.cold_threshold = decision->cold_threshold;
		trace.hot_threshold_1 = decision->hot_threshold_1;
		trace.hot_threshold_2 = decision->hot_threshold_2;
		trace.hot_threshold_3 = decision->hot_threshold_3;
		trace.delta_tier_q8 = decision->delta_tier_q8;
		trace.fixed_delta_q8 = decision->fixed_delta_q8;
		trace.binary_bypass_delta_q8 = decision->binary_bypass_delta_q8;
		trace.pressure_aware_delta_q8 =
			decision->pressure_aware_delta_q8;
		trace.effective_tier_q8 = decision->effective_tier_q8;
		trace.priority = ctx->reclaim_priority;
		trace.nr_to_reclaim = ctx->nr_to_reclaim;
		trace.nr_reclaimed_before = ctx->nr_reclaimed_before;
		trace.epoch_reclaimed_pages = ctx->epoch_reclaimed_pages;
		trace.batch_scanned_pages = ctx->batch_scanned_pages;
		trace.batch_isolated_pages = ctx->batch_isolated_pages;
		trace.batch_reclaimed_pages = ctx->batch_reclaimed_pages;
		trace.folio_nr_pages = decision->folio_nr_pages;
		trace.lifetime_epoch = lifetime_epoch;
		trace.nid = ctx->nid;
		trace.expected_model_version = PARP_TIER_MODEL_VERSION;
		trace.pressure_policy_version = decision->pressure_policy_version;
		strscpy(trace.pressure_policy_provenance,
			PARP_TIER_PRESSURE_PROVENANCE,
			sizeof(trace.pressure_policy_provenance)); /* #lzx */
		trace.consecutive_no_progress_batches =
			ctx->consecutive_no_progress_batches;
		if (decision->scorer_kind == PARP_TIER_SCORER_PAIRWISE_LINEAR) {
			trace.model_version = parp_global_model.model_version;
			trace.feature_schema_version =
				parp_global_model.feature_schema_version;
			strscpy(trace.model_type, PARP_TIER_MODEL_TYPE,
				sizeof(trace.model_type));
			strscpy(trace.model_checksum, PARP_TIER_MODEL_CHECKSUM,
				sizeof(trace.model_checksum));
			strscpy(trace.model_provenance,
				PARP_TIER_MODEL_PROVENANCE,
				sizeof(trace.model_provenance));
		} else {
			trace.model_version = 1;
			trace.feature_schema_version = PARP_TIER_SCHEMA_VERSION;
			strscpy(trace.model_type,
				decision->scorer_kind ==
				PARP_TIER_SCORER_RECENCY_BASELINE ?
				"recency_baseline" : "random_matched_baseline",
				sizeof(trace.model_type));
			strscpy(trace.model_checksum, "none",
				sizeof(trace.model_checksum));
			strscpy(trace.model_provenance, "CONTROL_BASELINE",
				sizeof(trace.model_provenance));
		}
		trace.generation_index = decision->generation_index;
		trace.native_tier = decision->native_tier;
		trace.native_tier_idx = decision->native_tier_idx;
		trace.page_type = ctx->type;
		trace.mode = ctx->mode;
		trace.action = decision->action;
		trace.bypass = decision->bypass;
		trace.rank_score_bin = decision->rank_score_bin;
		trace.pressure_level_kernel = decision->pressure_level_kernel;
		trace.reclaim_context = decision->reclaim_context;
		trace.pressure_bypass_reason = decision->pressure_bypass_reason;
		trace.special_native_protect = decision->special_native_protect;
		trace.model_valid = decision->model_valid;
		trace.features_valid = decision->features_valid;
		trace.native_protect = decision->native_protect;
		trace.effective_protect = decision->effective_protect;
		trace.fixed_effective_protect = decision->fixed_effective_protect;
		trace.pressure_aware_effective_protect =
			decision->pressure_aware_effective_protect; /* #lzx */
		trace.actual_tier_protect = decision->actual_tier_protect;
		trace.sort_result = sort_result;
		trace.isolate_attempted = isolate_attempted;
		trace.isolate_result = isolate_result;
		trace.trace_sequence = atomic64_inc_return(
			&parp_tier_trace_sequence);
		atomic64_inc(&parp_tier_stats.trace_decisions);
		trace_parp_effective_tier_decision(&trace);
	}
} /* #lzx */

static void parp_effective_tier_trace_batch(
		struct parp_tier_scan_ctx *ctx, unsigned long isolated,
		bool reclaim_result)
{
	if (trace_parp_effective_tier_batch_enabled()) {
		struct parp_effective_tier_batch_trace trace = {
			.timestamp_ns = ktime_get_mono_fast_ns(),
			.experiment_id = ctx->experiment_id,
			.session_id = ctx->session_id,
			.batch_id = ctx->batch_id,
			.reclaim_epoch = ctx->epoch_id,
			.model_time_ns = ctx->model_time_ns,
			.candidate_pages = ctx->candidate_pages,
			.upgrade_pages = ctx->upgrade_pages,
			.downgrade_pages = ctx->downgrade_pages,
			.isolated_pages = isolated,
			.nr_to_reclaim = ctx->nr_to_reclaim,
			.nr_reclaimed_before = ctx->nr_reclaimed_before,
			.epoch_reclaimed_pages = ctx->epoch_reclaimed_pages,
			.batch_scanned_pages = ctx->batch_scanned_pages,
			.batch_isolated_pages = ctx->batch_isolated_pages,
			.batch_reclaimed_pages = ctx->batch_reclaimed_pages,
			.pressure_policy_version =
				PARP_TIER_PRESSURE_POLICY_VERSION,
			.consecutive_no_progress_batches =
				ctx->consecutive_no_progress_batches,
			.page_type = ctx->type,
			.mode = ctx->mode,
			.pressure_level_kernel = ctx->pressure_level_kernel,
			.reclaim_context = ctx->reclaim_context,
			.pressure_bypass_reason = ctx->pressure_bypass_reason,
			.reclaim_result = reclaim_result,
		};

		trace.trace_sequence = atomic64_inc_return(
			&parp_tier_trace_sequence);
		atomic64_inc(&parp_tier_stats.trace_batches);
		trace_parp_effective_tier_batch(&trace);
	}
} /* #lzx */

void __parp_effective_tier_batch_finish(struct parp_tier_scan_ctx *ctx,
		unsigned long isolated)
{
	parp_effective_tier_trace_batch(ctx, isolated, false); /* #lzx */
	if (ctx->rcu_held) {
		ctx->rcu_held = false;
		rcu_read_unlock();
	}
}

void __parp_effective_tier_batch_reclaim(struct parp_tier_scan_ctx *ctx,
		unsigned long reclaimed)
{
	ctx->batch_reclaimed_pages = parp_saturating_add(
		ctx->batch_reclaimed_pages, reclaimed);
	ctx->epoch_reclaimed_pages = parp_saturating_add(
		ctx->nr_reclaimed_before, ctx->batch_reclaimed_pages);
	parp_effective_tier_trace_batch(ctx, ctx->batch_isolated_pages, true);
} /* #lzx */

void __parp_effective_tier_feedback(struct lruvec *lruvec, int type,
		u8 run_epoch, unsigned long isolated, unsigned long reclaimed)
{
	typeof(lruvec->lrugen.parp_effective_tier[0]) *state =
		&lruvec->lrugen.parp_effective_tier[type];

	if (!run_epoch || run_epoch != READ_ONCE(parp_tier_state_epoch) ||
	    state->state_epoch != run_epoch || !isolated)
		return;
	if (reclaimed) {
		state->no_progress_rounds = 0;
		return;
	}
	if (state->no_progress_rounds < U16_MAX)
		state->no_progress_rounds++;
}

void __parp_effective_tier_outcome(struct folio *folio,
		enum parp_tier_outcome outcome)
{
	struct parp_reuse_page_ext *ext;
	struct page_ext *page_ext;
	u32 old_state;
	u32 new_state;
	u32 decision_epochs;
	u8 pending;

	if (outcome > PARP_TIER_OUTCOME_DEMOTE_ATTEMPT)
		return;
	ext = parp_reuse_ext_get(folio, &page_ext);
	if (!ext)
		return;
	if (!parp_state_write_begin(ext, &old_state)) {
		atomic64_inc(&parp_tier_stats.state_unstable);
		parp_state_mark_uncertain(ext);
		atomic_set(&parp_tier_state_fault, 1);
		page_ext_put(page_ext);
		return;
	}
	pending = FIELD_GET(PARP_STATE_ACTION_MASK, old_state);
	decision_epochs = READ_ONCE(ext->decision_epochs);
	new_state = old_state & ~PARP_STATE_ACTION_MASK;
	parp_state_write_end(ext, old_state, new_state);
	if (!pending ||
	    FIELD_GET(PARP_TIER_STATE_EPOCH_MASK, decision_epochs) !=
	    READ_ONCE(parp_tier_state_epoch)) {
		page_ext_put(page_ext);
		return;
	}
	atomic64_inc(&parp_tier_stats.outcomes[outcome]);
	if (trace_parp_effective_tier_outcome_enabled()) {
		struct parp_effective_tier_outcome_trace trace = { };
		struct parp_tier_runtime_config config = { };

		trace.timestamp_ns = ktime_get_mono_fast_ns();
		if (parp_runtime_config_read(&config)) {
			trace.experiment_id = config.experiment_id;
			trace.session_id = config.session_id;
		}
		trace.lifetime_epoch = READ_ONCE(ext->lifetime_epoch);
		trace.folio_cookie = parp_folio_cookie(folio,
						trace.lifetime_epoch);
		trace.proposed_action = pending - 1;
		trace.actual_action = trace.proposed_action ==
			PARP_TIER_PREDICTIVE_UPGRADE ?
			PARP_TIER_KEEP_RECLAIM : trace.proposed_action;
		trace.outcome = outcome;
		trace.trace_sequence = atomic64_inc_return(
			&parp_tier_trace_sequence);
		atomic64_inc(&parp_tier_stats.trace_outcomes);
		trace_parp_effective_tier_outcome(&trace);
	}
	page_ext_put(page_ext);
}

void __parp_effective_tier_lock_start(
		struct parp_tier_lock_measurement *measurement)
{
	struct parp_tier_runtime_config config = { };

	*measurement = (struct parp_tier_lock_measurement) { };
	measurement->irq_disabled_started_ns = ktime_get_mono_fast_ns();
	measurement->mode = READ_ONCE(parp_tier_mode);
	if (parp_runtime_config_read(&config)) {
		measurement->experiment_id = config.experiment_id;
		measurement->session_id = config.session_id;
	}
	measurement->lock_attempt_started_ns = ktime_get_mono_fast_ns();
}

void __parp_effective_tier_lock_acquired(
		struct parp_tier_lock_measurement *measurement)
{
	measurement->acquired_ns = ktime_get_mono_fast_ns();
}

void __parp_effective_tier_lock_releasing(
		struct parp_tier_lock_measurement *measurement)
{
	measurement->releasing_ns = ktime_get_mono_fast_ns();
}

void __parp_effective_tier_lock_unlocked(
		struct parp_tier_lock_measurement *measurement)
{
	measurement->irq_disabled_ended_ns = ktime_get_mono_fast_ns();
}

void __parp_effective_tier_lock_finish(
		struct parp_tier_lock_measurement *measurement,
		enum parp_tier_lock_scope scope, int nid)
{
	u64 duration = measurement->releasing_ns - measurement->acquired_ns;

	parp_account_latency(&parp_tier_stats.lock_time_ns_total,
		&parp_tier_stats.lock_time_ns_max, parp_tier_stats.lock_hist,
		duration);
	if (trace_parp_effective_tier_lock_enabled()) {
		struct parp_effective_tier_lock_trace trace = {
			.timestamp_ns = measurement->irq_disabled_ended_ns,
			.experiment_id = measurement->experiment_id,
			.session_id = measurement->session_id,
			.wait_ns = measurement->acquired_ns -
				measurement->lock_attempt_started_ns,
			.held_ns = duration,
			.irq_disabled_ns = measurement->irq_disabled_measured ?
				measurement->irq_disabled_ended_ns -
				measurement->irq_disabled_started_ns : 0,
			.nid = nid,
			.mode = measurement->mode,
			.scope = scope,
			.irq_disabled_measured =
				measurement->irq_disabled_measured,
		};

		trace.trace_sequence = atomic64_inc_return(
			&parp_tier_trace_sequence);
		atomic64_inc(&parp_tier_stats.trace_locks);
		trace_parp_effective_tier_lock(&trace);
	}
}

bool parp_effective_tier_get_lock_observe(void)
{
	return static_branch_unlikely(&parp_effective_tier_lock_observe);
}

int parp_effective_tier_set_lock_observe(bool enabled)
{
	mutex_lock(&parp_tier_lock_observe_lock);
	if (enabled != parp_effective_tier_get_lock_observe()) {
		if (enabled)
			static_branch_enable(&parp_effective_tier_lock_observe);
		else
			static_branch_disable(&parp_effective_tier_lock_observe);
	}
	mutex_unlock(&parp_tier_lock_observe_lock);
	return 0;
}

enum parp_effective_tier_mode parp_effective_tier_get_mode(void)
{
	return READ_ONCE(parp_tier_mode);
}

bool parp_effective_tier_metadata_ready(void)
{
	return READ_ONCE(parp_tier_metadata_ready);
} /* #lzx */

int parp_effective_tier_set_mode(enum parp_effective_tier_mode mode)
{
	struct parp_tier_runtime_config config;
	enum parp_effective_tier_mode old;
	int error = 0;

	if (mode > PARP_EFFECTIVE_TIER_RECENCY_BASELINE)
		return -EINVAL;
	if (mode >= PARP_EFFECTIVE_TIER_PROTECT_ONLY &&
	    !IS_ENABLED(CONFIG_PARP_EFFECTIVE_TIER_EXPERIMENTAL_APPLY))
		return -EOPNOTSUPP;
	if (mode == PARP_EFFECTIVE_TIER_SHADOW &&
	    !parp_effective_tier_metadata_ready())
		return -EOPNOTSUPP; /* #lzx */
	mutex_lock(&parp_tier_mode_lock);
	if (mode >= PARP_EFFECTIVE_TIER_PROTECT_ONLY &&
	    (!parp_runtime_config_read(&config) ||
	     config.policy.max_upgrade_tiers > 2)) {
		error = -ERANGE;
		goto unlock;
	}
	old = READ_ONCE(parp_tier_mode);
	if (old == mode)
		goto unlock;
	if (old == PARP_EFFECTIVE_TIER_OFF &&
	    mode != PARP_EFFECTIVE_TIER_OFF) {
		if (parp_tier_state_epoch == U8_MAX) {
			error = -EOVERFLOW;
			goto unlock;
		}
		WRITE_ONCE(parp_tier_state_epoch,
			   parp_tier_state_epoch + 1);
		atomic_set(&parp_tier_state_fault, 0);
		WRITE_ONCE(parp_tier_mode, mode);
		static_branch_enable(&parp_effective_tier_enabled);
	} else if (mode == PARP_EFFECTIVE_TIER_OFF) {
		static_branch_disable(&parp_effective_tier_enabled);
		WRITE_ONCE(parp_tier_mode, mode);
	} else {
		error = -EBUSY;
		goto unlock;
	}
	synchronize_rcu();
unlock:
	mutex_unlock(&parp_tier_mode_lock);
	return error;
}

ssize_t parp_effective_tier_format_config(char *buf, size_t size)
{
	struct parp_tier_runtime_config config;

	if (!parp_runtime_config_read(&config))
		return scnprintf(buf, size, "invalid 1\n");
	return scnprintf(buf, size,
		"model GLOBAL_REUSE_MODEL\nmodel_type %s\nmodel_version %u\n"
		"model_checksum %s\nmodel_provenance %s\n"
		"feature_schema_version %u\nconfig_sequence %u\n"
		"metadata_reservation_requested %u\nmetadata_ready %u\n"
		"metadata_payload_bytes %zu\n"
		"cold_threshold %d\nhot_threshold_1 %d\n"
		"hot_threshold_2 %d\nhot_threshold_3 %d\n"
		"max_upgrade_tiers %u\n"
		"max_downgrade_tiers %u\nrequire_two_cold %u\n"
		"upgrade_batch_pages %lu\nupgrade_epoch_pages %lu\n"
		"upgrade_ratio_permyriad %u\n"
		"downgrade_batch_pages %lu\ndowngrade_epoch_pages %lu\n"
		"downgrade_ratio_permyriad %u\nexperiment_id %llu\n"
		"session_id %llu\n",
		PARP_TIER_MODEL_TYPE, PARP_TIER_MODEL_VERSION,
		PARP_TIER_MODEL_CHECKSUM, PARP_TIER_MODEL_PROVENANCE,
		PARP_TIER_SCHEMA_VERSION,
		config.sequence, READ_ONCE(parp_tier_metadata_requested),
		READ_ONCE(parp_tier_metadata_ready),
		parp_effective_tier_metadata_size(), config.policy.cold_threshold, /* #lzx */
		config.policy.hot_threshold_1,
		config.policy.hot_threshold_2,
		config.policy.hot_threshold_3,
		config.policy.max_upgrade_tiers,
		config.policy.max_downgrade_tiers,
		config.policy.require_two_cold,
		config.upgrade_batch_pages, config.upgrade_epoch_pages,
		config.upgrade_ratio_permyriad,
		config.downgrade_batch_pages, config.downgrade_epoch_pages,
		config.downgrade_ratio_permyriad, config.experiment_id,
		config.session_id);
}

int parp_effective_tier_set_config(const char *buf)
{
	struct parp_tier_runtime_config next;
	unsigned int max_upgrade;
	unsigned int require_two_cold;
	unsigned long long experiment_id;
	unsigned long long session_id;
	u32 sequence;
	int matched;

	if (!parp_runtime_config_read(&next))
		return -EAGAIN;
	matched = sscanf(buf, "%d %d %d %d %u %u %lu %lu %u %lu %lu %u %llu %llu",
		&next.policy.cold_threshold, &next.policy.hot_threshold_1,
		&next.policy.hot_threshold_2, &next.policy.hot_threshold_3,
		&max_upgrade,
		&require_two_cold, &next.upgrade_batch_pages,
		&next.upgrade_epoch_pages, &next.upgrade_ratio_permyriad,
		&next.downgrade_batch_pages, &next.downgrade_epoch_pages,
		&next.downgrade_ratio_permyriad, &experiment_id, &session_id);
	if (matched != 14)
		return -EINVAL;
	if (next.policy.cold_threshold >= next.policy.hot_threshold_1 ||
	    next.policy.hot_threshold_1 >= next.policy.hot_threshold_2 ||
	    next.policy.hot_threshold_2 >= next.policy.hot_threshold_3 ||
	    max_upgrade < 1 || max_upgrade > 3 || require_two_cold > 1 ||
	    !next.upgrade_batch_pages || !next.upgrade_epoch_pages ||
	    !next.downgrade_batch_pages || !next.downgrade_epoch_pages ||
	    !next.upgrade_ratio_permyriad ||
	    next.upgrade_ratio_permyriad > 10000 ||
	    !next.downgrade_ratio_permyriad ||
	    next.downgrade_ratio_permyriad > 10000)
		return -ERANGE;
	next.policy.max_upgrade_tiers = max_upgrade;
	next.policy.max_downgrade_tiers = 1;
	next.policy.require_two_cold = require_two_cold;
	next.experiment_id = experiment_id;
	next.session_id = session_id;
	mutex_lock(&parp_tier_mode_lock);
	if (READ_ONCE(parp_tier_mode) != PARP_EFFECTIVE_TIER_OFF) {
		mutex_unlock(&parp_tier_mode_lock);
		return -EBUSY;
	}
	mutex_lock(&parp_tier_config_lock);
	sequence = READ_ONCE(parp_tier_config.sequence);
	WRITE_ONCE(parp_tier_config.sequence, sequence + 1);
	/* Publish the odd sequence before replacing the config payload. */
	smp_wmb();
	next.sequence = sequence + 1;
	parp_tier_config = next;
	/* Publish the config payload before making the sequence even. */
	smp_wmb();
	WRITE_ONCE(parp_tier_config.sequence, sequence + 2);
	mutex_unlock(&parp_tier_config_lock);
	mutex_unlock(&parp_tier_mode_lock);
	return 0;
}

static ssize_t parp_format_histogram(char *buf, size_t size, ssize_t offset,
		const char *name,
		atomic64_t histogram[PARP_TIER_LATENCY_BUCKETS])
{
	int bin;

	if (offset >= size)
		return offset;
	offset += scnprintf(buf + offset, size - offset,
			    "%s", name);
	for (bin = 0; bin < PARP_TIER_LATENCY_BUCKETS && offset < size;
	     bin++)
		offset += scnprintf(buf + offset, size - offset, "%c%lld",
				    bin ? ',' : ' ',
				    atomic64_read(&histogram[bin]));
	if (offset < size)
		offset += scnprintf(buf + offset, size - offset, "\n");
	return offset;
}

ssize_t parp_effective_tier_format_stats(char *buf, size_t size)
{
	ssize_t len;

	len = scnprintf(buf, size,
		"mode %u\napply_compiled %u\npage_ext_bytes %zu\n"
		"state_epoch %u\ncandidates %lld\ntier_gate_decisions %lld\n"
		"candidate_pages %lld\n"
		"state_fault %u\n"
		"scores %lld\nmetadata_missing %lld\nstate_unstable %lld\n"
		"model_invalid %lld\nkeep_reclaim_pages %lld\n"
		"upgrade_pages %lld\nkeep_protect_pages %lld\n"
		"downgrade_pages %lld\nspecial_protect_pages %lld\n"
		"policy_promotions %lld\nnative_tier_promotions %lld\n"
		"native_generation_moves %lld\nscore_time_ns_total %lld\n"
		"score_time_ns_max %lld\ndecision_time_ns_total %lld\n"
		"decision_time_ns_max %lld\nlock_time_ns_total %lld\n"
		"lock_time_ns_max %lld\ntrace_decisions %lld\n"
		"trace_accesses %lld\ntrace_outcomes %lld\ntrace_batches %lld\n"
		"trace_locks %lld\n",
		parp_effective_tier_get_mode(),
		IS_ENABLED(CONFIG_PARP_EFFECTIVE_TIER_EXPERIMENTAL_APPLY),
		parp_effective_tier_metadata_size(),
		READ_ONCE(parp_tier_state_epoch),
		atomic64_read(&parp_tier_stats.candidates), /* #lzx */
		/* Explicit coverage alias: same counter, never a sampled estimate. */
		atomic64_read(&parp_tier_stats.candidates),
		atomic64_read(&parp_tier_stats.candidate_pages),
		atomic_read(&parp_tier_state_fault),
		atomic64_read(&parp_tier_stats.scores),
		atomic64_read(&parp_tier_stats.metadata_missing),
		atomic64_read(&parp_tier_stats.state_unstable),
		atomic64_read(&parp_tier_stats.model_invalid),
		atomic64_read(&parp_tier_stats.action_pages[0]),
		atomic64_read(&parp_tier_stats.action_pages[1]),
		atomic64_read(&parp_tier_stats.action_pages[2]),
		atomic64_read(&parp_tier_stats.action_pages[3]),
		atomic64_read(&parp_tier_stats.action_pages[4]),
		atomic64_read(&parp_tier_stats.policy_promotions),
		atomic64_read(&parp_tier_stats.native_tier_promotions),
		atomic64_read(&parp_tier_stats.native_generation_moves),
		atomic64_read(&parp_tier_stats.score_time_ns_total),
		atomic64_read(&parp_tier_stats.score_time_ns_max),
		atomic64_read(&parp_tier_stats.decision_time_ns_total),
		atomic64_read(&parp_tier_stats.decision_time_ns_max),
		atomic64_read(&parp_tier_stats.lock_time_ns_total),
		atomic64_read(&parp_tier_stats.lock_time_ns_max),
		atomic64_read(&parp_tier_stats.trace_decisions),
		atomic64_read(&parp_tier_stats.trace_accesses),
		atomic64_read(&parp_tier_stats.trace_outcomes),
		atomic64_read(&parp_tier_stats.trace_batches),
		atomic64_read(&parp_tier_stats.trace_locks));
	len = parp_format_histogram(buf, size, len, "score_hist_log2_ns",
				    parp_tier_stats.score_hist);
	len = parp_format_histogram(buf, size, len, "decision_hist_log2_ns",
				    parp_tier_stats.decision_hist);
	len = parp_format_histogram(buf, size, len, "lock_hist_log2_ns",
				    parp_tier_stats.lock_hist);
	return min_t(ssize_t, len, size);
}
