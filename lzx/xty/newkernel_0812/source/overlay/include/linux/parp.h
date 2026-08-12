/* SPDX-License-Identifier: GPL-2.0 */
#ifndef _LINUX_PARP_H
#define _LINUX_PARP_H

#include <linux/types.h>
#include <linux/jump_label.h>

struct folio;
struct page;
struct lruvec;
struct scan_control;
struct mem_cgroup;
struct damon_ctx;
struct damon_target;
struct damon_region;
struct page_ext_operations;

enum parp_frontier_mode {
	PARP_FRONTIER_OFF,
	PARP_FRONTIER_SHADOW,
	PARP_FRONTIER_APPLY,
};

enum parp_frontier_reason {
	PARP_FRONTIER_SCORE,
	PARP_FRONTIER_DISABLED,
	PARP_FRONTIER_NOT_TARGET_MEMCG,
	PARP_FRONTIER_PRESSURE_BYPASS,
	PARP_FRONTIER_NO_APP_CONTEXT,
	PARP_FRONTIER_NO_EFFICIENCY,
	PARP_FRONTIER_NO_DEMAND,
	PARP_FRONTIER_NO_CAPACITY,
	PARP_FRONTIER_NOT_FRONTIER,
	PARP_FRONTIER_METADATA_MISSING,
	PARP_FRONTIER_MODEL_INVALID,
	PARP_FRONTIER_BELOW_THRESHOLD,
	PARP_FRONTIER_BUDGET,
	PARP_FRONTIER_REPEAT_EPOCH,
	PARP_FRONTIER_EXPIRED,
};

struct parp_frontier_scan_ctx {
	bool valid;
	u8 type;
	u8 mode;
	u8 reason;
	u32 model_version;
	u32 threshold;
	u32 efficiency_q15;
	u32 app_reentry_q15;
	u32 app_id;
	u32 nid;
	u32 source_generation;
	u64 domain_id;
	u64 epoch_id;
	u64 batch_id;
	u64 foreground_epoch_id;
	u64 valid_until_ns;
	unsigned long source_seq;
	unsigned long frontier_seq;
	unsigned long remaining_demand;
	unsigned long frontier_headroom;
	unsigned long app_budget;
	unsigned long batch_budget;
	unsigned long epoch_budget;
	unsigned long batch_used;
	unsigned long app_used;
};

#define PARP_TIER_SCALE		256
#define PARP_MAX_TIER		3
#define PARP_TIER_FEATURES	6
#define PARP_TIER_BINS		6
#define PARP_TIER_MODEL_VERSION	1U
#define PARP_TIER_MODEL_TYPE	"pairwise_linear_ranker"
/* SHA-256 of the canonical embedded scorer parameters, not training data. */
#define PARP_TIER_MODEL_CHECKSUM						\
	"1d5cc9918ae0e2caea316d36841fd61bef562e68f0820188c1cc0a6195f1fa5e"
#define PARP_TIER_MODEL_PROVENANCE	"ENGINEERING_FIXTURE_UNTRAINED"
#define PARP_TIER_TRACE_MODEL_TYPE_LEN	32
#define PARP_TIER_TRACE_CHECKSUM_LEN	65
#define PARP_TIER_TRACE_PROVENANCE_LEN	32
#define PARP_TIER_PRESSURE_POLICY_VERSION	1U
#define PARP_TIER_PRESSURE_PROVENANCE \
	"ENGINEERING_PRESSURE_POLICY_UNVALIDATED"
#define PARP_TIER_TRACE_PRESSURE_PROVENANCE_LEN	48 /* #lzx */
#define PARP_TIER_RUNTIME_PAIRWISE_COMPARISON	0
#define PARP_TIER_RUNTIME_CANDIDATE_SORTING	0
#define PARP_TIER_RUNTIME_SIGMOID		0

enum parp_rank_score_bin {
	PARP_RANK_SCORE_COLD,
	PARP_RANK_SCORE_NEUTRAL,
	PARP_RANK_SCORE_HOT_1,
	PARP_RANK_SCORE_HOT_2,
	PARP_RANK_SCORE_HOT_3,
	PARP_RANK_SCORE_INVALID = 0xff,
};

enum parp_tier_scorer_kind {
	PARP_TIER_SCORER_PAIRWISE_LINEAR,
	PARP_TIER_SCORER_RECENCY_BASELINE,
	PARP_TIER_SCORER_RANDOM_BASELINE,
};

