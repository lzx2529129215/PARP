/* SPDX-License-Identifier: GPL-2.0 */
#ifndef _MM_PARP_INTERNAL_H
#define _MM_PARP_INTERNAL_H

#include <linux/parp.h>
#include <linux/rcupdate.h>
#include <linux/spinlock.h>
#include <linux/ktime.h>

#define PARP_Q15_ONE		32767U
#define PARP_MAX_APPS		32
#define PARP_MAX_DOMAINS	64
#define PARP_MAX_STATES		16
#define PARP_MAX_REGIONS	64
#define PARP_MAX_SPLITS_PER_DAMON_REGION 64
#define PARP_WINDOW_BUCKETS	60
#define PARP_EVIDENCE_TTL_NS	(60ULL * NSEC_PER_SEC)
#define PARP_APP_PRIOR_BATCH_SCHEMA 1U
#define PARP_APP_PRIOR_MAX_TTL_NS (10ULL * 60 * NSEC_PER_SEC)
#define PARP_APP_PRIOR_MAX_HORIZON_NS (60ULL * 60 * NSEC_PER_SEC)

#define PARP_FRONTIER_FEATURES	8
#define PARP_FRONTIER_BINS	6
#define PARP_FRONTIER_SCHEMA_VERSION 1
#define PARP_FRONTIER_MODEL_VERSION 1
#define PARP_FRONTIER_GENERIC_APP 0

struct parp_frontier_model {
	u32 app_id;
	u32 model_version;
	u32 feature_schema_version;
	s32 threshold;
	s64 bin_edges[PARP_FRONTIER_FEATURES][PARP_FRONTIER_BINS - 1];
	s16 weights[PARP_FRONTIER_FEATURES][PARP_FRONTIER_BINS];
};

struct parp_frontier_stats {
	atomic64_t prepare;
	atomic64_t candidates;
	atomic64_t scores;
	atomic64_t would_promote;
	atomic64_t would_promote_pages;
	atomic64_t applied;
	atomic64_t actual_promote_pages;
	atomic64_t native_bypass_pages;
	atomic64_t budget_bypass_pages;
	atomic64_t frontier_bypass_pages;
	atomic64_t invalid_model_pages;
	atomic64_t metadata_missing;
	atomic64_t no_context;
	atomic64_t no_efficiency;
	atomic64_t no_capacity;
	atomic64_t not_frontier;
	atomic64_t below_threshold;
	atomic64_t budget_reject;
	atomic64_t repeat_epoch;
	atomic64_t pressure_bypass;
	atomic64_t expired;
	atomic64_t feedback_samples;
	atomic64_t trace_events;
	atomic64_t score_time_ns_total;
	atomic64_t score_time_ns_max;
};

extern struct parp_frontier_stats parp_frontier_stats;

#ifdef CONFIG_PARP_FRONTIER_SCORE
s32 parp_frontier_score_values(u32 app_id,
		const s64 values[PARP_FRONTIER_FEATURES], s32 *threshold);
bool parp_frontier_select(const unsigned long *capacities,
		unsigned int nr_capacities, unsigned long demand, u32 eta_q15,
		unsigned int *frontier, unsigned long *headroom);
unsigned long parp_frontier_budget_min(unsigned long headroom,
		unsigned long app_remaining, unsigned long batch_remaining,
		unsigned long epoch_remaining);
bool parp_frontier_context_valid(u64 now_ns, u64 valid_until_ns,
		unsigned long source_seq, unsigned long expected_source_seq);
#endif

#define PARP_SCAN_INPUT_BIND_PRESENT	BIT(0)
#define PARP_SCAN_INPUT_BIND_VALID	BIT(1)
#define PARP_SCAN_INPUT_PRIOR_PRESENT	BIT(2)
#define PARP_SCAN_INPUT_PRIOR_VALID	BIT(3)
#define PARP_SCAN_INPUT_GENERATION_VALID BIT(4)
#define PARP_SCAN_INPUT_MODEL_COMPATIBLE BIT(5)
#define PARP_SCAN_INPUT_CIRCUIT_OK	BIT(6)

enum parp_page_type {
	PARP_PAGE_ANON,
	PARP_PAGE_FILE,
};

enum parp_evidence_mode {
	PARP_EVIDENCE_ONLY,
	PARP_MODEL_TEST,
};

