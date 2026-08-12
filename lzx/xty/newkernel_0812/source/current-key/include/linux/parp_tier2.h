/* SPDX-License-Identifier: GPL-2.0 */
#ifndef _LINUX_PARP_TIER2_H
#define _LINUX_PARP_TIER2_H

#include <linux/atomic.h>
#include <linux/spinlock.h>
#include <linux/workqueue.h>

struct kernfs_open_file;
struct mem_cgroup;
struct seq_file;

#ifdef CONFIG_PARP

/*
 * All mutable prediction state is embedded in a memcg.  In particular, no
 * EWMA sample is shared by siblings, which keeps differently paced workloads
 * from contaminating each other's arrival estimates.
 */
struct parp_tier2_memcg {
	/* Serializes the predictor values and timer deadline below. */
	spinlock_t lock;
	struct delayed_work predict_work;
	struct mem_cgroup *memcg;

	bool online;
	bool enabled;
	bool ewma_valid;
	u32 alloc_scale;
	u32 demote_scale;

	u64 limit_bytes;
	u64 alloc_wmark_bytes;
	u64 demote_wmark_bytes;
	u64 headroom_bytes;
	u64 ewma_headroom_bytes;
	u64 previous_ewma_headroom_bytes;
	u64 last_sample_ns;
	u64 scheduled_for_ns;
	s64 predicted_ms;

	atomic64_t samples;
	atomic64_t below_alloc;
	atomic64_t below_demote;
	atomic64_t predict_scheduled;
	atomic64_t predict_executed;
	atomic64_t predict_cancelled;
	atomic64_t reclaim_invocations;
	atomic64_t reclaim_pages;
};

void parp_tier2_memcg_init(struct mem_cgroup *memcg);
void parp_tier2_memcg_online(struct mem_cgroup *memcg);
void parp_tier2_memcg_offline(struct mem_cgroup *memcg);
void parp_tier2_memcg_reset(struct mem_cgroup *memcg);
void parp_tier2_memcg_destroy(struct mem_cgroup *memcg);
void parp_tier2_memcg_sample(struct mem_cgroup *memcg);
void parp_tier2_memcg_charge(struct mem_cgroup *memcg);

int parp_tier2_enabled_show(struct seq_file *m, void *v);
ssize_t parp_tier2_enabled_write(struct kernfs_open_file *of, char *buf,
				 size_t nbytes, loff_t off);
int parp_tier2_alloc_scale_show(struct seq_file *m, void *v);
ssize_t parp_tier2_alloc_scale_write(struct kernfs_open_file *of, char *buf,
				     size_t nbytes, loff_t off);
int parp_tier2_demote_scale_show(struct seq_file *m, void *v);
ssize_t parp_tier2_demote_scale_write(struct kernfs_open_file *of, char *buf,
				      size_t nbytes, loff_t off);
int parp_tier2_alloc_wmark_show(struct seq_file *m, void *v);
int parp_tier2_demote_wmark_show(struct seq_file *m, void *v);
int parp_tier2_headroom_show(struct seq_file *m, void *v);
int parp_tier2_below_show(struct seq_file *m, void *v);
int parp_tier2_stats_show(struct seq_file *m, void *v);

#else /* !CONFIG_PARP */

static inline void parp_tier2_memcg_init(struct mem_cgroup *memcg) { }
static inline void parp_tier2_memcg_online(struct mem_cgroup *memcg) { }
static inline void parp_tier2_memcg_offline(struct mem_cgroup *memcg) { }
static inline void parp_tier2_memcg_reset(struct mem_cgroup *memcg) { }
static inline void parp_tier2_memcg_destroy(struct mem_cgroup *memcg) { }
static inline void parp_tier2_memcg_sample(struct mem_cgroup *memcg) { }
static inline void parp_tier2_memcg_charge(struct mem_cgroup *memcg) { }

#endif /* CONFIG_PARP */

#endif /* _LINUX_PARP_TIER2_H */