enum parp_effective_tier_mode {
	PARP_EFFECTIVE_TIER_OFF,
	PARP_EFFECTIVE_TIER_SHADOW,
	PARP_EFFECTIVE_TIER_PROTECT_ONLY,
	PARP_EFFECTIVE_TIER_BIDIRECTIONAL,
	PARP_EFFECTIVE_TIER_RANDOM_MATCHED,
	PARP_EFFECTIVE_TIER_RECENCY_BASELINE,
};

enum parp_tier_action {
	PARP_TIER_KEEP_RECLAIM,
	PARP_TIER_PREDICTIVE_UPGRADE,
	PARP_TIER_KEEP_PROTECT,
	PARP_TIER_PREDICTIVE_DOWNGRADE,
	PARP_TIER_SPECIAL_NATIVE_PROTECT,
};

enum parp_tier_bypass_reason {
	PARP_TIER_BYPASS_NONE,
	PARP_TIER_BYPASS_DISABLED,
	PARP_TIER_BYPASS_MODEL_INVALID,
	PARP_TIER_BYPASS_METADATA_MISSING,
	PARP_TIER_BYPASS_STATE_UNSTABLE,
	PARP_TIER_BYPASS_NOT_BOUNDARY,
	PARP_TIER_BYPASS_STRONG_NATIVE,
	PARP_TIER_BYPASS_UPGRADE_BUDGET,
	PARP_TIER_BYPASS_DOWNGRADE_BUDGET,
	PARP_TIER_BYPASS_REPEAT_UPGRADE,
	PARP_TIER_BYPASS_REPEAT_DOWNGRADE,
	PARP_TIER_BYPASS_PRESSURE,
	PARP_TIER_BYPASS_NO_PROGRESS,
	PARP_TIER_BYPASS_GENERATION_RACE,
	PARP_TIER_BYPASS_RANDOM_UNSELECTED,
	PARP_TIER_BYPASS_NR,
};

/*
 * This is intentionally a local reclaim-path classification.  It must not
 * incorporate PSI, cgroup files, or any user-space signal while lru_lock is
 * held.  The aliases keep the pre-existing scan-budget interface source and
 * ABI compatible while giving effective-tier traces the Phase-F vocabulary.
 */
enum parp_tier_reclaim_context {
	PARP_TIER_RECLAIM_UNKNOWN,
	PARP_TIER_RECLAIM_KSWAPD,
	PARP_TIER_RECLAIM_DIRECT,
	PARP_TIER_RECLAIM_MEMCG,
	PARP_TIER_RECLAIM_PROACTIVE_MEMCG,
}; /* #lzx */

enum parp_tier_outcome {
	PARP_TIER_OUTCOME_RECLAIMED,
	PARP_TIER_OUTCOME_PUTBACK,
	PARP_TIER_OUTCOME_ACTIVATED,
	PARP_TIER_OUTCOME_DEMOTE_ATTEMPT,
};

enum parp_tier_lock_scope {
	PARP_TIER_LOCK_SCAN_FOLIOS,
	PARP_TIER_LOCK_BATCH,
};

struct parp_tier_lock_measurement {
	u64 irq_disabled_started_ns;
	u64 lock_attempt_started_ns;
	u64 acquired_ns;
	u64 releasing_ns;
	u64 irq_disabled_ended_ns;
	u64 experiment_id;
	u64 session_id;
	u8 mode;
	bool irq_disabled_measured;
};

enum parp_access_event {
	PARP_ACCESS_PTE_YOUNG,
	PARP_ACCESS_MARK_ACCESSED,
	PARP_ACCESS_FD_REFERENCE,
	PARP_ACCESS_UNTRUSTED_MARK,
	PARP_ACCESS_POLICY_MARK,
	PARP_NATIVE_TIER_PROMOTION,
	PARP_NATIVE_GENERATION_MOVE,
	PARP_POLICY_PROMOTION,
	PARP_OTHER_MOVE,
	PARP_ACCESS_EVENT_NR,
};

struct parp_tier_policy {
	s32 cold_threshold;
	s32 hot_threshold_1;
	s32 hot_threshold_2;
	s32 hot_threshold_3;
	u8 max_upgrade_tiers;
	u8 max_downgrade_tiers;
	bool require_two_cold;
};

