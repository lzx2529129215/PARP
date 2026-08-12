/* SPDX-License-Identifier: GPL-2.0 */
#undef TRACE_SYSTEM
#define TRACE_SYSTEM parp

#if !defined(_TRACE_PARP_H) || defined(TRACE_HEADER_MULTI_READ)
#define _TRACE_PARP_H

#include <linux/parp.h>
#include <linux/tracepoint.h>

#ifndef _TRACE_PARP_TYPES_H
#define _TRACE_PARP_TYPES_H
struct parp_region_trace {
	u64 sample_id;
	u64 sample_timestamp_ns;
	u32 pid;
	u32 tgid;
	u64 domain_id;
	u32 app_id;
	u32 bind_generation;
	u64 foreground_epoch_id;
	u64 model_version;
	u64 mm_cookie;
	u8 region_type;
	u8 alignment;
	u64 region_start;
	u64 region_end;
	u64 logical_start;
	u32 nr_pages;
	u32 dev_major;
	u32 dev_minor;
	u64 inode;
	u64 file_version;
	u64 file_size_bytes;
	u64 file_page_count;
	u64 vma_signature;
	u32 sample_interval_us;
	u32 aggregation_interval_us;
	u32 nr_accesses;
	u32 age;
	u32 confidence_q15;
	u32 reason_flags;
};

struct parp_scan_budget_trace {
	u64 timestamp_ns;
	u64 budget_sequence;
	u8 reclaim_scope;
	u64 domain_id;
	u32 app_id;
	bool foreground;
	u16 app_score_q15;
	u16 app_rank;
	u32 prediction_generation;
	u32 model_version;
	u64 prediction_age_ms;
	u64 native_nr_to_scan;
	u64 proposed_nr_to_scan;
	u64 applied_nr_to_scan;
	u32 multiplier_q15;
	u8 pressure_level;
	s32 reclaim_priority;
	u8 reason;
	u8 mode;
};

struct parp_frontier_trace {
	u64 timestamp_ns;
	u64 domain_id;
	u64 epoch_id;
	u64 batch_id;
	u64 foreground_epoch_id;
	u64 folio_cookie;
	u64 source_seq;
	u64 frontier_seq;
	u64 remaining_demand;
	u64 frontier_headroom;
	u64 app_budget;
	u64 batch_budget;
	u64 epoch_budget;
	u64 folio_pages;
	u64 score_duration_ns;
	s32 score;
	u32 threshold;
	u32 model_version;
	u32 efficiency_q15;
	u32 app_id;
	u32 nid;
	u32 source_generation;
	u32 feature_schema_version;
	u8 page_type;
	u8 mode;
	u8 reason;
	bool would_promote;
	bool applied;
};

struct parp_effective_tier_trace {
	u64 timestamp_ns;
	u64 experiment_id;
	u64 session_id;
	u64 folio_cookie;
	u64 memcg_id;
	u64 source_seq;
	u64 batch_id;
	u64 reclaim_epoch;
	u64 score_duration_ns;
	u64 decision_duration_ns;
	u64 trace_sequence;
	s64 features[6];
	s32 reuse_score;
	s32 cold_threshold;
	s32 hot_threshold_1;
	s32 hot_threshold_2;
	s32 hot_threshold_3;
	s32 delta_tier_q8;
	s32 fixed_delta_q8;
	s32 binary_bypass_delta_q8;
	s32 pressure_aware_delta_q8; /* #lzx */
	s32 effective_tier_q8;
	s32 priority;
	u64 nr_to_reclaim;
	u64 nr_reclaimed_before;
	u64 epoch_reclaimed_pages;
	u64 batch_scanned_pages;
	u64 batch_isolated_pages;
	u64 batch_reclaimed_pages; /* #lzx */
	u64 folio_nr_pages;
	u32 lifetime_epoch;
	u32 nid;
	u32 model_version;
	u32 expected_model_version;
	u32 feature_schema_version;
	u32 pressure_policy_version;
	char model_type[PARP_TIER_TRACE_MODEL_TYPE_LEN];
	char model_checksum[PARP_TIER_TRACE_CHECKSUM_LEN];
	char model_provenance[PARP_TIER_TRACE_PROVENANCE_LEN];
	char pressure_policy_provenance[PARP_TIER_TRACE_PRESSURE_PROVENANCE_LEN];
	u16 consecutive_no_progress_batches; /* #lzx */
	u8 generation_index;
	u8 native_tier;
	u8 native_tier_idx;
	u8 page_type;
	u8 mode;
	u8 action;
	u8 bypass;
	u8 rank_score_bin;
	u8 pressure_level_kernel;
	u8 reclaim_context;
	u8 pressure_bypass_reason;
	bool special_native_protect;
	bool model_valid;
	bool features_valid;
	bool native_protect;
	bool effective_protect;
	bool fixed_effective_protect;
	bool pressure_aware_effective_protect; /* #lzx */
	bool actual_tier_protect;
	bool sort_result;
	bool isolate_attempted;
	bool isolate_result;
};

