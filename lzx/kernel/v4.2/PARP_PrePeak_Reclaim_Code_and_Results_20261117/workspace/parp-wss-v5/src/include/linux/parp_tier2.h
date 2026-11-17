/* SPDX-License-Identifier: GPL-2.0 */
#ifndef _LINUX_PARP_TIER2_H
#define _LINUX_PARP_TIER2_H

#include <linux/atomic.h>
#include <linux/errno.h>
#include <linux/mutex.h>
#include <linux/parp_wss.h>
#include <linux/workqueue.h>

struct kernfs_open_file;
struct mem_cgroup;
struct seq_file;

/* lzx-note: The model implementation is supplied by the prediction owner. */
typedef int (*parp_tier2_predictor_fn)(u64 list_num, u64 op_id, u64 dram_num,
				     u64 *wss, u64 *pgfault);

struct parp_tier2_predictor_input {
	u64 list_num;
	u64 op_id;
	u64 dram_num; /* memory.max - RSS in bytes; supplied by the caller. */
};

struct parp_tier2_predictor_result {
	u64 wss; /* Predicted next-step working-set bytes. */
	u64 pgfault; /* Retained for observation; no fault-policy threshold yet. */
};

/* The existing WSS controller also needs these caller-supplied estimates. */
struct parp_tier2_predictor_context {
	u64 predicted_resident_bytes; /* Resident portion of the predicted set. */
	u64 timestamp_ns; /* CLOCK_MONOTONIC, for this model invocation. */
	u64 ttl_ns;
	u32 generation; /* Strictly increasing for this memcg. */
	u16 confidence_q15;
	u16 estimator_version;
};

struct parp_tier2_model_prediction {
	struct parp_tier2_predictor_input input;
	struct parp_tier2_predictor_result result;
	struct parp_tier2_predictor_context context;
};

#if defined(CONFIG_PARP_TIER2_WSS_PREDICT)
/* Call only in process context, outside reclaim/charge hooks and spinlocks.
 * The caller must hold a live memcg reference and keep predict callable.
 */
int parp_tier2_call_predictor(struct mem_cgroup *memcg,
		const struct parp_tier2_predictor_input *input,
		const struct parp_tier2_predictor_context *context,
		parp_tier2_predictor_fn predict);
/* A future userspace bridge can deliver an already-computed result here. */
int parp_tier2_submit_prediction(struct mem_cgroup *memcg,
		const struct parp_tier2_model_prediction *prediction);
#else
static inline int parp_tier2_call_predictor(struct mem_cgroup *memcg,
		const struct parp_tier2_predictor_input *input,
		const struct parp_tier2_predictor_context *context,
		parp_tier2_predictor_fn predict)
{
	return -EOPNOTSUPP;
}
static inline int parp_tier2_submit_prediction(struct mem_cgroup *memcg,
		const struct parp_tier2_model_prediction *prediction)
{
	return -EOPNOTSUPP;
}
#endif

/* Reclaim-bin counters make APPLY coverage auditable across cgroup children. */
struct parp_reclaim_bin_stats { /* lzx-note */
	u64 lookups;
	u64 policy_hits;
	u64 inherited_hits;
	u64 context_hits;
	u64 headroom_hits;
	u64 rank_scores;
	u64 headroom_fallback_scores;
	u64 rebin_moves;
	u64 subtree_passes;
	u64 subtree_selected;
	u64 subtree_skipped;
	u64 scored;
	u64 fallbacks;
	u64 score_bins[8];
}; /* lzx-note */

struct parp_reclaim_cold_stats { /* lzx-note */
	u64 passes;
	u64 scanned;
	u64 reclaimed;
	u64 priority_boost_total;
	u64 writepage_promotions;
	u64 swappiness_promotions;
	u64 swappiness_demotions; /* lzx-note */
	u64 workload_profile_hits; /* lzx-note */
	u64 workload_profile_misses; /* lzx-note */
	u64 anon_heavy_passes; /* lzx-note */
	u64 file_clean_passes; /* lzx-note */
	u64 file_dirty_passes; /* lzx-note */
	u64 mixed_passes; /* lzx-note */
	u64 bin_passes[8];
}; /* lzx-note */

#ifdef CONFIG_PARP_TIER2
/* All controller entry points are sleepable; bin readers use live counters. */
struct parp_tier2_memcg {
	struct mutex control_mutex;
	struct delayed_work predict_work;
	struct mem_cgroup *memcg;
	bool online;
	bool enabled;
	bool forecast_present;
	bool profile_present;
	bool model_prediction_present;
	bool high_owned;
	u32 generation;
	u32 blocked_generation;
	u32 no_progress;
	enum parp_wss_reason stop_reason;
	u64 admin_write_seq;
	u64 saved_admin_seq;
	unsigned long saved_high;
	unsigned long applied_high;
	u64 cooldown_until_ns;
	u64 refault_window_ns;
	u64 refault_baseline;
	u64 window_reclaimed_pages;
	u64 reclaim_invocations;
	u64 reclaimed_pages;
	u64 requested_pages;
	u64 forecasts_received;
	u64 high_updates;
	u64 high_restores;
	u64 no_progress_stops;
	u64 refault_stops;
	struct parp_wss_forecast forecast;
	struct parp_wss_profile profile;
	struct parp_wss_decision decision;
	struct parp_wss_cycle cycle;
	struct parp_tier2_model_prediction model_prediction;
};