struct parp_tier_decision {
	s64 features[PARP_TIER_FEATURES];
	s32 reuse_score;
	s32 raw_delta_tier_q8;
	s32 delta_tier_q8;
	s32 fixed_delta_q8;
	s32 binary_bypass_delta_q8;
	s32 pressure_aware_delta_q8; /* #lzx */
	s32 effective_tier_q8;
	s32 cold_threshold;
	s32 hot_threshold_1;
	s32 hot_threshold_2;
	s32 hot_threshold_3;
	u64 score_duration_ns;
	u64 decision_duration_ns;
	unsigned long folio_nr_pages;
	u8 native_tier;
	u8 native_tier_idx;
	u8 generation_index;
	u8 action;
	u8 bypass;
	u8 rank_score_bin;
	u8 scorer_kind;
	u8 pressure_level_kernel;
	u8 reclaim_context;
	u8 pressure_bypass_reason;
	u16 page_state_sequence;
	u32 pressure_policy_version;
	bool evaluated;
	bool model_valid;
	bool features_valid;
	bool native_protect;
	bool effective_protect;
	bool fixed_effective_protect;
	bool pressure_aware_effective_protect; /* #lzx */
	bool special_native_protect;
	bool actual_tier_protect;
	bool reservation_active;
	bool reservation_upgrade;
};

struct parp_tier_scan_ctx {
	u64 experiment_id;
	u64 session_id;
	u64 batch_id;
	u64 epoch_id;
	u64 memcg_id;
	u64 model_time_ns;
	u64 nr_to_reclaim;
	u64 nr_reclaimed_before;
	u64 epoch_reclaimed_pages; /* #lzx */
	unsigned long source_seq;
	unsigned long candidate_pages;
	unsigned long upgrade_pages;
	unsigned long downgrade_pages;
	unsigned long batch_scanned_pages;
	unsigned long batch_isolated_pages;
	unsigned long batch_reclaimed_pages; /* #lzx */
	u32 nid;
	u32 config_sequence;
	s32 reclaim_priority;
	u16 epoch_tag;
	u8 type;
	u8 mode;
	u8 run_epoch;
	u8 pressure_level_kernel;
	u8 reclaim_context;
	u8 pressure_bypass_reason;
	u16 consecutive_no_progress_batches; /* #lzx */
	bool enabled;
	bool severe_pressure;
	bool no_progress;
	bool rcu_held;
};

struct parp_tier_state_snapshot {
	u32 last_access_ms;
	u32 previous_interval_ms;
	u32 reuse_interval_ema_ms;
	u32 generation_enter_ms;
	u16 last_upgrade_epoch;
	u16 last_downgrade_epoch;
	u32 lifetime_epoch;
	u8 access_ema_q8;
	u8 consecutive_candidates;
	u8 generation;
	u8 pending_action;
	u8 state_epoch;
	u16 state_sequence;
	bool uncertain;
};

#ifdef CONFIG_PARP_EFFECTIVE_TIER
extern struct static_key_false parp_effective_tier_enabled;
extern struct static_key_false parp_effective_tier_lock_observe;
extern struct page_ext_operations parp_effective_tier_page_ext_ops;

static inline bool parp_effective_tier_tracking_active(void)
{
	return static_branch_unlikely(&parp_effective_tier_enabled);
}

void __parp_effective_tier_note_access(struct folio *folio,
		enum parp_access_event event);
void __parp_effective_tier_note_move(struct folio *folio,
		enum parp_access_event event, int generation);
void parp_effective_tier_page_alloc(struct page *page, unsigned int order);
void __parp_effective_tier_mglru_state_change(void);
void __parp_effective_tier_prepare(struct parp_tier_scan_ctx *ctx,
		struct lruvec *lruvec, int type, int reclaim_priority,
		unsigned long nr_to_reclaim, unsigned long nr_reclaimed,
		bool is_kswapd, bool is_memcg_reclaim, bool proactive_reclaim); /* #lzx */
void __parp_effective_tier_decide(struct parp_tier_scan_ctx *ctx,
		struct lruvec *lruvec, struct folio *folio, int native_tier,
		int tier_idx, bool special_native_protect,
		struct parp_tier_decision *decision);
void __parp_effective_tier_finish(struct parp_tier_scan_ctx *ctx,
		struct lruvec *lruvec, struct folio *folio,
		struct parp_tier_decision *decision, bool sort_result,
		bool isolate_attempted, bool isolate_result);
void __parp_effective_tier_batch_finish(struct parp_tier_scan_ctx *ctx,
		unsigned long isolated);
void __parp_effective_tier_batch_reclaim(struct parp_tier_scan_ctx *ctx,
		unsigned long reclaimed); /* #lzx */