struct parp_effective_tier_access_trace {
	u64 timestamp_ns;
	u64 trace_sequence;
	u64 experiment_id;
	u64 session_id;
	u64 folio_cookie;
	u32 lifetime_epoch;
	s32 generation;
	u8 page_type;
	u8 event;
	u8 candidate_count; /* #lzx */
	bool real_access;
};

#define PARP_EFFECTIVE_TIER_ACCESS_TRACE_TIME_FORMAT \
	"time=%llu trace_seq=%llu experiment=%llu session=%llu " /* #lzx */
#define PARP_EFFECTIVE_TIER_ACCESS_TRACE_IDENTITY_FORMAT \
	"cookie=%llu lifetime=%u gen=%d type=%u event=%u " /* #lzx */
#define PARP_EFFECTIVE_TIER_ACCESS_TRACE_CANDIDATE_FORMAT \
	"candidate_count=%u real=%d" /* #lzx */

struct parp_effective_tier_outcome_trace {
	u64 timestamp_ns;
	u64 trace_sequence;
	u64 experiment_id;
	u64 session_id;
	u64 folio_cookie;
	u32 lifetime_epoch;
	u8 proposed_action;
	u8 actual_action;
	u8 outcome;
};

struct parp_effective_tier_batch_trace {
	u64 timestamp_ns;
	u64 trace_sequence;
	u64 experiment_id;
	u64 session_id;
	u64 batch_id;
	u64 reclaim_epoch;
	u64 model_time_ns;
	u64 candidate_pages;
	u64 upgrade_pages;
	u64 downgrade_pages;
	u64 isolated_pages;
	u64 nr_to_reclaim;
	u64 nr_reclaimed_before;
	u64 epoch_reclaimed_pages;
	u64 batch_scanned_pages;
	u64 batch_isolated_pages;
	u64 batch_reclaimed_pages;
	u32 pressure_policy_version;
	u16 consecutive_no_progress_batches;
	u8 page_type;
	u8 mode;
	u8 pressure_level_kernel;
	u8 reclaim_context;
	u8 pressure_bypass_reason;
	bool reclaim_result; /* #lzx */
};

struct parp_effective_tier_lock_trace {
	u64 timestamp_ns;
	u64 trace_sequence;
	u64 experiment_id;
	u64 session_id;
	u64 wait_ns;
	u64 held_ns;
	u64 irq_disabled_ns;
	u32 nid;
	u8 mode;
	u8 scope;
	bool irq_disabled_measured;
};
#endif /* _TRACE_PARP_TYPES_H */

TRACE_EVENT(parp_decision,
	TP_PROTO(u64 domain_id, u8 mode, u8 page_type, u8 original,
		 u8 proposed, u8 applied, u8 fallback, u16 score),
	TP_ARGS(domain_id, mode, page_type, original, proposed, applied,
		fallback, score),
	TP_STRUCT__entry(
		__field(u64, domain_id)
		__field(u8, mode)
		__field(u8, page_type)
		__field(u8, original)
		__field(u8, proposed)
		__field(u8, applied)
		__field(u8, fallback)
		__field(u16, score)
	),
	TP_fast_assign(
		__entry->domain_id = domain_id;
		__entry->mode = mode;
		__entry->page_type = page_type;
		__entry->original = original;
		__entry->proposed = proposed;
		__entry->applied = applied;
		__entry->fallback = fallback;
		__entry->score = score;
	),
	TP_printk("domain=%llu mode=%u type=%u original=%u proposed=%u applied=%u fallback=%u score_q15=%u",
		  __entry->domain_id, __entry->mode, __entry->page_type,
		  __entry->original, __entry->proposed, __entry->applied,
		  __entry->fallback, __entry->score)
);