void parp_tier2_memcg_init(struct mem_cgroup *memcg);
void parp_tier2_memcg_online(struct mem_cgroup *memcg);
void parp_tier2_memcg_offline(struct mem_cgroup *memcg);
void parp_tier2_memcg_reset(struct mem_cgroup *memcg);
void parp_tier2_memcg_destroy(struct mem_cgroup *memcg);
bool parp_tier2_runtime_enabled(void);
u64 parp_tier2_limit_bytes(struct mem_cgroup *memcg);
u64 parp_tier2_usage_bytes(struct mem_cgroup *memcg);
void parp_tier2_high_user_begin(struct mem_cgroup *memcg);
void parp_tier2_high_user_end(struct mem_cgroup *memcg);
#ifdef CONFIG_PARP_RECLAIM_BIN_SCORE /* lzx-note */
bool parp_tier2_reclaim_bin_apply_enabled(void); /* lzx-note */
bool parp_tier2_reclaim_bin_order_enabled(struct mem_cgroup *memcg); /* lzx-note */
bool parp_tier2_reclaim_bin_score(struct mem_cgroup *memcg,
				  u32 *score_q15,
				  bool *prediction_backed); /* lzx-note */
u32 parp_tier2_reclaim_rank_score_q15(u16 app_prior_q15, u16 app_rank,
				      bool foreground); /* lzx-note */
void parp_tier2_reclaim_bin_stats_snapshot(
		struct parp_reclaim_bin_stats *stats); /* lzx-note */
void parp_tier2_reclaim_bin_note_rebin(void); /* lzx-note */
void parp_tier2_reclaim_bin_note_subtree_pass(void); /* lzx-note */
void parp_tier2_reclaim_bin_note_subtree(bool selected); /* lzx-note */
#endif /* CONFIG_PARP_RECLAIM_BIN_SCORE */ /* lzx-note */

#ifdef CONFIG_PARP_RECLAIM_COLD_AGGRESSIVE /* lzx-note */
bool parp_tier2_reclaim_cold_aggressive_apply_enabled(void); /* lzx-note */
bool parp_tier2_reclaim_workload_apply_enabled(void); /* lzx-note */
bool parp_tier2_reclaim_cold_pressure(struct mem_cgroup *memcg,
				       unsigned int bin, u8 *priority_boost,
				       u16 *swappiness, bool *allow_writepage,
				       u8 *workload_class); /* lzx-note */
void parp_tier2_reclaim_cold_note(unsigned int bin, u8 priority_boost,
				  bool writepage_promoted,
				  int swappiness_delta, u8 workload_class,
				  unsigned long scanned,
				  unsigned long reclaimed); /* lzx-note */
void parp_tier2_reclaim_cold_stats_snapshot(
		struct parp_reclaim_cold_stats *stats); /* lzx-note */
#endif /* CONFIG_PARP_RECLAIM_COLD_AGGRESSIVE */ /* lzx-note */

int parp_tier2_enabled_show(struct seq_file *m, void *v);
ssize_t parp_tier2_enabled_write(struct kernfs_open_file *of, char *buf,
				 size_t nbytes, loff_t off);
int parp_tier2_stats_show(struct seq_file *m, void *v);
int parp_tier2_profile_show(struct seq_file *m, void *v);
ssize_t parp_tier2_profile_write(struct kernfs_open_file *of, char *buf,
			       size_t nbytes, loff_t off);
ssize_t parp_tier2_forecast_write(struct kernfs_open_file *of, char *buf,
				size_t nbytes, loff_t off);
#else
static inline void parp_tier2_memcg_init(struct mem_cgroup *m) { }
static inline void parp_tier2_memcg_online(struct mem_cgroup *m) { }
static inline void parp_tier2_memcg_offline(struct mem_cgroup *m) { }
static inline void parp_tier2_memcg_reset(struct mem_cgroup *m) { }
static inline void parp_tier2_memcg_destroy(struct mem_cgroup *m) { }
static inline bool parp_tier2_runtime_enabled(void) { return false; }
static inline void parp_tier2_high_user_begin(struct mem_cgroup *m) { }
static inline void parp_tier2_high_user_end(struct mem_cgroup *m) { }
#endif

#if defined(CONFIG_PARP_TIER2_WSS_PREDICT)
int parp_tier2_submit_forecast(struct mem_cgroup *memcg,
			     const struct parp_wss_forecast *forecast);
int parp_tier2_set_profile(struct mem_cgroup *memcg,
			 const struct parp_wss_profile *profile);
#else
static inline int parp_tier2_submit_forecast(struct mem_cgroup *m,
				const struct parp_wss_forecast *f)
{ return -EOPNOTSUPP; }
static inline int parp_tier2_set_profile(struct mem_cgroup *m,
			       const struct parp_wss_profile *p)
{ return -EOPNOTSUPP; }
#endif
#endif /* _LINUX_PARP_TIER2_H */