void __parp_effective_tier_feedback(struct lruvec *lruvec, int type,
		u8 run_epoch, unsigned long isolated, unsigned long reclaimed);
void __parp_effective_tier_outcome(struct folio *folio,
		enum parp_tier_outcome outcome);
void __parp_effective_tier_lock_start(
		struct parp_tier_lock_measurement *measurement);
void __parp_effective_tier_lock_acquired(
		struct parp_tier_lock_measurement *measurement);
void __parp_effective_tier_lock_releasing(
		struct parp_tier_lock_measurement *measurement);
void __parp_effective_tier_lock_unlocked(
		struct parp_tier_lock_measurement *measurement);
void __parp_effective_tier_lock_finish(
		struct parp_tier_lock_measurement *measurement,
		enum parp_tier_lock_scope scope, int nid);

enum parp_effective_tier_mode parp_effective_tier_get_mode(void);
int parp_effective_tier_set_mode(enum parp_effective_tier_mode mode);
bool parp_effective_tier_metadata_ready(void); /* #lzx */
ssize_t parp_effective_tier_format_stats(char *buf, size_t size);
ssize_t parp_effective_tier_format_config(char *buf, size_t size);
int parp_effective_tier_set_config(const char *buf);
bool parp_effective_tier_get_lock_observe(void);
int parp_effective_tier_set_lock_observe(bool enabled);

bool parp_access_event_is_real(enum parp_access_event event);
bool parp_effective_tier_score_values(
		const s64 values[PARP_TIER_FEATURES], s32 *score);
s32 parp_score_to_delta_q8(s32 score,
		const struct parp_tier_policy *policy);
s32 parp_effective_tier_q8(int native_tier, s32 delta_tier_q8);
void parp_effective_tier_classify(s32 score, bool model_valid,
		int native_tier, int tier_idx, bool special_native_protect,
		const struct parp_tier_policy *policy,
		struct parp_tier_decision *decision);
bool parp_effective_tier_actual_protect(
		enum parp_effective_tier_mode mode,
		const struct parp_tier_decision *decision);
bool parp_effective_tier_budget_allows(unsigned long used,
		unsigned long candidates, unsigned long pages,
		unsigned long absolute_limit, u32 ratio_permyriad);
bool parp_effective_tier_random_claim(u64 random_value,
		unsigned long selected, unsigned long target,
		unsigned long seen, unsigned long eligible);
bool parp_effective_tier_upgrade_gate(bool severe_pressure,
		bool no_progress, enum parp_tier_bypass_reason *bypass);
u8 parp_effective_tier_pressure_level(int reclaim_priority,
		bool no_progress, unsigned long nr_to_reclaim,
		unsigned long nr_reclaimed);
s32 parp_effective_tier_pressure_delta_q8(s32 delta_q8,
		u8 pressure_level_kernel); /* #lzx */
s32 parp_effective_tier_recency_score(u32 access_age_ms,
		const struct parp_tier_policy *policy);
int parp_effective_tier_next_generation(int generation);
unsigned long parp_effective_tier_policy_flags(unsigned long old_flags,
		int new_generation);
bool parp_effective_tier_claim_epoch(struct folio *folio, bool upgrade,
		u16 epoch_tag, enum parp_tier_bypass_reason *bypass);
bool parp_effective_tier_candidate_access_trace_eligible(bool real_access,
		u8 candidate_count); /* #lzx */
u64 parp_effective_tier_cookie(struct folio *folio);
u32 parp_effective_tier_elapsed_ms(u32 now, u32 then, bool *valid);
size_t parp_effective_tier_metadata_size(void);
bool parp_effective_tier_state_snapshot(struct folio *folio,
		struct parp_tier_state_snapshot *snapshot);
bool parp_effective_tier_revalidate(struct folio *folio,
		u16 state_sequence);
bool parp_effective_tier_commit_revalidate(
		const struct parp_tier_scan_ctx *ctx, struct folio *folio,
		u16 state_sequence);

static inline void parp_effective_tier_note_access(
		struct folio *folio, enum parp_access_event event)
{
	if (static_branch_unlikely(&parp_effective_tier_enabled))
		__parp_effective_tier_note_access(folio, event);
}

static inline void parp_effective_tier_note_move(
		struct folio *folio, enum parp_access_event event, int generation)
{
	if (static_branch_unlikely(&parp_effective_tier_enabled))
		__parp_effective_tier_note_move(folio, event, generation);
}