TRACE_EVENT(parp_frontier_decision,
	TP_PROTO(const struct parp_frontier_trace *event),
	TP_ARGS(event),
	TP_STRUCT__entry(
		__field(u64, timestamp_ns)
		__field(u64, domain_id)
		__field(u64, epoch_id)
		__field(u64, batch_id)
		__field(u64, foreground_epoch_id)
		__field(u64, folio_cookie)
		__field(u64, source_seq)
		__field(u64, frontier_seq)
		__field(u64, remaining_demand)
		__field(u64, frontier_headroom)
		__field(u64, app_budget)
		__field(u64, batch_budget)
		__field(u64, epoch_budget)
		__field(u64, folio_pages)
		__field(u64, score_duration_ns)
		__field(s32, score)
		__field(u32, threshold)
		__field(u32, model_version)
		__field(u32, efficiency_q15)
		__field(u32, app_id)
		__field(u32, nid)
		__field(u32, source_generation)
		__field(u32, feature_schema_version)
		__field(u8, page_type)
		__field(u8, mode)
		__field(u8, reason)
		__field(bool, would_promote)
		__field(bool, applied)
	),
	TP_fast_assign(
		__entry->timestamp_ns = event->timestamp_ns;
		__entry->domain_id = event->domain_id;
		__entry->epoch_id = event->epoch_id;
		__entry->batch_id = event->batch_id;
		__entry->foreground_epoch_id = event->foreground_epoch_id;
		__entry->folio_cookie = event->folio_cookie;
		__entry->source_seq = event->source_seq;
		__entry->frontier_seq = event->frontier_seq;
		__entry->remaining_demand = event->remaining_demand;
		__entry->frontier_headroom = event->frontier_headroom;
		__entry->app_budget = event->app_budget;
		__entry->batch_budget = event->batch_budget;
		__entry->epoch_budget = event->epoch_budget;
		__entry->folio_pages = event->folio_pages;
		__entry->score_duration_ns = event->score_duration_ns;
		__entry->score = event->score;
		__entry->threshold = event->threshold;
		__entry->model_version = event->model_version;
		__entry->efficiency_q15 = event->efficiency_q15;
		__entry->app_id = event->app_id;
		__entry->nid = event->nid;
		__entry->source_generation = event->source_generation;
		__entry->feature_schema_version = event->feature_schema_version;
		__entry->page_type = event->page_type;
		__entry->mode = event->mode;
		__entry->reason = event->reason;
		__entry->would_promote = event->would_promote;
		__entry->applied = event->applied;
	),
	TP_printk("time=%llu domain=%llu app=%u nid=%u type=%u mode=%u epoch=%llu batch=%llu foreground_epoch=%llu folio_cookie=%llu source_seq=%llu source_gen=%u frontier_seq=%llu remaining=%llu headroom=%llu app_budget=%llu batch_budget=%llu epoch_budget=%llu folio_pages=%llu score=%d threshold=%u model=%u schema=%u efficiency_q15=%u score_duration_ns=%llu reason=%u would_promote=%d applied=%d",
		  __entry->timestamp_ns, __entry->domain_id, __entry->app_id,
		  __entry->nid, __entry->page_type, __entry->mode,
		  __entry->epoch_id, __entry->batch_id,
		  __entry->foreground_epoch_id, __entry->folio_cookie,
		  __entry->source_seq, __entry->source_generation,
		  __entry->frontier_seq, __entry->remaining_demand,
		  __entry->frontier_headroom, __entry->app_budget,
		  __entry->batch_budget, __entry->epoch_budget,
		  __entry->folio_pages, __entry->score, __entry->threshold,
		  __entry->model_version, __entry->feature_schema_version,
		  __entry->efficiency_q15, __entry->score_duration_ns,
		  __entry->reason, __entry->would_promote, __entry->applied)
);

