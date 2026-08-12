/* SPDX-License-Identifier: GPL-2.0 */
#ifndef _LINUX_TIER2_WATERMARK_H
#define _LINUX_TIER2_WATERMARK_H

#include <linux/mmzone.h>
#include <linux/mm.h>
#include <linux/atomic.h>
#include <linux/jiffies.h>
#include <linux/seq_file.h>
#include <linux/kernfs.h>
#include <linux/spinlock.h>
#include <linux/workqueue.h>

struct mem_cgroup;
/* ================================================================
 * eBPF-compatible Markov prediction data structures
 * ================================================================
 * These structures are byte-compatible with the eBPF map definitions
 * from huawei_mem/zb/MGLRU/ebpf/cache_ext_bpf_common.h and the
 * CSV export format from lzx/operation_predictor/src/models/op_markov.py
 *
 * CSV format (huawei_mem compatible):
 *   Markov:  app_id,ctx0,ctx1,ctx2,ctx3,next_op,count
 *   Profile: app_id,op_id,dev,ino,index_start,index_end,priority
 *   History: app_id,op0,op1,op2,op3,length
 */

/* Operation history ring buffer (mirrors history_map)
 * Updated by userspace agent on each app operation switch.
 */
struct tier2_hist_entry {
	unsigned short app_id;
	unsigned short ops[4];      /* Most recent 4 operation IDs */
	unsigned char  length;      /* Number of valid entries (1-4) */
};

/* Markov transition table entry (mirrors markov_map)
 * 1-4 order Markov chain with high-to-low order fallback.
 * Populated from CSV exported by op_markov.py.
 */
struct tier2_markov_entry {
	unsigned short app_id;
	unsigned short ctx[4];      /* Markov context: last 1-4 operations */
	unsigned short next_op;     /* Predicted next operation ID */
	unsigned int   count;       /* Transition frequency (confidence) */
};

/* Page profile entry (mirrors profile_map)
 * Maps a predicted operation to its file page ranges.
 * Populated from CSV exported by page cache profiling.
 */
struct tier2_profile_entry {
	unsigned short app_id;
	unsigned short op_id;
	unsigned int   dev;         /* Device number (major:minor) */
	unsigned long long ino;     /* Inode number */
	unsigned long long index_start;  /* File page offset start */
	unsigned long long index_end;    /* File page offset end */
	unsigned short priority;         /* Page priority (0-100) */
};

/* Maximum entries for in-kernel prediction tables */
#define TIER2_MAX_HISTORY   16
#define TIER2_MAX_MARKOV   512
#define TIER2_MAX_PROFILES 128

#ifdef CONFIG_TIER2_WATERMARK

/* ================================================================
 * Per-node statistics (unchanged from original)
 * ================================================================ */

struct tier2_wmark_stats {
	atomic64_t below_alloc;
	atomic64_t below_demote;
	atomic64_t reclaim_wakeup;
	atomic64_t reclaim_target_adj;
	atomic64_t demote_attempt;
	atomic64_t demote_success;
	atomic64_t demote_fail;
	atomic64_t promotion_hint;
	atomic64_t promotion_success;
	atomic64_t pingpong_suspect;
};

struct tier2_wmark_ewma {
	unsigned long ewma_free_pages;
	unsigned long last_update_jiffies;
	unsigned long last_free_pages;
};

struct tier2_wmark_node {
	struct tier2_wmark_stats stats;
	struct tier2_wmark_ewma ewma;
};

/* ================================================================
 * Node-level API
 * ================================================================ */

bool tier2_wmark_enabled(void);
unsigned long tier2_wmark_alloc_pages_pgdat(struct pglist_data *pgdat);
unsigned long tier2_wmark_demote_pages_pgdat(struct pglist_data *pgdat);
bool tier2_wmark_below_alloc(struct pglist_data *pgdat);
bool tier2_wmark_below_demote(struct pglist_data *pgdat);
void tier2_wmark_record_below(struct pglist_data *pgdat);
void tier2_wmark_record_reclaim_wakeup(struct pglist_data *pgdat);
void tier2_wmark_record_demote_attempt(struct pglist_data *pgdat, unsigned long nr_pages);
void tier2_wmark_record_demote_result(struct pglist_data *pgdat, unsigned long success, unsigned long fail);
void tier2_wmark_update_ewma(struct pglist_data *pgdat);
int tier2_wmark_state_show(struct seq_file *m, void *v);