static inline void parp_effective_tier_mglru_state_change(void)
{
	if (static_branch_unlikely(&parp_effective_tier_enabled))
		__parp_effective_tier_mglru_state_change();
}

static inline void parp_effective_tier_prepare(
		struct parp_tier_scan_ctx *ctx, struct lruvec *lruvec,
		int type, int reclaim_priority, unsigned long nr_to_reclaim,
		unsigned long nr_reclaimed, bool is_kswapd,
		bool is_memcg_reclaim, bool proactive_reclaim)
{
	if (static_branch_unlikely(&parp_effective_tier_enabled))
		__parp_effective_tier_prepare(ctx, lruvec, type,
				reclaim_priority, nr_to_reclaim, nr_reclaimed,
		is_kswapd, is_memcg_reclaim, proactive_reclaim); /* #lzx */
	else
		ctx->enabled = false;
}

static inline void parp_effective_tier_decide(
		struct parp_tier_scan_ctx *ctx, struct lruvec *lruvec,
		struct folio *folio, int native_tier, int tier_idx,
		bool special_native_protect, struct parp_tier_decision *decision)
{
	if (ctx->enabled)
		__parp_effective_tier_decide(ctx, lruvec, folio, native_tier,
				tier_idx, special_native_protect, decision);
	else
		decision->evaluated = false;
}

static inline void parp_effective_tier_finish(
		struct parp_tier_scan_ctx *ctx, struct lruvec *lruvec,
		struct folio *folio, struct parp_tier_decision *decision,
		bool sort_result, bool isolate_attempted, bool isolate_result)
{
	if (decision->evaluated)
		__parp_effective_tier_finish(ctx, lruvec, folio, decision,
				sort_result, isolate_attempted, isolate_result);
}

static inline void parp_effective_tier_feedback(struct lruvec *lruvec,
		int type, u8 run_epoch, unsigned long isolated,
		unsigned long reclaimed)
{
	if (static_branch_unlikely(&parp_effective_tier_enabled))
		__parp_effective_tier_feedback(lruvec, type, run_epoch, isolated,
				reclaimed);
}

static inline void parp_effective_tier_batch_finish(
		struct parp_tier_scan_ctx *ctx, unsigned long isolated)
{
	if (ctx->enabled)
		__parp_effective_tier_batch_finish(ctx, isolated);
}

static inline void parp_effective_tier_batch_reclaim(
		struct parp_tier_scan_ctx *ctx, unsigned long reclaimed)
{
	if (ctx->enabled)
		__parp_effective_tier_batch_reclaim(ctx, reclaimed); /* #lzx */
}

static inline void parp_effective_tier_outcome(struct folio *folio,
		enum parp_tier_outcome outcome)
{
	if (static_branch_unlikely(&parp_effective_tier_enabled))
		__parp_effective_tier_outcome(folio, outcome);
}

static inline void parp_effective_tier_lock_start(
		struct parp_tier_lock_measurement *measurement)
{
	if (static_branch_unlikely(&parp_effective_tier_enabled))
		__parp_effective_tier_lock_start(measurement);
	else if (static_branch_unlikely(&parp_effective_tier_lock_observe))
		__parp_effective_tier_lock_start(measurement);
	else
		*measurement = (struct parp_tier_lock_measurement) { };
}

static inline void parp_effective_tier_lock_acquired(
		struct parp_tier_lock_measurement *measurement)
{
	if (measurement->irq_disabled_started_ns)
		__parp_effective_tier_lock_acquired(measurement);
}

static inline void parp_effective_tier_lock_releasing(
		struct parp_tier_lock_measurement *measurement)
{
	if (measurement->acquired_ns)
		__parp_effective_tier_lock_releasing(measurement);
}

static inline void parp_effective_tier_lock_unlocked(
		struct parp_tier_lock_measurement *measurement)
{
	if (measurement->releasing_ns)
		__parp_effective_tier_lock_unlocked(measurement);
}

static inline void parp_effective_tier_lock_finish(
		struct parp_tier_lock_measurement *measurement,
		enum parp_tier_lock_scope scope, int nid)
{
	if (measurement->irq_disabled_ended_ns)
		__parp_effective_tier_lock_finish(measurement, scope, nid);
}
#else
static inline bool parp_effective_tier_tracking_active(void)
{
	return false;
}

static inline bool parp_effective_tier_candidate_access_trace_eligible(
		bool real_access, u8 candidate_count) /* #lzx */
{
	return false;
}

static inline bool parp_effective_tier_commit_revalidate(
		const struct parp_tier_scan_ctx *ctx, struct folio *folio,
		u16 state_sequence)
{
	return false;
}