TRACE_EVENT(parp_effective_tier_decision,
	TP_PROTO(const struct parp_effective_tier_trace *event),
	TP_ARGS(event),
	TP_STRUCT__entry(
		__field(u64, timestamp_ns)
		__field(u64, experiment_id)
		__field(u64, session_id)
		__field(u64, folio_cookie)
		__field(u64, memcg_id)
		__field(u64, source_seq)
		__field(u64, batch_id)
		__field(u64, reclaim_epoch)
		__field(u64, score_duration_ns)
		__field(u64, decision_duration_ns)
		__field(u64, trace_sequence)
		__array(s64, features, 6)
		__field(s32, reuse_score)
		__field(s32, cold_threshold)
		__field(s32, hot_threshold_1)
		__field(s32, hot_threshold_2)
		__field(s32, hot_threshold_3)
		__field(s32, delta_tier_q8)
		__field(s32, fixed_delta_q8)
		__field(s32, binary_bypass_delta_q8)
		__field(s32, pressure_aware_delta_q8) /* #lzx */
		__field(s32, effective_tier_q8)
		__field(s32, priority)
		__field(u64, nr_to_reclaim)
		__field(u64, nr_reclaimed_before)
		__field(u64, epoch_reclaimed_pages)
		__field(u64, batch_scanned_pages)
		__field(u64, batch_isolated_pages)
		__field(u64, batch_reclaimed_pages) /* #lzx */
		__field(u64, folio_nr_pages)
		__field(u32, lifetime_epoch)
		__field(u32, nid)
		__field(u32, model_version)
		__field(u32, expected_model_version)
		__field(u32, feature_schema_version)
		__field(u32, pressure_policy_version)
		__string(model_type, event->model_type) /* #lzx */
		__string(model_checksum, event->model_checksum) /* #lzx */
		__string(model_provenance, event->model_provenance) /* #lzx */
		__string(pressure_policy_provenance,
			event->pressure_policy_provenance) /* #lzx */
		__field(u16, consecutive_no_progress_batches) /* #lzx */
		__field(u8, generation_index)
		__field(u8, native_tier)
		__field(u8, native_tier_idx)
		__field(u8, page_type)
		__field(u8, mode)
		__field(u8, action)
		__field(u8, bypass)
		__field(u8, rank_score_bin)
		__field(u8, pressure_level_kernel)
		__field(u8, reclaim_context)
		__field(u8, pressure_bypass_reason)
		__field(bool, special_native_protect)
		__field(bool, model_valid)
		__field(bool, features_valid)
		__field(bool, native_protect)
		__field(bool, effective_protect)
		__field(bool, fixed_effective_protect)
		__field(bool, pressure_aware_effective_protect) /* #lzx */
		__field(bool, actual_tier_protect)
		__field(bool, sort_result)
		__field(bool, isolate_attempted)
		__field(bool, isolate_result)
	),
	TP_fast_assign(
		__entry->timestamp_ns = event->timestamp_ns;
		__entry->experiment_id = event->experiment_id;
		__entry->session_id = event->session_id;
		__entry->folio_cookie = event->folio_cookie;
		__entry->memcg_id = event->memcg_id;
		__entry->source_seq = event->source_seq;
		__entry->batch_id = event->batch_id;
		__entry->reclaim_epoch = event->reclaim_epoch;
		__entry->score_duration_ns = event->score_duration_ns;
		__entry->decision_duration_ns = event->decision_duration_ns;
		__entry->trace_sequence = event->trace_sequence;
		memcpy(__entry->features, event->features,
		       sizeof(__entry->features));
		__entry->reuse_score = event->reuse_score;
		__entry->cold_threshold = event->cold_threshold;
		__entry->hot_threshold_1 = event->hot_threshold_1;
		__entry->hot_threshold_2 = event->hot_threshold_2;
		__entry->hot_threshold_3 = event->hot_threshold_3;
		__entry->delta_tier_q8 = event->delta_tier_q8;
		__entry->fixed_delta_q8 = event->fixed_delta_q8;
		__entry->binary_bypass_delta_q8 = event->binary_bypass_delta_q8;
		__entry->pressure_aware_delta_q8 = event->pressure_aware_delta_q8;
		/* #lzx */
		__entry->effective_tier_q8 = event->effective_tier_q8;
		__entry->priority = event->priority;
		__entry->nr_to_reclaim = event->nr_to_reclaim;
		__entry->nr_reclaimed_before = event->nr_reclaimed_before;
		__entry->epoch_reclaimed_pages = event->epoch_reclaimed_pages;
		__entry->batch_scanned_pages = event->batch_scanned_pages;
		__entry->batch_isolated_pages = event->batch_isolated_pages;
		__entry->batch_reclaimed_pages = event->batch_reclaimed_pages; /* #lzx */
		__entry->folio_nr_pages = event->folio_nr_pages;
		__entry->lifetime_epoch = event->lifetime_epoch;
		__entry->nid = event->nid;
		__entry->model_version = event->model_version;
		__entry->expected_model_version = event->expected_model_version;
		__entry->feature_schema_version = event->feature_schema_version;
		__entry->pressure_policy_version = event->pressure_policy_version;
		__assign_str(model_type); /* #lzx */
		__assign_str(model_checksum); /* #lzx */
		__assign_str(model_provenance); /* #lzx */
		__assign_str(pressure_policy_provenance); /* #lzx */
		__entry->consecutive_no_progress_batches =
			event->consecutive_no_progress_batches; /* #lzx */
		__entry->generation_index = event->generation_index;
		__entry->native_tier = event->native_tier;
		__entry->native_tier_idx = event->native_tier_idx;
		__entry->page_type = event->page_type;
		__entry->mode = event->mode;
		__entry->action = event->action;
		__entry->bypass = event->bypass;
		__entry->rank_score_bin = event->rank_score_bin;
		__entry->pressure_level_kernel = event->pressure_level_kernel;
		__entry->reclaim_context = event->reclaim_context;
		__entry->pressure_bypass_reason = event->pressure_bypass_reason;
		__entry->special_native_protect = event->special_native_protect;
		__entry->model_valid = event->model_valid;
		__entry->features_valid = event->features_valid;
		__entry->native_protect = event->native_protect;
		__entry->effective_protect = event->effective_protect;
		__entry->fixed_effective_protect = event->fixed_effective_protect;
		__entry->pressure_aware_effective_protect =
			event->pressure_aware_effective_protect; /* #lzx */
		__entry->actual_tier_protect = event->actual_tier_protect;
		__entry->sort_result = event->sort_result;
		__entry->isolate_attempted = event->isolate_attempted;
		__entry->isolate_result = event->isolate_result;
	),
	TP_printk("time=%llu experiment=%llu session=%llu cookie=%llu lifetime=%u memcg=%llu nid=%u type=%u source_seq=%llu gen=%u native_tier=%u tier_idx=%u special=%d mode=%u model_type=%s model_version=%u expected_model_version=%u feature_schema_version=%u model_checksum=%s model_provenance=%s pressure_policy_provenance=%s model_valid=%d features_valid=%d native_protect=%d effective_protect=%d actual_tier_protect=%d score=%d score_bin=%u rank_score_bin=%u thresholds=%d/%d/%d/%d delta_q8=%d effective_q8=%d action=%u bypass=%u pages=%llu batch=%llu epoch=%llu priority=%d score_ns=%llu decision_ns=%llu trace_seq=%llu sort=%d isolate_attempted=%d isolate_result=%d features=%lld,%lld,%lld,%lld,%lld,%lld pressure_level_kernel=%u reclaim_context=%u sc_priority=%d nr_to_reclaim=%llu nr_reclaimed_before=%llu epoch_reclaimed_pages=%llu batch_scanned_pages=%llu batch_isolated_pages=%llu batch_reclaimed_pages=%llu consecutive_no_progress_batches=%u fixed_delta_q8=%d binary_bypass_delta_q8=%d pressure_aware_delta_q8=%d fixed_effective_protect=%d pressure_aware_effective_protect=%d pressure_policy_version=%u pressure_bypass_reason=%u", /* #lzx */
		  __entry->timestamp_ns, __entry->experiment_id,
		  __entry->session_id, __entry->folio_cookie,
		  __entry->lifetime_epoch, __entry->memcg_id, __entry->nid,
		  __entry->page_type, __entry->source_seq,
		  __entry->generation_index, __entry->native_tier,
		  __entry->native_tier_idx, __entry->special_native_protect,
		__entry->mode, __get_str(model_type), __entry->model_version, /* #lzx */
		__entry->expected_model_version,
		__entry->feature_schema_version, __get_str(model_checksum), /* #lzx */
		__get_str(model_provenance), __get_str(pressure_policy_provenance), /* #lzx */
		__entry->model_valid, /* #lzx */
		  __entry->features_valid,
		  __entry->native_protect, __entry->effective_protect,
		  __entry->actual_tier_protect, __entry->reuse_score,
		  __entry->rank_score_bin, __entry->rank_score_bin,
		  __entry->cold_threshold, __entry->hot_threshold_1,
		  __entry->hot_threshold_2, __entry->hot_threshold_3,
		  __entry->delta_tier_q8,
		  __entry->effective_tier_q8, __entry->action, __entry->bypass,
		  __entry->folio_nr_pages, __entry->batch_id,
		  __entry->reclaim_epoch, __entry->priority,
		  __entry->score_duration_ns, __entry->decision_duration_ns,
		  __entry->trace_sequence,
		  __entry->sort_result, __entry->isolate_attempted,
		  __entry->isolate_result, __entry->features[0],
		  __entry->features[1], __entry->features[2],
		  __entry->features[3], __entry->features[4],
		  __entry->features[5], __entry->pressure_level_kernel,
		  __entry->reclaim_context, __entry->priority,
		  __entry->nr_to_reclaim, __entry->nr_reclaimed_before,
		  __entry->epoch_reclaimed_pages, __entry->batch_scanned_pages,
		  __entry->batch_isolated_pages, __entry->batch_reclaimed_pages,
		  __entry->consecutive_no_progress_batches,
		  __entry->fixed_delta_q8, __entry->binary_bypass_delta_q8,
		  __entry->pressure_aware_delta_q8,
		  __entry->fixed_effective_protect,
		  __entry->pressure_aware_effective_protect,
		  __entry->pressure_policy_version,
		  __entry->pressure_bypass_reason) /* #lzx */
);