extern int sysctl_tier2_wmark_enabled;
extern int sysctl_tier2_alloc_scale_factor;
extern int sysctl_tier2_demote_scale_factor;

#else /* !CONFIG_TIER2_WATERMARK */

static inline bool tier2_wmark_enabled(void) { return false; }
static inline unsigned long tier2_wmark_alloc_pages_pgdat(struct pglist_data *pgdat) { return 0; }
static inline unsigned long tier2_wmark_demote_pages_pgdat(struct pglist_data *pgdat) { return 0; }
static inline bool tier2_wmark_below_alloc(struct pglist_data *pgdat) { return false; }
static inline bool tier2_wmark_below_demote(struct pglist_data *pgdat) { return false; }
static inline void tier2_wmark_record_below(struct pglist_data *pgdat) { }
static inline void tier2_wmark_record_reclaim_wakeup(struct pglist_data *pgdat) { }
static inline void tier2_wmark_record_demote_attempt(struct pglist_data *pgdat, unsigned long nr_pages) { }
static inline void tier2_wmark_record_demote_result(struct pglist_data *pgdat, unsigned long s, unsigned long f) { }
static inline void tier2_wmark_update_ewma(struct pglist_data *pgdat) { }

#endif /* CONFIG_TIER2_WATERMARK */

#ifdef CONFIG_TIER2_WATERMARK_MEMCG

/* ================================================================
 * Per-memcg tier2 watermark state
 * ================================================================
 * Each memory cgroup can have its own tier2 watermark configuration.
 * When enabled, watermarks are calculated based on the memcg's
 * memory.limit_in_bytes instead of the node's managed_pages.
 *
 * headroom = limit_bytes - current_usage_bytes
 * alloc_wmark = max(limit * alloc_scale / 10000, page_size)
 * demote_wmark = max(alloc_wmark, limit * demote_scale / 10000)
 * below_alloc  = (headroom < alloc_wmark)
 * below_demote = (headroom < demote_wmark)
 *
 * If limit is unlimited (PAGE_COUNTER_MAX), per-memcg tier2 is disabled
 * and falls back to global node-level watermarks.
 */
struct tier2_wmark_memcg {
	/* Configuration: writable by userspace */
	int		enabled;	/* 0=use global, 1=per-memcg */
	int		alloc_scale;	/* alloc scale in 1/10000 (default 100 = 1%) */
	int		demote_scale;	/* demote scale in 1/10000 (default 300 = 3%) */

	/* Computed watermarks (in bytes), updated on config change */
	unsigned long	alloc_wmark_bytes;
	unsigned long	demote_wmark_bytes;
	unsigned long	limit_bytes;	/* cached limit for watermark calc */

	/* Per-memcg statistics */
	atomic64_t	below_alloc;
	atomic64_t	below_demote;
	atomic64_t	reclaim_triggered;  /* total below_demote detections */
	atomic64_t	reclaim_actual;     /* actual reclaim invocations (rate-limited) */
	atomic64_t	reclaim_pages;      /* total pages reclaimed (cumulative) */
	atomic64_t	pressure_count;     /* reclaim pressure events counter */

	/* Back-pointer for workqueue access */
	struct mem_cgroup *memcg;

	/* Async reclaim work item */
	struct work_struct reclaim_work;
	/* Serializes offline with scheduling new reclaim work. */
	spinlock_t reclaim_lock;
	bool offline;
	/* Prediction: per-memcg EWMA for headroom tracking */
	unsigned long	ewma_headroom;
	unsigned long	last_headroom;
	unsigned long	last_ewma_jiffies;

	/* Prediction: proactive prediction delayed work */
	struct delayed_work predict_dwork;

	/* Prediction: statistics counters */
	atomic64_t	predict_scheduled;
	atomic64_t	predict_executed;
	atomic64_t	predict_pages_protected;
	atomic64_t	predict_pages_demoted;
};

/* ================================================================
 * Per-memcg API (NEW)
 * ================================================================ */