static inline void parp_effective_tier_note_access(
		struct folio *folio, enum parp_access_event event)
{
}
static inline void parp_effective_tier_note_move(
		struct folio *folio, enum parp_access_event event, int generation)
{
}
static inline void parp_effective_tier_page_alloc(struct page *page,
		unsigned int order)
{
}
static inline void parp_effective_tier_mglru_state_change(void)
{
}
static inline void parp_effective_tier_prepare(
		struct parp_tier_scan_ctx *ctx, struct lruvec *lruvec,
		int type, int reclaim_priority, unsigned long nr_to_reclaim,
		unsigned long nr_reclaimed, bool is_kswapd,
		bool is_memcg_reclaim, bool proactive_reclaim) /* #lzx */
{
	ctx->enabled = false;
}
static inline void parp_effective_tier_decide(
		struct parp_tier_scan_ctx *ctx, struct lruvec *lruvec,
		struct folio *folio, int native_tier, int tier_idx,
		bool special_native_protect, struct parp_tier_decision *decision)
{
	decision->evaluated = false;
}
static inline void parp_effective_tier_finish(
		struct parp_tier_scan_ctx *ctx, struct lruvec *lruvec,
		struct folio *folio, struct parp_tier_decision *decision,
		bool sort_result, bool isolate_attempted, bool isolate_result)
{
}
static inline void parp_effective_tier_feedback(struct lruvec *lruvec,
		int type, u8 run_epoch, unsigned long isolated,
		unsigned long reclaimed)
{
}
static inline void parp_effective_tier_batch_finish(
		struct parp_tier_scan_ctx *ctx, unsigned long isolated)
{
}
static inline void parp_effective_tier_batch_reclaim(
		struct parp_tier_scan_ctx *ctx, unsigned long reclaimed)
{
} /* #lzx */
static inline void parp_effective_tier_outcome(struct folio *folio,
		enum parp_tier_outcome outcome)
{
}
static inline void parp_effective_tier_lock_start(
		struct parp_tier_lock_measurement *measurement)
{
	*measurement = (struct parp_tier_lock_measurement) { };
}
static inline void parp_effective_tier_lock_acquired(
		struct parp_tier_lock_measurement *measurement)
{
}
static inline void parp_effective_tier_lock_releasing(
		struct parp_tier_lock_measurement *measurement)
{
}
static inline void parp_effective_tier_lock_unlocked(
		struct parp_tier_lock_measurement *measurement)
{
}
static inline void parp_effective_tier_lock_finish(
		struct parp_tier_lock_measurement *measurement,
		enum parp_tier_lock_scope scope, int nid)
{
}
#endif

enum parp_mode {
	PARP_MODE_DISABLED,
	PARP_MODE_OBSERVE,
	PARP_MODE_APPLY,
};

enum parp_action {
	PARP_ACTION_NATIVE,
	PARP_ACTION_PROTECT,
	PARP_ACTION_RECLAIM_BIAS,
	PARP_ACTION_PREFETCH_CANDIDATE,
};

enum parp_fallback_reason {
	PARP_FALLBACK_NONE,
	PARP_FALLBACK_DISABLED,
	PARP_FALLBACK_NO_DOMAIN,
	PARP_FALLBACK_NO_BINDING,
	PARP_FALLBACK_EXPIRED,
	PARP_FALLBACK_MODEL_VERSION,
	PARP_FALLBACK_NO_EVIDENCE,
	PARP_FALLBACK_UNSAFE_FOLIO,
	PARP_FALLBACK_EVIDENCE_ONLY,
	PARP_FALLBACK_NR,
};

struct parp_decision {
	enum parp_action original_action;
	enum parp_action proposed_action;
	enum parp_action applied_action;
	enum parp_fallback_reason fallback;
	u16 score_q15;
};

struct parp_reclaim_ctx {
	u64 domain_id;
	const void *snapshot;
	enum parp_mode mode;
	bool rcu_held;
};

enum parp_scan_budget_mode {
	PARP_SCAN_BUDGET_DISABLED,
	PARP_SCAN_BUDGET_OBSERVE,
	PARP_SCAN_BUDGET_APPLY,
};

enum parp_reclaim_scope {
	PARP_RECLAIM_SCOPE_UNKNOWN,
	PARP_RECLAIM_SCOPE_GLOBAL_KSWAPD,
	PARP_RECLAIM_SCOPE_GLOBAL_DIRECT,
	PARP_RECLAIM_SCOPE_TARGET_MEMCG,
	PARP_RECLAIM_SCOPE_PROACTIVE_MEMCG,
	PARP_RECLAIM_SCOPE_MEMCG_HIGH,
};