TRACE_EVENT(parp_effective_tier_access,
	TP_PROTO(const struct parp_effective_tier_access_trace *event),
	TP_ARGS(event),
	TP_STRUCT__entry(
		__field(u64, timestamp_ns)
		__field(u64, trace_sequence)
		__field(u64, experiment_id)
		__field(u64, session_id)
		__field(u64, folio_cookie)
		__field(u32, lifetime_epoch)
		__field(s32, generation)
		__field(u8, page_type)
		__field(u8, event)
		__field(u8, candidate_count) /* #lzx */
		__field(bool, real_access)
	),
	TP_fast_assign(
		__entry->timestamp_ns = event->timestamp_ns;
		__entry->trace_sequence = event->trace_sequence;
		__entry->experiment_id = event->experiment_id;
		__entry->session_id = event->session_id;
		__entry->folio_cookie = event->folio_cookie;
		__entry->lifetime_epoch = event->lifetime_epoch;
		__entry->generation = event->generation;
		__entry->page_type = event->page_type;
		__entry->event = event->event;
		__entry->candidate_count = event->candidate_count; /* #lzx */
		__entry->real_access = event->real_access;
	),
	TP_printk(PARP_EFFECTIVE_TIER_ACCESS_TRACE_TIME_FORMAT
		  PARP_EFFECTIVE_TIER_ACCESS_TRACE_IDENTITY_FORMAT
		  PARP_EFFECTIVE_TIER_ACCESS_TRACE_CANDIDATE_FORMAT, /* #lzx */
		  __entry->timestamp_ns, __entry->trace_sequence,
		  __entry->experiment_id, __entry->session_id,
		  __entry->folio_cookie,
		  __entry->lifetime_epoch, __entry->generation,
		  __entry->page_type, __entry->event, __entry->candidate_count,
		  __entry->real_access) /* #lzx */
);