enum parp_region_type {
	PARP_REGION_FILE,
	PARP_REGION_ANON,
	PARP_REGION_SPECIAL,
	PARP_REGION_UNRESOLVED,
};

enum parp_alignment_status {
	PARP_ALIGN_EXACT,
	PARP_ALIGN_SPLIT_EXACT,
	PARP_ALIGN_PARTIAL,
	PARP_ALIGN_AMBIGUOUS,
	PARP_ALIGN_STALE,
	PARP_ALIGN_UNRESOLVED,
};

enum parp_file_version_source {
	PARP_FILE_VERSION_IVERSION,
	PARP_FILE_VERSION_GENERATION,
	PARP_FILE_VERSION_METADATA_HASH,
	PARP_FILE_VERSION_WEAK,
	PARP_FILE_VERSION_UNKNOWN,
};

enum parp_backing_class {
	PARP_BACKING_REGULAR_FILE,
	PARP_BACKING_EXECUTABLE,
	PARP_BACKING_SHARED_LIBRARY,
	PARP_BACKING_SHMEM,
	PARP_BACKING_TMPFS,
	PARP_BACKING_DELETED_FILE,
	PARP_BACKING_SPECIAL,
	PARP_BACKING_UNKNOWN,
};

enum parp_anon_class {
	PARP_ANON_PRIVATE,
	PARP_ANON_SHARED,
	PARP_ANON_HEAP,
	PARP_ANON_STACK,
	PARP_ANON_SHMEM_STYLE,
	PARP_ANON_SPECIAL,
	PARP_ANON_UNKNOWN,
};

enum parp_alignment_reason {
	PARP_REASON_INVALID_RANGE	= BIT(0),
	PARP_REASON_TARGET_EXITED	= BIT(1),
	PARP_REASON_MM_RELEASED		= BIT(2),
	PARP_REASON_NO_MEMCG		= BIT(3),
	PARP_REASON_NO_APP_BIND		= BIT(4),
	PARP_REASON_BIND_EXPIRED		= BIT(5),
	PARP_REASON_SHARED_MM_AMBIGUOUS	= BIT(6),
	PARP_REASON_VMA_HOLE		= BIT(7),
	PARP_REASON_VMA_STALE		= BIT(8),
	PARP_REASON_SPLIT_LIMIT		= BIT(9),
	PARP_REASON_FILE_VERSION_WEAK	= BIT(10),
	PARP_REASON_SHMEM_UNSAFE		= BIT(11),
	PARP_REASON_OUT_OF_ORDER	= BIT(12),
	PARP_REASON_DUPLICATE		= BIT(13),
	PARP_REASON_TABLE_FULL		= BIT(14),
};

struct parp_file_region_key {
	u32 dev_major;
	u32 dev_minor;
	u64 inode;
	u64 file_version;
	u64 start_index;
	u32 nr_pages;
};

struct parp_anon_region_key {
	u64 domain_id;
	u64 foreground_epoch_id;
	u64 mm_cookie;
	u32 process_role;
	u64 vma_signature;
	u32 relative_start_pages;
	u32 nr_pages;
};

struct parp_damon_sample {
	u64 timestamp_ns;
	u64 sample_id;
	u64 target_cookie;
	u64 mm_cookie;
	u32 pid;
	u32 tgid;
	u64 region_start;
	u64 region_end;
	u32 nr_accesses;
	u32 age;
	u32 sample_interval_us;
	u32 aggregation_interval_us;
	u32 flags;
};

struct parp_app_context {
	u64 domain_id;
	u32 app_id;
	u32 bind_generation;
	u64 foreground_epoch_id;
	u64 bind_expiry_ns;
	u64 model_version;
	u16 app_prior_q15;
	u16 owner_confidence_q15;
	u32 owner_source;
};

struct parp_region_identity {
	enum parp_region_type type;
	enum parp_alignment_status alignment;
	u64 domain_id;
	u32 app_id;
	u32 bind_generation;
	u64 foreground_epoch_id;
	u64 mm_cookie;
	u64 virtual_start;
	u64 virtual_end;
	union {
		struct parp_file_region_key file;
		struct parp_anon_region_key anon;
	};
	u32 confidence_q15;
	u32 reason_flags;
};