enum parp_pressure_level {
	PARP_PRESSURE_LOW,
	PARP_PRESSURE_MEDIUM,
	PARP_PRESSURE_HIGH,
	PARP_PRESSURE_CRITICAL,
	/* Legacy names used by the scan-budget path. */
	PARP_PRESSURE_NORMAL = PARP_PRESSURE_LOW,
	PARP_PRESSURE_ELEVATED = PARP_PRESSURE_MEDIUM,
	PARP_PRESSURE_EMERGENCY = PARP_PRESSURE_CRITICAL,
}; /* #lzx */

enum parp_scan_budget_reason {
	PARP_SCAN_REASON_NATIVE,
	PARP_SCAN_REASON_FOREGROUND,
	PARP_SCAN_REASON_HIGH_PRIOR,
	PARP_SCAN_REASON_MEDIUM_PRIOR,
	PARP_SCAN_REASON_LOW_PRIOR,
	PARP_SCAN_REASON_NO_BIND,
	PARP_SCAN_REASON_STALE_BIND,
	PARP_SCAN_REASON_NO_PRIOR,
	PARP_SCAN_REASON_EXPIRED_PRIOR,
	PARP_SCAN_REASON_STALE_GENERATION,
	PARP_SCAN_REASON_MODEL_VERSION,
	PARP_SCAN_REASON_NOT_TARGET_MEMCG,
	PARP_SCAN_REASON_PRESSURE_BYPASS,
	PARP_SCAN_REASON_CIRCUIT_BREAKER,
	PARP_SCAN_REASON_CLAMP_MIN,
	PARP_SCAN_REASON_CLAMP_MAX,
	PARP_SCAN_REASON_APPLY_DOMAIN,
	PARP_SCAN_REASON_DISABLED,
};

struct parp_scan_budget_decision {
	bool valid;
	u64 native_nr_to_scan;
	u64 proposed_nr_to_scan;
	u64 applied_nr_to_scan;
	u32 multiplier_q15;
	u16 confidence_q15;
	enum parp_scan_budget_reason reason;
	u32 reason_flags;
	u64 budget_sequence;
};

#ifdef CONFIG_PARP
void parp_adapter_prepare(struct parp_reclaim_ctx *ctx,
			  struct lruvec *lruvec, struct scan_control *sc);
struct parp_decision
parp_adapter_score_folio(struct parp_reclaim_ctx *ctx, struct folio *folio,
			 int type, int generation);
void parp_adapter_apply_decision(struct parp_reclaim_ctx *ctx,
				 struct folio *folio,
				 struct parp_decision *decision);
void parp_adapter_finish(struct parp_reclaim_ctx *ctx,
			 unsigned long scanned, unsigned long isolated);
bool parp_adapter_prepare_scan_budget(struct lruvec *lruvec,
				      struct mem_cgroup *target_memcg,
				      bool global_kswapd, bool proactive,
				      int reclaim_priority,
				      unsigned long native_nr_to_scan,
				      struct parp_scan_budget_decision *decision);
unsigned long parp_adapter_apply_scan_budget(
				const struct parp_scan_budget_decision *decision);
#ifdef CONFIG_PARP_DAMON_ALIGNMENT
void parp_damon_aggregate(struct damon_ctx *ctx, struct damon_target *target,
			  struct damon_region *region);
#else
static inline void parp_damon_aggregate(struct damon_ctx *ctx,
					struct damon_target *target,
					struct damon_region *region)
{
}
#endif

#ifdef CONFIG_PARP_FRONTIER_SCORE
extern struct static_key_false parp_frontier_enabled;
extern struct page_ext_operations parp_frontier_page_ext_ops;

void __parp_frontier_note_access(struct folio *folio, int generation);
void __parp_frontier_prepare(struct parp_frontier_scan_ctx *ctx,
		struct lruvec *lruvec, bool target_memcg, int type,
		int reclaim_priority, int reclaim_idx,
		unsigned long remaining_demand);
void __parp_frontier_consider(struct parp_frontier_scan_ctx *ctx,
		struct lruvec *lruvec, struct folio *folio);
void parp_frontier_feedback(struct lruvec *lruvec, int type,
		unsigned long isolated, unsigned long reclaimed);