/* Allocate and initialize per-memcg tier2 state (incl workqueue) */
int tier2_wmark_memcg_alloc(struct mem_cgroup *memcg);
void tier2_wmark_memcg_offline(struct mem_cgroup *memcg);

/* Free per-memcg tier2 state (cancel pending work) */
void tier2_wmark_memcg_free(struct mem_cgroup *memcg);

/* Update per-memcg watermarks based on current limit */
void tier2_wmark_memcg_update(struct mem_cgroup *memcg);

/* Check and record per-memcg watermark status (observation only).
 * Returns true if below demote watermark. */
bool tier2_wmark_memcg_check(struct mem_cgroup *memcg);

/* Check watermarks AND schedule async reclaim if needed.
 * Called from done_restock in try_charge_memcg().
 * Uses workqueue for async reclaim to avoid blocking the charge path.
 * Returns true if reclaim work was scheduled. */
bool tier2_wmark_memcg_check_and_reclaim(struct mem_cgroup *memcg);

/* Prediction: update per-memcg EWMA headroom */
void tier2_wmark_update_ewma_memcg(struct mem_cgroup *memcg);

/* Prediction: predict time (ms) until demote watermark is reached */
long tier2_wmark_predict_time_to_wmark(struct mem_cgroup *memcg);

/* Prediction: proactively adjust page priorities based on prediction */
void tier2_predict_adjust_pages(struct mem_cgroup *memcg);
int tier2_predict_load_markov_csv(const char __user *buf, size_t len);
int tier2_predict_load_profile_csv(const char __user *buf, size_t len);
void tier2_predict_clear_tables(void);

/* Prediction sysctl globals */
extern int sysctl_tier2_predict_enabled;
extern int sysctl_tier2_predict_latency_ms;
extern int sysctl_tier2_predict_horizon_ratio;

/* ================================================================
 * Per-memcg cgroup v1 file show/write handlers (NEW)
 * ================================================================ */

int tier2_memcg_enabled_show(struct seq_file *m, void *v);
ssize_t tier2_memcg_enabled_write(struct kernfs_open_file *of,
		char *buf, size_t nbytes, loff_t off);

int tier2_memcg_alloc_scale_show(struct seq_file *m, void *v);
ssize_t tier2_memcg_alloc_scale_write(struct kernfs_open_file *of,
		char *buf, size_t nbytes, loff_t off);

int tier2_memcg_demote_scale_show(struct seq_file *m, void *v);
ssize_t tier2_memcg_demote_scale_write(struct kernfs_open_file *of,
		char *buf, size_t nbytes, loff_t off);

int tier2_memcg_alloc_wmark_show(struct seq_file *m, void *v);
int tier2_memcg_demote_wmark_show(struct seq_file *m, void *v);
int tier2_memcg_headroom_show(struct seq_file *m, void *v);
int tier2_memcg_below_show(struct seq_file *m, void *v);
int tier2_memcg_stats_show(struct seq_file *m, void *v);

#else /* !CONFIG_TIER2_WATERMARK_MEMCG */

static inline int tier2_wmark_memcg_alloc(struct mem_cgroup *memcg) { return 0; }
static inline void tier2_wmark_memcg_offline(struct mem_cgroup *memcg) { }
static inline void tier2_wmark_memcg_free(struct mem_cgroup *memcg) { }
static inline void tier2_wmark_memcg_update(struct mem_cgroup *memcg) { }
static inline bool tier2_wmark_memcg_check(struct mem_cgroup *memcg) { return false; }
static inline bool tier2_wmark_memcg_check_and_reclaim(struct mem_cgroup *memcg) { return false; }
static inline void tier2_wmark_update_ewma_memcg(struct mem_cgroup *memcg) { }
static inline long tier2_wmark_predict_time_to_wmark(struct mem_cgroup *memcg) { return -1; }
static inline void tier2_predict_adjust_pages(struct mem_cgroup *memcg) { }
static inline int tier2_predict_load_markov_csv(const char __user *buf, size_t len) { return -ENOSYS; }
static inline int tier2_predict_load_profile_csv(const char __user *buf, size_t len) { return -ENOSYS; }
static inline void tier2_predict_clear_tables(void) { }

#endif /* CONFIG_TIER2_WATERMARK_MEMCG */
#endif /* _LINUX_TIER2_WATERMARK_H */