TRACE_EVENT(parp_effective_tier_outcome,
	TP_PROTO(const struct parp_effective_tier_outcome_trace *event),
	TP_ARGS(event),
	TP_STRUCT__entry(
		__field(u64, timestamp_ns)
		__field(u64, trace_sequence)
		__field(u64, experiment_id)
		__field(u64, session_id)
		__field(u64, folio_cookie)
		__field(u32, lifetime_epoch)
		__field(u8, proposed_action)
		__field(u8, actual_action)
		__field(u8, outcome)
	),
	TP_fast_assign(
		__entry->timestamp_ns = event->timestamp_ns;
		__entry->trace_sequence = event->trace_sequence;
		__entry->experiment_id = event->experiment_id;
		__entry->session_id = event->session_id;
		__entry->folio_cookie = event->folio_cookie;
		__entry->lifetime_epoch = event->lifetime_epoch;
		__entry->proposed_action = event->proposed_action;
		__entry->actual_action = event->actual_action;
		__entry->outcome = event->outcome;
	),
	TP_printk("time=%llu trace_seq=%llu experiment=%llu session=%llu cookie=%llu lifetime=%u action=%u actual_action=%u outcome=%u",
		  __entry->timestamp_ns, __entry->trace_sequence,
		  __entry->experiment_id, __entry->session_id,
		  __entry->folio_cookie,
		  __entry->lifetime_epoch, __entry->proposed_action,
		  __entry->actual_action, __entry->outcome)
);

TRACE_EVENT(parp_effective_tier_batch,
	TP_PROTO(const struct parp_effective_tier_batch_trace *event),
	TP_ARGS(event),
	TP_STRUCT__entry(
		__field(u64, timestamp_ns)
		__field(u64, trace_sequence)
		__field(u64, experiment_id)
		__field(u64, session_id)
		__field(u64, batch_id)
		__field(u64, reclaim_epoch)
		__field(u64, model_time_ns)
		__field(u64, candidate_pages)
		__field(u64, upgrade_pages)
		__field(u64, downgrade_pages)
		__field(u64, isolated_pages)
		__field(u64, nr_to_reclaim)
		__field(u64, nr_reclaimed_before)
		__field(u64, epoch_reclaimed_pages)
		__field(u64, batch_scanned_pages)
		__field(u64, batch_isolated_pages)
		__field(u64, batch_reclaimed_pages)
		__field(u32, pressure_policy_version)
		__field(u16, consecutive_no_progress_batches)
		__field(u8, page_type)
		__field(u8, mode)
		__field(u8, pressure_level_kernel)
		__field(u8, reclaim_context)
		__field(u8, pressure_bypass_reason)
		__field(bool, reclaim_result) /* #lzx */
	),
	TP_fast_assign(
		__entry->timestamp_ns = event->timestamp_ns;
		__entry->trace_sequence = event->trace_sequence;
		__entry->experiment_id = event->experiment_id;
		__entry->session_id = event->session_id;
		__entry->batch_id = event->batch_id;
		__entry->reclaim_epoch = event->reclaim_epoch;
		__entry->model_time_ns = event->model_time_ns;
		__entry->candidate_pages = event->candidate_pages;
		__entry->upgrade_pages = event->upgrade_pages;
		__entry->downgrade_pages = event->downgrade_pages;
		__entry->isolated_pages = event->isolated_pages;
		__entry->nr_to_reclaim = event->nr_to_reclaim;
		__entry->nr_reclaimed_before = event->nr_reclaimed_before;
		__entry->epoch_reclaimed_pages = event->epoch_reclaimed_pages;
		__entry->batch_scanned_pages = event->batch_scanned_pages;
		__entry->batch_isolated_pages = event->batch_isolated_pages;
		__entry->batch_reclaimed_pages = event->batch_reclaimed_pages;
		__entry->pressure_policy_version = event->pressure_policy_version;
		__entry->consecutive_no_progress_batches =
			event->consecutive_no_progress_batches; /* #lzx */
		__entry->page_type = event->page_type;
		__entry->mode = event->mode;
		__entry->pressure_level_kernel = event->pressure_level_kernel;
		__entry->reclaim_context = event->reclaim_context;
		__entry->pressure_bypass_reason = event->pressure_bypass_reason;
		__entry->reclaim_result = event->reclaim_result; /* #lzx */
	),
	TP_printk("time=%llu trace_seq=%llu experiment=%llu session=%llu batch=%llu epoch=%llu type=%u mode=%u candidates=%llu upgrades=%llu downgrades=%llu isolated=%llu model_ns=%llu nr_to_reclaim=%llu nr_reclaimed_before=%llu epoch_reclaimed_pages=%llu batch_scanned_pages=%llu batch_isolated_pages=%llu batch_reclaimed_pages=%llu pressure_policy_version=%u consecutive_no_progress_batches=%u pressure_level_kernel=%u reclaim_context=%u pressure_bypass_reason=%u reclaim_result=%d",
		  __entry->timestamp_ns, __entry->trace_sequence,
		  __entry->experiment_id, __entry->session_id,
		  __entry->batch_id,
		  __entry->reclaim_epoch, __entry->page_type, __entry->mode,
		  __entry->candidate_pages, __entry->upgrade_pages,
		  __entry->downgrade_pages, __entry->isolated_pages,
		  __entry->model_time_ns, __entry->nr_to_reclaim,
		  __entry->nr_reclaimed_before, __entry->epoch_reclaimed_pages,
		  __entry->batch_scanned_pages, __entry->batch_isolated_pages,
		  __entry->batch_reclaimed_pages,
		  __entry->pressure_policy_version,
		  __entry->consecutive_no_progress_batches,
		  __entry->pressure_level_kernel, __entry->reclaim_context,
		  __entry->pressure_bypass_reason, __entry->reclaim_result) /* #lzx */
);