struct parp_window_stats {
	u64 access_evidence_10s;
	u64 access_evidence_30s;
	u64 access_evidence_60s;
	u32 active_intervals_10s;
	u32 active_intervals_30s;
	u32 active_intervals_60s;
	u64 first_seen_ns;
	u64 last_seen_ns;
	u64 last_access_ns;
	u64 observed_duration_ns;
	u32 age;
	u32 region_size_pages;
};

struct parp_file_observation {
	struct parp_file_region_key key;
	struct parp_app_context owner;
	struct parp_damon_sample sample;
	u32 backing_class;
	u32 version_source;
	u32 alignment_confidence_q15;
	u32 flags;
};

struct parp_anon_observation {
	struct parp_anon_region_key key;
	struct parp_app_context owner;
	struct parp_damon_sample sample;
	u32 anon_class;
	u32 identity_confidence_q15;
	u32 flags;
};

struct parp_file_evidence {
	u64 domain_id;
	u32 app_id;
	u32 bind_generation;
	struct parp_file_region_key key;
	struct parp_window_stats windows;
	u32 backing_class;
	u32 version_source;
	u32 alignment_confidence_q15;
	u64 expires_ns;
	u64 model_version;
	u64 snapshot_version;
};

struct parp_anon_evidence {
	struct parp_anon_region_key key;
	u32 app_id;
	u32 bind_generation;
	u64 model_version;
	struct parp_window_stats windows;
	u32 anon_class;
	u32 identity_confidence_q15;
	u64 expires_ns;
	u64 snapshot_version;
};

struct parp_domain_anon_evidence {
	u64 domain_id;
	u32 app_id;
	u32 bind_generation;
	u64 model_version;
	u64 observed_pages;
	u64 active_pages_10s;
	u64 active_pages_30s;
	u64 active_pages_60s;
	u64 cooling_pages;
	u32 confidence_q15;
};

struct parp_evidence_snapshot {
	struct rcu_head rcu;
	u64 version;
	u64 created_ns;
	u64 expires_ns;
	u32 nr_file_regions;
	u32 nr_anon_regions;
	u32 nr_domains;
	struct parp_file_evidence *files;
	struct parp_anon_evidence *anons;
	struct parp_domain_anon_evidence *domains;
};

struct parp_evidence_stats {
	atomic64_t samples_queued;
	atomic64_t samples_dropped;
	atomic64_t queue_depth;
	atomic64_t queue_high_water;
	atomic64_t target_to_mm_ok;
	atomic64_t mm_to_domain_ok;
	atomic64_t domain_to_bind_ok;
	atomic64_t exact;
	atomic64_t partial;
	atomic64_t ambiguous;
	atomic64_t unresolved;
	atomic64_t unresolved_bytes;
	atomic64_t duplicates;
	atomic64_t out_of_order;
	atomic64_t table_full;
	atomic64_t entries_evicted;
	atomic64_t entries_rejected;
	atomic64_t snapshots;
	atomic64_t file_folio_queries;
	atomic64_t file_folio_matches;
	atomic64_t file_folio_version_mismatch;
	atomic64_t file_folio_domain_mismatch;
	atomic64_t file_folio_no_mapping;
	atomic64_t file_folio_no_region;
	atomic64_t file_folio_expired;
};

struct parp_page_sample {
	enum parp_page_type type;
	u64 domain_id;
	u64 epoch_id;
	u64 file_version;
	u64 index;
	u32 accesses_10s;
	u32 accesses_30s;
	u32 accesses_60s;
	u32 age;
	u16 active_ratio_q15;
	u16 app_prior_q15;
	u16 next_state_q15;
	u16 support_q15;
	u16 stability_q15;
	u16 freshness_q15;
	u8 generation;
	bool resident;
	bool dirty;
	bool writeback;
	bool unevictable;
	bool evidence_valid;
};

struct parp_app_prior {
	u32 app_id;
	u16 use_score_q15;
	u16 rank;
	u32 horizon_ms;
	u64 updated_ns;
	u64 expires_ns;
	u64 model_version;
	u32 prediction_generation;
	u32 flags;
	bool foreground;
	bool valid;
};