enum parp_frontier_mode parp_frontier_get_mode(void);
int parp_frontier_set_mode(enum parp_frontier_mode mode);
ssize_t parp_frontier_format_stats(char *buf, size_t size);

static inline void parp_frontier_note_access(struct folio *folio,
					     int generation)
{
	if (static_branch_unlikely(&parp_frontier_enabled))
		__parp_frontier_note_access(folio, generation);
}

static inline void parp_frontier_prepare(struct parp_frontier_scan_ctx *ctx,
		struct lruvec *lruvec, bool target_memcg, int type,
		int reclaim_priority, int reclaim_idx,
		unsigned long remaining_demand)
{
	if (static_branch_unlikely(&parp_frontier_enabled))
		__parp_frontier_prepare(ctx, lruvec, target_memcg, type,
				reclaim_priority, reclaim_idx, remaining_demand);
	else
		ctx->valid = false;
}

static inline void parp_frontier_consider(struct parp_frontier_scan_ctx *ctx,
					  struct lruvec *lruvec,
					  struct folio *folio)
{
	if (static_branch_unlikely(&parp_frontier_enabled))
		__parp_frontier_consider(ctx, lruvec, folio);
}
#else
static inline void parp_frontier_note_access(struct folio *folio,
					     int generation)
{
}
static inline void parp_frontier_prepare(struct parp_frontier_scan_ctx *ctx,
		struct lruvec *lruvec, bool target_memcg, int type,
		int reclaim_priority, int reclaim_idx,
		unsigned long remaining_demand)
{
	ctx->valid = false;
}
static inline void parp_frontier_consider(struct parp_frontier_scan_ctx *ctx,
					  struct lruvec *lruvec,
					  struct folio *folio)
{
}
static inline void parp_frontier_feedback(struct lruvec *lruvec, int type,
		unsigned long isolated, unsigned long reclaimed)
{
}
#endif
#else
static inline void parp_adapter_prepare(struct parp_reclaim_ctx *ctx,
					struct lruvec *lruvec,
					struct scan_control *sc)
{
	ctx->mode = PARP_MODE_DISABLED;
	ctx->rcu_held = false;
}

static inline struct parp_decision
parp_adapter_score_folio(struct parp_reclaim_ctx *ctx, struct folio *folio,
			 int type, int generation)
{
	return (struct parp_decision) {
		.original_action = PARP_ACTION_NATIVE,
		.proposed_action = PARP_ACTION_NATIVE,
		.applied_action = PARP_ACTION_NATIVE,
		.fallback = PARP_FALLBACK_DISABLED,
	};
}

static inline void
parp_adapter_apply_decision(struct parp_reclaim_ctx *ctx, struct folio *folio,
			    struct parp_decision *decision)
{
}

static inline void parp_adapter_finish(struct parp_reclaim_ctx *ctx,
				       unsigned long scanned,
				       unsigned long isolated)
{
}
static inline bool parp_adapter_prepare_scan_budget(
		struct lruvec *lruvec, struct mem_cgroup *target_memcg,
		bool global_kswapd, bool proactive, int reclaim_priority,
		unsigned long native_nr_to_scan,
		struct parp_scan_budget_decision *decision)
{
	*decision = (struct parp_scan_budget_decision) {
		.native_nr_to_scan = native_nr_to_scan,
		.proposed_nr_to_scan = native_nr_to_scan,
		.applied_nr_to_scan = native_nr_to_scan,
		.reason = PARP_SCAN_REASON_DISABLED,
	};
	return false;
}

static inline unsigned long parp_adapter_apply_scan_budget(
		const struct parp_scan_budget_decision *decision)
{
	return decision->native_nr_to_scan;
}
static inline void parp_damon_aggregate(struct damon_ctx *ctx,
					struct damon_target *target,
					struct damon_region *region)
{
}
static inline void parp_frontier_note_access(struct folio *folio,
					     int generation)
{
}
static inline void parp_frontier_prepare(struct parp_frontier_scan_ctx *ctx,
		struct lruvec *lruvec, bool target_memcg, int type,
		int reclaim_priority, int reclaim_idx,
		unsigned long remaining_demand)
{
	ctx->valid = false;
}
static inline void parp_frontier_consider(struct parp_frontier_scan_ctx *ctx,
					  struct lruvec *lruvec,
					  struct folio *folio)
{
}
static inline void parp_frontier_feedback(struct lruvec *lruvec, int type,
		unsigned long isolated, unsigned long reclaimed)
{
}
#endif
#endif