TRACE_EVENT(parp_effective_tier_lock,
	TP_PROTO(const struct parp_effective_tier_lock_trace *event),
	TP_ARGS(event),
	TP_STRUCT__entry(
		__field(u64, timestamp_ns)
		__field(u64, trace_sequence)
		__field(u64, experiment_id)
		__field(u64, session_id)
		__field(u64, wait_ns)
		__field(u64, held_ns)
		__field(u64, irq_disabled_ns)
		__field(u32, nid)
		__field(u8, mode)
		__field(u8, scope)
		__field(bool, irq_disabled_measured)
	),
	TP_fast_assign(
		__entry->timestamp_ns = event->timestamp_ns;
		__entry->trace_sequence = event->trace_sequence;
		__entry->experiment_id = event->experiment_id;
		__entry->session_id = event->session_id;
		__entry->wait_ns = event->wait_ns;
		__entry->held_ns = event->held_ns;
		__entry->irq_disabled_ns = event->irq_disabled_ns;
		__entry->nid = event->nid;
		__entry->mode = event->mode;
		__entry->scope = event->scope;
		__entry->irq_disabled_measured = event->irq_disabled_measured;
	),
	TP_printk("time=%llu trace_seq=%llu experiment=%llu session=%llu nid=%u mode=%u scope=%u wait_ns=%llu held_ns=%llu irq_disabled_ns=%llu irq_measured=%d",
		  __entry->timestamp_ns, __entry->trace_sequence,
		  __entry->experiment_id,
		  __entry->session_id, __entry->nid, __entry->mode,
		  __entry->scope, __entry->wait_ns, __entry->held_ns,
		  __entry->irq_disabled_ns,
		  __entry->irq_disabled_measured)
);