struct parp_app_prior_batch {
	u32 schema_version;
	u32 model_version;
	u32 prediction_generation;
	u64 timestamp_ns;
	u64 horizon_ns;
	u64 expiry_ns;
	u32 nr_entries;
	struct parp_app_prior entries[PARP_MAX_APPS];
};

struct parp_scan_budget_input {
	u64 domain_id;
	u32 app_id;
	enum parp_reclaim_scope reclaim_scope;
	enum parp_pressure_level pressure;
	u64 native_nr_to_scan;
	bool foreground;
	u16 app_use_score_q15;
	u16 app_rank;
	u64 foreground_epoch_id;
	u64 bind_generation;
	u64 bind_expiry_ns;
	u64 prediction_timestamp_ns;
	u64 prediction_expiry_ns;
	u32 prediction_generation;
	u32 model_version;
	s32 reclaim_priority;
	u64 now_ns;
	u32 flags;
};

struct parp_binding {
	u64 domain_id;
	u32 app_id;
	u32 bind_generation;
	u64 updated_ns;
	u64 expires_ns;
	u64 epoch_id;
	u64 model_version;
	bool active;
};

struct parp_snapshot {
	struct rcu_head rcu;
	u64 version;
	u64 created_ns;
	u64 expires_ns;
	u32 nr_priors;
	u32 nr_bindings;
	u32 prediction_schema_version;
	u32 prediction_model_version;
	u32 prediction_generation;
	u64 prediction_timestamp_ns;
	u64 prediction_horizon_ns;
	u64 prediction_expiry_ns;
	struct parp_app_prior priors[PARP_MAX_APPS];
	struct parp_binding bindings[PARP_MAX_DOMAINS];
};

struct parp_scan_budget_stats {
	atomic64_t scan_budget_queries;
	atomic64_t target_memcg_queries;
	atomic64_t global_kswapd_bypass;
	atomic64_t global_direct_bypass;
	atomic64_t unknown_scope_bypass;
	atomic64_t no_appbind;
	atomic64_t stale_bind;
	atomic64_t no_prior;
	atomic64_t expired_prior;
	atomic64_t stale_generation;
	atomic64_t model_version_mismatch;
	atomic64_t foreground_decisions;
	atomic64_t high_prior_decisions;
	atomic64_t medium_prior_decisions;
	atomic64_t low_prior_decisions;
	atomic64_t pressure_bypass;
	atomic64_t clamp_min;
	atomic64_t clamp_max;
	atomic64_t observe_count;
	atomic64_t apply_count;
	atomic64_t apply_domain_bypass;
	atomic64_t native_units_total;
	atomic64_t proposed_units_total;
	atomic64_t applied_units_total;
	atomic64_t double_scaling_reject;
	atomic64_t invalid_batch;
	atomic64_t stale_batch;
	atomic64_t circuit_breaker_count;
};

struct parp_scan_guard_view {
	u64 domain_id;
	u32 generation;
	u32 failures;
	bool tripped;
};

struct parp_stats {
	atomic64_t prepare;
	atomic64_t scored;
	atomic64_t proposed[4];
	atomic64_t fallback[PARP_FALLBACK_NR];
	atomic64_t finish;
};

extern struct parp_stats parp_stats;
extern struct parp_evidence_stats parp_evidence_stats;
extern struct parp_scan_budget_stats parp_scan_budget_stats;

u16 parp_q15_mul(u16 a, u16 b);
s16 parp_q15_sat_add(s16 a, s16 b);
u64 parp_tier2_scaled_wmark(u64 limit_bytes, u32 scale, u64 floor);
u64 parp_tier2_ewma_next(u64 previous, u64 sample);
s64 parp_tier2_predict_ms(u64 previous_ewma, u64 ewma,
			  u64 headroom, u64 demote_wmark,
			  u64 elapsed_ms);
int parp_assign_state(const s16 *features, const s16 *centers,
		      unsigned int nr_features, unsigned int nr_states,
		      u32 unknown_threshold);
u16 parp_predict_next_state(const u16 *table, unsigned int nr_states,
			    unsigned int current_state, unsigned int previous_state,
			    unsigned int duration_bin,
			    unsigned int app_prior_bin);
u16 parp_file_future_score(const struct parp_page_sample *sample);
u16 parp_anon_cold_score(const struct parp_page_sample *sample);
struct parp_decision parp_engine_score(const struct parp_snapshot *snapshot,
				       const struct parp_page_sample *sample);