TRACE_EVENT(parp_region_evidence,
	TP_PROTO(const struct parp_region_trace *event),
	TP_ARGS(event),
	TP_STRUCT__entry(
		__field(u64, sample_id)
		__field(u64, sample_timestamp_ns)
		__field(u32, pid)
		__field(u32, tgid)
		__field(u64, domain_id)
		__field(u32, app_id)
		__field(u32, bind_generation)
		__field(u64, foreground_epoch_id)
		__field(u64, model_version)
		__field(u64, mm_cookie)
		__field(u8, region_type)
		__field(u8, alignment)
		__field(u64, region_start)
		__field(u64, region_end)
		__field(u64, logical_start)
		__field(u32, nr_pages)
		__field(u32, dev_major)
		__field(u32, dev_minor)
		__field(u64, inode)
		__field(u64, file_version)
		__field(u64, file_size_bytes)
		__field(u64, file_page_count)
		__field(u64, vma_signature)
		__field(u32, sample_interval_us)
		__field(u32, aggregation_interval_us)
		__field(u32, nr_accesses)
		__field(u32, age)
		__field(u32, confidence_q15)
		__field(u32, reason_flags)
	),
	TP_fast_assign(
		__entry->sample_id = event->sample_id;
		__entry->sample_timestamp_ns = event->sample_timestamp_ns;
		__entry->pid = event->pid;
		__entry->tgid = event->tgid;
		__entry->domain_id = event->domain_id;
		__entry->app_id = event->app_id;
		__entry->bind_generation = event->bind_generation;
		__entry->foreground_epoch_id = event->foreground_epoch_id;
		__entry->model_version = event->model_version;
		__entry->mm_cookie = event->mm_cookie;
		__entry->region_type = event->region_type;
		__entry->alignment = event->alignment;
		__entry->region_start = event->region_start;
		__entry->region_end = event->region_end;
		__entry->logical_start = event->logical_start;
		__entry->nr_pages = event->nr_pages;
		__entry->dev_major = event->dev_major;
		__entry->dev_minor = event->dev_minor;
		__entry->inode = event->inode;
		__entry->file_version = event->file_version;
		__entry->file_size_bytes = event->file_size_bytes;
		__entry->file_page_count = event->file_page_count;
		__entry->vma_signature = event->vma_signature;
		__entry->sample_interval_us = event->sample_interval_us;
		__entry->aggregation_interval_us = event->aggregation_interval_us;
		__entry->nr_accesses = event->nr_accesses;
		__entry->age = event->age;
		__entry->confidence_q15 = event->confidence_q15;
		__entry->reason_flags = event->reason_flags;
	),
	TP_printk("sample=%llu sample_time=%llu pid=%u tgid=%u domain=%llu app=%u bind_generation=%u foreground_epoch=%llu model=%llu mm_cookie=%llu type=%u align=%u region_start=%llu region_end=%llu logical_start=%llu nr_pages=%u dev_major=%u dev_minor=%u inode=%llu file_version=%llu file_size=%llu file_pages=%llu vma_signature=%llu sample_us=%u aggregation_us=%u access_evidence=%u age=%u confidence_q15=%u reasons=0x%x",
		  __entry->sample_id, __entry->sample_timestamp_ns,
		  __entry->pid, __entry->tgid, __entry->domain_id,
		  __entry->app_id, __entry->bind_generation,
		  __entry->foreground_epoch_id, __entry->model_version,
		  __entry->mm_cookie, __entry->region_type,
		  __entry->alignment, __entry->region_start,
		  __entry->region_end, __entry->logical_start,
		  __entry->nr_pages, __entry->dev_major,
		  __entry->dev_minor, __entry->inode,
		  __entry->file_version, __entry->file_size_bytes,
		  __entry->file_page_count, __entry->vma_signature,
		  __entry->sample_interval_us,
		  __entry->aggregation_interval_us,
		  __entry->nr_accesses, __entry->age,
		  __entry->confidence_q15, __entry->reason_flags)
);

TRACE_EVENT(parp_scan_budget_decision,
	TP_PROTO(const struct parp_scan_budget_trace *event),
	TP_ARGS(event),
	TP_STRUCT__entry(
		__field(u64, timestamp_ns)
		__field(u64, budget_sequence)
		__field(u8, reclaim_scope)
		__field(u64, domain_id)
		__field(u32, app_id)
		__field(bool, foreground)
		__field(u16, app_score_q15)
		__field(u16, app_rank)
		__field(u32, prediction_generation)
		__field(u32, model_version)
		__field(u64, prediction_age_ms)
		__field(u64, native_nr_to_scan)
		__field(u64, proposed_nr_to_scan)
		__field(u64, applied_nr_to_scan)
		__field(u32, multiplier_q15)
		__field(u8, pressure_level)
		__field(s32, reclaim_priority)
		__field(u8, reason)
		__field(u8, mode)
	),
	TP_fast_assign(
		__entry->timestamp_ns = event->timestamp_ns;
		__entry->budget_sequence = event->budget_sequence;
		__entry->reclaim_scope = event->reclaim_scope;
		__entry->domain_id = event->domain_id;
		__entry->app_id = event->app_id;
		__entry->foreground = event->foreground;
		__entry->app_score_q15 = event->app_score_q15;
		__entry->app_rank = event->app_rank;
		__entry->prediction_generation = event->prediction_generation;
		__entry->model_version = event->model_version;
		__entry->prediction_age_ms = event->prediction_age_ms;
		__entry->native_nr_to_scan = event->native_nr_to_scan;
		__entry->proposed_nr_to_scan = event->proposed_nr_to_scan;
		__entry->applied_nr_to_scan = event->applied_nr_to_scan;
		__entry->multiplier_q15 = event->multiplier_q15;
		__entry->pressure_level = event->pressure_level;
		__entry->reclaim_priority = event->reclaim_priority;
		__entry->reason = event->reason;
		__entry->mode = event->mode;
	),
	TP_printk("time=%llu sequence=%llu scope=%u domain=%llu app=%u foreground=%d score_q15=%u rank=%u generation=%u model=%u age_ms=%llu native_units=%llu proposed_units=%llu applied_units=%llu multiplier_q15=%u pressure=%u priority=%d reason=%u mode=%u",
		  __entry->timestamp_ns, __entry->budget_sequence,
		  __entry->reclaim_scope, __entry->domain_id, __entry->app_id,
		  __entry->foreground, __entry->app_score_q15, __entry->app_rank,
		  __entry->prediction_generation, __entry->model_version,
		  __entry->prediction_age_ms, __entry->native_nr_to_scan,
		  __entry->proposed_nr_to_scan, __entry->applied_nr_to_scan,
		  __entry->multiplier_q15, __entry->pressure_level,
		  __entry->reclaim_priority, __entry->reason, __entry->mode)
);

#endif
#include <trace/define_trace.h>