enum parp_mode parp_get_mode(void);
int parp_set_mode(enum parp_mode mode);
const struct parp_snapshot *parp_snapshot_acquire(void);
void parp_snapshot_release(void);
int parp_snapshot_update_binding(const struct parp_binding *binding);
int parp_snapshot_update_prior(const struct parp_app_prior *prior);
int parp_snapshot_replace_prior_batch(const struct parp_app_prior_batch *batch);
void parp_snapshot_fill_scan_budget_input(u64 domain_id, u64 now_ns,
					 struct parp_scan_budget_input *input);
int parp_compute_scan_budget(const struct parp_scan_budget_input *input,
			     struct parp_scan_budget_decision *decision);
enum parp_scan_budget_mode parp_get_scan_budget_mode(void);
int parp_set_scan_budget_mode(enum parp_scan_budget_mode mode);
u64 parp_get_scan_budget_apply_domain(void);
int parp_set_scan_budget_apply_domain(u64 domain_id);
bool parp_scan_budget_guard(u64 domain_id, u32 generation, bool valid);
void parp_scan_budget_guard_clear(u64 domain_id);
unsigned int parp_scan_budget_guard_snapshot(
		struct parp_scan_guard_view *views, unsigned int max_views);
void parp_scan_budget_guard_reset_all_for_test(void);
void parp_scan_budget_account(const struct parp_scan_budget_input *input,
			      const struct parp_scan_budget_decision *decision);
void parp_stats_account(const struct parp_decision *decision);
enum parp_action parp_policy_applied(enum parp_mode mode,
				     enum parp_action original,
				     enum parp_action proposed);
bool parp_budget_allow(unsigned int used, unsigned int limit);
bool parp_fallback_is_native(enum parp_fallback_reason reason);
unsigned int parp_app_prior_bin(u16 score);
bool parp_file_key_equal(const struct parp_file_region_key *a,
			 const struct parp_file_region_key *b);
bool parp_anon_key_valid(const struct parp_anon_region_key *key, u64 epoch);
bool parp_not_expired(u64 expires_ns, u64 now_ns);
bool parp_align_interval(u64 start, u64 end, u64 page_size,
			 u64 *aligned_start, u64 *aligned_end);
bool parp_file_range_from_vma(u64 vm_start, u64 vm_pgoff,
			      u64 start, u64 end, u64 page_size,
			      u64 *file_page_start, u32 *nr_pages);
bool parp_anon_range_from_vma(u64 vm_start, u64 start, u64 end,
			      u64 page_size, u32 *relative_start_pages,
			      u32 *nr_pages);
u64 parp_vma_signature(u32 anon_class, u64 semantic_flags,
		       u64 length_pages, u32 process_role, u64 name_hash);
bool parp_context_lookup(u64 domain_id, u64 now_ns,
			 struct parp_app_context *context);
int parp_evidence_update_file(const struct parp_file_observation *observation);
int parp_evidence_update_anon(const struct parp_anon_observation *observation);
int parp_evidence_publish(u64 now_ns);
void parp_evidence_domain_offline(u64 domain_id);
void parp_evidence_set_limits_for_test(unsigned int file_limit,
				       unsigned int anon_limit,
				       unsigned int total_limit);
void parp_evidence_account_unresolved(u64 bytes, enum parp_alignment_status status);
bool parp_damon_mm_task_budget_exhausted(struct mm_struct *candidate,
					 struct mm_struct *expected,
					 unsigned int *checked);
bool parp_evidence_lookup_file(u64 domain_id,
			       const struct parp_file_region_key *key,
			       u64 index, u32 nr_pages,
			       struct parp_file_evidence *result);
bool parp_evidence_lookup_anon_domain(u64 domain_id,
				      struct parp_domain_anon_evidence *result);
enum parp_evidence_mode parp_get_evidence_mode(void);
int parp_set_evidence_mode(enum parp_evidence_mode mode);
u32 parp_backing_classify(bool has_file, bool shmem, bool deleted,
			  bool executable);
bool parp_segments_conserve(const u64 *starts, const u64 *ends,
			    unsigned int nr_segments, u64 original_start,
			    u64 original_end);
int parp_control_init(void);
void parp_control_exit(void);

#endif
