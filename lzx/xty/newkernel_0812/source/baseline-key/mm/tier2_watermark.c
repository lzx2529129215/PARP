// SPDX-License-Identifier: GPL-2.0
/*
 * Tier-2 Watermark for Proactive Demotion and Memory Tiering
 *
 * Implements a secondary watermark system on top of the existing
 * min/low/high/promo zone watermarks. Provides alloc/demote watermarks
 * at the node level, statistics, EWMA-based prediction, and a
 * sysfs state interface for userspace aging/prediction tools.
 *
 * Designed to work with the existing memory-tiers infrastructure
 * (WMARK_PROMO, node_is_toptier, next_demotion_node, demote_folio_list).
 */

#include <linux/kernel.h>
#include <linux/mm.h>
#include <linux/mmzone.h>
#include <linux/swap.h>
#include <linux/vmstat.h>
#include <linux/atomic.h>
#include <linux/seq_file.h>
#include <linux/sysfs.h>
#include <linux/kobject.h>
#include <linux/module.h>
#include <linux/nodemask.h>
#include <linux/memory-tiers.h>
#include <linux/tier2_watermark.h>
#include <linux/sched/sysctl.h>
#include <linux/ktime.h>
#include <linux/math64.h>

#include <linux/memcontrol.h>
#include <linux/page_counter.h>
#include "internal.h"
#include <linux/mm_inline.h>
#include <linux/page-flags.h>
#include <linux/uaccess.h>
#include <linux/string.h>

#ifdef CONFIG_TIER2_WATERMARK_MEMCG
#include <linux/sysctl.h>
#endif

/* Global configuration - accessible via /proc/sys/vm/ */
int sysctl_tier2_wmark_enabled __read_mostly = 0;
int sysctl_tier2_alloc_scale_factor __read_mostly = 100;   /* 1.00% */
int sysctl_tier2_demote_scale_factor __read_mostly = 300;  /* 3.00% */

/* Global vmstat-like tier2 counters (sum across all memcgs) */
atomic64_t tier2_global_below_alloc = ATOMIC64_INIT(0);
atomic64_t tier2_global_below_demote = ATOMIC64_INIT(0);
atomic64_t tier2_global_reclaim_actual = ATOMIC64_INIT(0);
atomic64_t tier2_global_reclaim_pages = ATOMIC64_INIT(0);
EXPORT_SYMBOL_GPL(tier2_global_below_alloc);
EXPORT_SYMBOL_GPL(tier2_global_below_demote);
EXPORT_SYMBOL_GPL(tier2_global_reclaim_actual);
EXPORT_SYMBOL_GPL(tier2_global_reclaim_pages);

#ifdef CONFIG_TIER2_WATERMARK_MEMCG
static struct tier2_hist_entry hist_table[TIER2_MAX_HISTORY];
static int hist_count;
static struct tier2_markov_entry markov_table[TIER2_MAX_MARKOV];
static int markov_count;
static struct tier2_profile_entry profile_table[TIER2_MAX_PROFILES];
static int profile_count;
static atomic64_t tier2_ebpf_markov_hits = ATOMIC64_INIT(0);
static atomic64_t tier2_ebpf_markov_misses = ATOMIC64_INIT(0);
static atomic64_t tier2_ebpf_profile_hits = ATOMIC64_INIT(0);
static atomic64_t tier2_ebpf_profile_misses = ATOMIC64_INIT(0);
#endif

/* Per-node tier2 watermark state */
static struct tier2_wmark_node *tier2_wmark_nodes __read_mostly;

/* EWMA decay factor: alpha = 1/16 -> ~16-sample window */
#define TIER2_EWMA_SHIFT 4

/* sysfs kobject */
static struct kobject *tier2_wmark_kobj;

#ifdef CONFIG_TIER2_WATERMARK
int sysctl_tier2_predict_enabled __read_mostly = 0;
int sysctl_tier2_predict_latency_ms __read_mostly = 100;
int sysctl_tier2_predict_horizon_ratio __read_mostly = 3;
#endif /* CONFIG_TIER2_WATERMARK */



bool tier2_wmark_enabled(void)
{
	return !!READ_ONCE(sysctl_tier2_wmark_enabled);
}
EXPORT_SYMBOL_GPL(tier2_wmark_enabled);

/*
 * Determine the role of a node: top, slow, or unknown.
 * Uses existing memory-tiers infrastructure.
 */
static __maybe_unused const char *tier2_wmark_node_role(int nid)
{
	if (!node_state(nid, N_MEMORY))
		return "unknown";
	if (node_is_toptier(nid))
		return "top";
	if (node_state(nid, N_CPU))
		return "top";
	return "slow";
}

/*
 * Return the first managed zone for a pgdat, for watermark comparison.
 */
static struct zone *first_managed_zone_pgdat(struct pglist_data *pgdat)
{
	struct zone *zone;
	int z;

	for ((z) = 0, (zone) = (pgdat)->node_zones; (z) <= (ZONE_MOVABLE); (z)++, (zone)++) if (managed_zone(zone))
		return zone;
	return NULL;
}

/*
 * Sum managed pages across managed zones in a node.
 */
static unsigned long tier2_managed_pages_pgdat(struct pglist_data *pgdat)
{
	unsigned long managed = 0;
	struct zone *zone;
	int z;

	for ((z) = 0, (zone) = (pgdat)->node_zones; (z) <= (ZONE_MOVABLE); (z)++, (zone)++) if (managed_zone(zone))
		managed += zone_managed_pages(zone);

	return managed;
}

/*
 * Calculate the alloc watermark for a pgdat (in pages).
 */
unsigned long tier2_wmark_alloc_pages_pgdat(struct pglist_data *pgdat)
{
	unsigned long managed = tier2_managed_pages_pgdat(pgdat);
	struct zone *zone;

	if (!managed)
		return 0;

	zone = first_managed_zone_pgdat(pgdat);
	if (!zone)
		return 0;

	return max(high_wmark_pages(zone),
		   managed * READ_ONCE(sysctl_tier2_alloc_scale_factor) / 10000);
}
EXPORT_SYMBOL_GPL(tier2_wmark_alloc_pages_pgdat);

/*
 * Calculate the demote watermark for a pgdat (in pages).
 * Must be >= alloc watermark.
 */
unsigned long tier2_wmark_demote_pages_pgdat(struct pglist_data *pgdat)
{
	unsigned long managed = tier2_managed_pages_pgdat(pgdat);
	unsigned long alloc_wmark = tier2_wmark_alloc_pages_pgdat(pgdat);

	if (!managed)
		return 0;

	return max(alloc_wmark,
		   managed * READ_ONCE(sysctl_tier2_demote_scale_factor) / 10000);
}
EXPORT_SYMBOL_GPL(tier2_wmark_demote_pages_pgdat);

/*
 * Get aggregate free pages for a node.
 */
static unsigned long tier2_wmark_node_free_pages(struct pglist_data *pgdat)
{
	unsigned long free_pages = 0;
	struct zone *zone;
	int z;

	for ((z) = 0, (zone) = (pgdat)->node_zones; (z) <= (ZONE_MOVABLE); (z)++, (zone)++) if (managed_zone(zone))
		free_pages += zone_page_state(zone, NR_FREE_PAGES);

	return free_pages;
}

/*
 * Check if node free pages are below alloc watermark.
 */
bool tier2_wmark_below_alloc(struct pglist_data *pgdat)
{
	unsigned long free_pages, alloc_wmark;

	if (!tier2_wmark_enabled())
		return false;

	free_pages = tier2_wmark_node_free_pages(pgdat);
	alloc_wmark = tier2_wmark_alloc_pages_pgdat(pgdat);

	return free_pages < alloc_wmark;
}
EXPORT_SYMBOL_GPL(tier2_wmark_below_alloc);

/*
 * Check if node free pages are below demote watermark.
 */
bool tier2_wmark_below_demote(struct pglist_data *pgdat)
{
	unsigned long free_pages, demote_wmark;

	if (!tier2_wmark_enabled())
		return false;

	free_pages = tier2_wmark_node_free_pages(pgdat);
	demote_wmark = tier2_wmark_demote_pages_pgdat(pgdat);

	return free_pages < demote_wmark;
}
EXPORT_SYMBOL_GPL(tier2_wmark_below_demote);

/*
 * Record that free pages dropped below some watermark.
 */
void tier2_wmark_record_below(struct pglist_data *pgdat)
{
	int nid = pgdat->node_id;

	if (!tier2_wmark_enabled() || !tier2_wmark_nodes)
		return;

	if (tier2_wmark_below_alloc(pgdat))
		atomic64_inc(&tier2_wmark_nodes[nid].stats.below_alloc);
	if (tier2_wmark_below_demote(pgdat))
		atomic64_inc(&tier2_wmark_nodes[nid].stats.below_demote);
}
EXPORT_SYMBOL_GPL(tier2_wmark_record_below);

/*
 * Record a reclaim wakeup due to tier2 watermark.
 */
void tier2_wmark_record_reclaim_wakeup(struct pglist_data *pgdat)
{
	int nid = pgdat->node_id;

	if (!tier2_wmark_enabled() || !tier2_wmark_nodes)
		return;

	atomic64_inc(&tier2_wmark_nodes[nid].stats.reclaim_wakeup);
}
EXPORT_SYMBOL_GPL(tier2_wmark_record_reclaim_wakeup);

/*
 * Record a demotion attempt.
 */
void tier2_wmark_record_demote_attempt(struct pglist_data *pgdat,
				       unsigned long nr_pages)
{
	int nid = pgdat->node_id;

	if (!tier2_wmark_enabled() || !tier2_wmark_nodes)
		return;

	atomic64_inc(&tier2_wmark_nodes[nid].stats.demote_attempt);
}
EXPORT_SYMBOL_GPL(tier2_wmark_record_demote_attempt);

/*
 * Record demotion result (success/fail counts).
 */
void tier2_wmark_record_demote_result(struct pglist_data *pgdat,
				      unsigned long success,
				      unsigned long fail)
{
	int nid = pgdat->node_id;

	if (!tier2_wmark_enabled() || !tier2_wmark_nodes)
		return;

	if (success)
		atomic64_add(success, &tier2_wmark_nodes[nid].stats.demote_success);
	if (fail)
		atomic64_add(fail, &tier2_wmark_nodes[nid].stats.demote_fail);
}
EXPORT_SYMBOL_GPL(tier2_wmark_record_demote_result);

/*
 * Update EWMA for a node. Uses exponential weighted moving average
 * with alpha = 1/16, giving a ~16-read smoothing window.
 * Returns the new EWMA value.
 */
void tier2_wmark_update_ewma(struct pglist_data *pgdat)
{
	int nid = pgdat->node_id;
	unsigned long free_pages, prev_ewma;

	if (!tier2_wmark_enabled() || !tier2_wmark_nodes)
		return;

	free_pages = tier2_wmark_node_free_pages(pgdat);
	prev_ewma = tier2_wmark_nodes[nid].ewma.ewma_free_pages;

	if (unlikely(!prev_ewma)) {
		tier2_wmark_nodes[nid].ewma.ewma_free_pages = free_pages;
	} else {
		/* EWMA = prev * (1 - 1/16) + sample * (1/16) */
		tier2_wmark_nodes[nid].ewma.ewma_free_pages =
			((prev_ewma << TIER2_EWMA_SHIFT) - prev_ewma +
			 free_pages) >> TIER2_EWMA_SHIFT;
	}

	tier2_wmark_nodes[nid].ewma.last_free_pages = free_pages;
	tier2_wmark_nodes[nid].ewma.last_update_jiffies = jiffies;
}
EXPORT_SYMBOL_GPL(tier2_wmark_update_ewma);

/*
 * sysfs state file: /sys/kernel/mm/tier2_watermark/state
 */
int tier2_wmark_state_show(struct seq_file *m, void *v)
{
	int nid;
	unsigned long now_jiffies __maybe_unused = jiffies;
	int enabled = READ_ONCE(sysctl_tier2_wmark_enabled);

	seq_printf(m, "version=1\n");
	seq_printf(m, "timestamp_ns=%llu\n", ktime_get_ns());
	seq_printf(m, "enabled=%d\n", enabled);

	for_each_online_node(nid) {
		struct pglist_data *pgdat = NODE_DATA(nid);
		struct zone *zone;
		int z;
		long predicted_alloc_sec __maybe_unused = -1;
		long predicted_demote_sec __maybe_unused = -1;

		if (!pgdat)
			continue;

		if (enabled && tier2_wmark_nodes)
			tier2_wmark_update_ewma(pgdat);

		for ((z) = 0, (zone) = (pgdat)->node_zones; (z) <= (ZONE_MOVABLE); (z)++, (zone)++) if (managed_zone(zone)) {
			unsigned long free_pages = zone_page_state(zone, NR_FREE_PAGES);
			unsigned long alloc_wmark =
				enabled ? tier2_wmark_alloc_pages_pgdat(pgdat) : 0;
			unsigned long demote_wmark =
				enabled ? tier2_wmark_demote_pages_pgdat(pgdat) : 0;
			unsigned long ewma_free = 0;

			if (enabled && tier2_wmark_nodes)
				ewma_free = tier2_wmark_nodes[nid].ewma.ewma_free_pages;

			/* Simple linear prediction based on EWMA-vs-current gap */
			if (enabled && ewma_free > 0 && tier2_wmark_nodes &&
			    tier2_wmark_nodes[nid].ewma.last_free_pages > 0) {
				unsigned long elapsed_jiffies =
					now_jiffies -
					tier2_wmark_nodes[nid].ewma.last_update_jiffies;
				unsigned long elapsed_sec =
					jiffies_to_msecs(elapsed_jiffies) / 1000;
				long delta = (long)free_pages -
					(long)tier2_wmark_nodes[nid].ewma.last_free_pages;

				if (free_pages < alloc_wmark) {
					predicted_alloc_sec = 0;
				} else if (delta < 0 && elapsed_sec > 0) {
					long rate = -delta / (long)max(elapsed_sec, 1UL);
					if (rate > 0)
						predicted_alloc_sec =
							(long)((free_pages - alloc_wmark) / rate);
				}

				if (free_pages < demote_wmark) {
					predicted_demote_sec = 0;
				} else if (delta < 0 && elapsed_sec > 0) {
					long rate = -delta / (long)max(elapsed_sec, 1UL);
					if (rate > 0)
						predicted_demote_sec =
							(long)((free_pages - demote_wmark) / rate);
				}
			}

			seq_printf(m,
				"node=%d\n"
				"zone=%s\n"
				"role=%s\n"
				"managed_pages=%lu\n"
				"free_pages=%lu\n"
				"min=%lu\n"
				"low=%lu\n"
				"high=%lu\n"
				"tier2_alloc_wmark=%lu\n"
				"tier2_demote_wmark=%lu\n"
				"below_alloc=%d\n"
				"below_demote=%d\n"
				"active_anon=%lu\n"
				"inactive_anon=%lu\n"
				"active_file=%lu\n"
				"inactive_file=%lu\n"
				"workingset_refault_anon=%ld\n"
				"workingset_refault_file=%ld\n"
				"pgdemote_success=%ld\n"
				"pgdemote_fail=%ld\n"
				"numa_pages_migrated=%ld\n"
				"ewma_free_pages=%lu\n"
				"predicted_seconds_to_alloc_wmark=%ld\n"
				"predicted_seconds_to_demote_wmark=%ld\n",
				nid,
				zone->name,
				tier2_wmark_node_role(nid),
				zone_managed_pages(zone),
				free_pages,
				min_wmark_pages(zone),
				low_wmark_pages(zone),
				high_wmark_pages(zone),
				alloc_wmark,
				demote_wmark,
				free_pages < alloc_wmark ? 1 : 0,
				free_pages < demote_wmark ? 1 : 0,
				node_page_state(pgdat, NR_ACTIVE_ANON),
				node_page_state(pgdat, NR_INACTIVE_ANON),
				node_page_state(pgdat, NR_ACTIVE_FILE),
				node_page_state(pgdat, NR_INACTIVE_FILE),
				(long)-1L /* vm_event, hard to get globally */,
				(long)-1L /* vm_event, hard to get globally */,
				(tier2_wmark_nodes ?
				 (long)atomic64_read(&tier2_wmark_nodes[nid].stats.demote_success) : -1L),
				(tier2_wmark_nodes ?
				 (long)atomic64_read(&tier2_wmark_nodes[nid].stats.demote_fail) : -1L),
				(long)-1L /* use NUMA_PAGE_MIGRATE vm_event instead */,
				ewma_free > 0 ? ewma_free : (unsigned long)-1,
				predicted_alloc_sec,
				predicted_demote_sec);
		}
	}

	return 0;
}
EXPORT_SYMBOL_GPL(tier2_wmark_state_show);

/* sysfs state file open/release */
static int tier2_wmark_state_open(struct inode *inode, struct file *file)
{
	return single_open(file, tier2_wmark_state_show, NULL);
}

static const struct file_operations tier2_wmark_state_fops = {
	.open		= tier2_wmark_state_open,
	.read		= seq_read,
	.llseek		= seq_lseek,
	.release	= single_release,
	.owner		= THIS_MODULE,
};

/* sysfs stats file */
static int tier2_wmark_stats_show(struct seq_file *m, void *v)
{
	int nid;

	seq_printf(m, "enabled=%d\n", READ_ONCE(sysctl_tier2_wmark_enabled));
	seq_printf(m, "alloc_scale_factor=%d\n",
		   READ_ONCE(sysctl_tier2_alloc_scale_factor));
	seq_printf(m, "demote_scale_factor=%d\n",
		   READ_ONCE(sysctl_tier2_demote_scale_factor));

	for_each_online_node(nid) {
		if (!tier2_wmark_nodes)
			break;

		seq_printf(m,
			"node=%d\n"
			"below_alloc=%ld\n"
			"below_demote=%ld\n"
			"reclaim_wakeup=%ld\n"
			"reclaim_target_adj=%ld\n"
			"demote_attempt=%ld\n"
			"demote_success=%ld\n"
			"demote_fail=%ld\n"
			"promotion_hint=%ld\n"
			"promotion_success=%ld\n"
			"pingpong_suspect=%ld\n",
			nid,
			(long)atomic64_read(&tier2_wmark_nodes[nid].stats.below_alloc),
			(long)atomic64_read(&tier2_wmark_nodes[nid].stats.below_demote),
			(long)atomic64_read(&tier2_wmark_nodes[nid].stats.reclaim_wakeup),
			(long)atomic64_read(&tier2_wmark_nodes[nid].stats.reclaim_target_adj),
			(long)atomic64_read(&tier2_wmark_nodes[nid].stats.demote_attempt),
			(long)atomic64_read(&tier2_wmark_nodes[nid].stats.demote_success),
			(long)atomic64_read(&tier2_wmark_nodes[nid].stats.demote_fail),
			(long)atomic64_read(&tier2_wmark_nodes[nid].stats.promotion_hint),
			(long)atomic64_read(&tier2_wmark_nodes[nid].stats.promotion_success),
			(long)atomic64_read(&tier2_wmark_nodes[nid].stats.pingpong_suspect));
	}

	return 0;
}

static int tier2_wmark_stats_open(struct inode *inode, struct file *file)
{
	return single_open(file, tier2_wmark_stats_show, NULL);
}

static const struct file_operations tier2_wmark_stats_fops = {
	.open		= tier2_wmark_stats_open,
	.read		= seq_read,
	.llseek		= seq_lseek,
	.release	= single_release,
	.owner		= THIS_MODULE,
};

/*
 * Create a sysfs file with custom file_operations.
 * Since we use seq_file-based fops, we create files as
 * "kernel objects" attributes manually.
 */
#ifdef CONFIG_DEBUG_FS
#include <linux/debugfs.h>
static const struct file_operations tier2_predict_data_fops;

static struct dentry *tier2_wmark_debugfs_dir;

static int __init tier2_wmark_debugfs_init(void)
{
	struct dentry *dentry;

	tier2_wmark_debugfs_dir = debugfs_create_dir("tier2_watermark", NULL);
	if (IS_ERR(tier2_wmark_debugfs_dir) || !tier2_wmark_debugfs_dir) {
		pr_warn("tier2_watermark: failed to create debugfs directory\n");
		return 0;
	}

	dentry = debugfs_create_file("state", 0444, tier2_wmark_debugfs_dir,
				     NULL, &tier2_wmark_state_fops);
	if (IS_ERR(dentry) || !dentry)
		pr_warn("tier2_watermark: failed to create debugfs state file\n");

	dentry = debugfs_create_file("stats", 0444, tier2_wmark_debugfs_dir, NULL, &tier2_wmark_stats_fops);
	dentry = debugfs_create_file("predict_data", 0200, tier2_wmark_debugfs_dir, NULL, &tier2_predict_data_fops);
	if (IS_ERR(dentry) || !dentry) pr_warn("tier2_watermark: failed to create debugfs predict_data file\n");
	if (IS_ERR(dentry) || !dentry)
		pr_warn("tier2_watermark: failed to create debugfs stats file\n");

	return 0;
}
#else
static inline int tier2_wmark_debugfs_init(void) { return 0; }
#endif

/*
 * Initialize per-node state.
 */
/* Forward declaration */

static int __init tier2_wmark_init(void)
{
	int nid;

	tier2_wmark_nodes = kcalloc(nr_node_ids, sizeof(*tier2_wmark_nodes),
				    GFP_KERNEL);
	if (!tier2_wmark_nodes) {
		pr_err("tier2_watermark: failed to allocate per-node state\n");
		return -ENOMEM;
	}

	/* Create /sys/kernel/mm/tier2_watermark/ for potential future use */
	tier2_wmark_kobj = kobject_create_and_add("tier2_watermark", mm_kobj);
	if (!tier2_wmark_kobj) {
		pr_warn("tier2_watermark: failed to create sysfs dir (non-fatal)\n");
	}

	/* Initialize EWMA */
	for_each_online_node(nid) {
		struct pglist_data *pgdat = NODE_DATA(nid);
		if (pgdat && tier2_wmark_nodes)
			tier2_wmark_nodes[nid].ewma.ewma_free_pages =
				tier2_wmark_node_free_pages(pgdat);
	}

	tier2_wmark_debugfs_init();

	/* Register per-memcg tier2 cgroup v1 files */

pr_info("tier2_watermark: initialized (enabled=%d, alloc_scale=%d.%02d%%, demote_scale=%d.%02d%%)\n",
		READ_ONCE(sysctl_tier2_wmark_enabled),
		READ_ONCE(sysctl_tier2_alloc_scale_factor) / 100,
		READ_ONCE(sysctl_tier2_alloc_scale_factor) % 100,
		READ_ONCE(sysctl_tier2_demote_scale_factor) / 100,
		READ_ONCE(sysctl_tier2_demote_scale_factor) % 100);

	return 0;
}

module_init(tier2_wmark_init);

#ifdef CONFIG_TIER2_WATERMARK_MEMCG

/* ================================================================
 * Per-memcg watermark calculation helpers
 * ================================================================ */

/*
 * Get the memory limit for a memcg in bytes.
 * Returns PAGE_COUNTER_MAX if unlimited.
 */
static unsigned long tier2_memcg_limit_bytes(struct mem_cgroup *memcg)
{
	unsigned long limit;

	if (!memcg)
		return PAGE_COUNTER_MAX;

	limit = READ_ONCE(memcg->memory.max);
	if (limit == PAGE_COUNTER_MAX)
		return PAGE_COUNTER_MAX;
	return limit * PAGE_SIZE;
}

/*
 * Get current memory usage for a memcg in bytes.
 */
static unsigned long tier2_memcg_usage_bytes(struct mem_cgroup *memcg)
{
	if (!memcg)
		return 0;

	return page_counter_read(&memcg->memory) * PAGE_SIZE;
}

/*
 * Compute per-memcg alloc watermark in bytes.
 *
 * alloc_wmark = max(PAGE_SIZE, limit * alloc_scale / 10000)
 *
 * If the memcg has no per-memcg tier2 enabled, falls back to global
 * settings (sysctl_tier2_alloc_scale_factor).
 */
static unsigned long tier2_memcg_alloc_wmark_bytes(struct mem_cgroup *memcg)
{
	struct tier2_wmark_memcg *tm;
	unsigned long limit;
	int scale;

	if (!memcg || !memcg->tier2_wmark)
		return 0;

	tm = memcg->tier2_wmark;
	limit = tier2_memcg_limit_bytes(memcg);

	/* Unlimited limit = per-memcg tier2 disabled */
	if (limit == PAGE_COUNTER_MAX || limit == 0)
		return 0;

	scale = tm->enabled ? tm->alloc_scale : READ_ONCE(sysctl_tier2_alloc_scale_factor);
	if (scale <= 0)
		return 0;

	return max(limit * (unsigned long)scale / 10000UL, (unsigned long)PAGE_SIZE);
}

/*
 * Compute per-memcg demote watermark in bytes.
 *
 * demote_wmark = max(alloc_wmark, limit * demote_scale / 10000)
 */
static unsigned long tier2_memcg_demote_wmark_bytes(struct mem_cgroup *memcg)
{
	struct tier2_wmark_memcg *tm;
	unsigned long limit, alloc_wmark;
	int scale;

	if (!memcg || !memcg->tier2_wmark)
		return 0;

	tm = memcg->tier2_wmark;
	limit = tier2_memcg_limit_bytes(memcg);

	if (limit == PAGE_COUNTER_MAX || limit == 0)
		return 0;

	alloc_wmark = tier2_memcg_alloc_wmark_bytes(memcg);
	scale = tm->enabled ? tm->demote_scale : READ_ONCE(sysctl_tier2_demote_scale_factor);
	if (scale <= 0)
		return alloc_wmark;

	return max(alloc_wmark, limit * (unsigned long)scale / 10000UL);
}

/*
 * Compute per-memcg headroom = limit - current_usage
 */
static unsigned long tier2_memcg_headroom_bytes(struct mem_cgroup *memcg)
{
	unsigned long limit, usage;

	if (!memcg)
		return 0;

	limit = tier2_memcg_limit_bytes(memcg);
	if (limit == PAGE_COUNTER_MAX)
		return ULONG_MAX;  /* unlimited => infinite headroom */

	usage = tier2_memcg_usage_bytes(memcg);
	if (usage >= limit)
		return 0;

	return limit - usage;
}

/* ================================================================
 * Per-memcg lifecycle
 * ================================================================ */

/*
 * Allocate and initialize per-memcg tier2 state.
 * Called from mem_cgroup_css_alloc().
 */
static void tier2_wmark_reclaim_work_fn(struct work_struct *work);
static void tier2_wmark_prediction_work_fn(struct work_struct *work);

int tier2_wmark_memcg_alloc(struct mem_cgroup *memcg)
{
	struct tier2_wmark_memcg *tm;

	if (!memcg)
		return -EINVAL;

	/* Don't allocate for root memcg (use global watermarks) */
	if (mem_cgroup_is_root(memcg))
		return 0;

	tm = kzalloc(sizeof(*tm), GFP_KERNEL);
	if (!tm)
		return -ENOMEM;

	/* Default: inherit global settings, but per-memcg is disabled */
	tm->enabled = 0;
	tm->alloc_scale = READ_ONCE(sysctl_tier2_alloc_scale_factor);
	tm->demote_scale = READ_ONCE(sysctl_tier2_demote_scale_factor);
	atomic64_set(&tm->below_alloc, 0);
	atomic64_set(&tm->below_demote, 0);
	atomic64_set(&tm->reclaim_triggered, 0);
	atomic64_set(&tm->reclaim_actual, 0);
	atomic64_set(&tm->reclaim_pages, 0);
	atomic64_set(&tm->pressure_count, 0);

	/* Back-pointer for workqueue access */
	tm->memcg = memcg;

	/* Initialize async reclaim work */
	INIT_WORK(&tm->reclaim_work, tier2_wmark_reclaim_work_fn);
	spin_lock_init(&tm->reclaim_lock);

	/* Initialize prediction delayed work and statistics */
	INIT_DELAYED_WORK(&tm->predict_dwork, tier2_wmark_prediction_work_fn);
	tm->ewma_headroom = 0;
	tm->last_headroom = 0;
	tm->last_ewma_jiffies = 0;
	atomic64_set(&tm->predict_scheduled, 0);
	atomic64_set(&tm->predict_executed, 0);
	atomic64_set(&tm->predict_pages_protected, 0);
	atomic64_set(&tm->predict_pages_demoted, 0);

	WRITE_ONCE(memcg->tier2_wmark, tm);
	return 0;
}

void tier2_wmark_memcg_offline(struct mem_cgroup *memcg)
{
	struct tier2_wmark_memcg *tm;

	if (!memcg)
		return;

	tm = READ_ONCE(memcg->tier2_wmark);
	if (!tm)
		return;

	spin_lock_irq(&tm->reclaim_lock);
	WRITE_ONCE(tm->enabled, 0);
	WRITE_ONCE(tm->offline, true);
	spin_unlock_irq(&tm->reclaim_lock);
	cancel_work_sync(&tm->reclaim_work);
	cancel_delayed_work_sync(&tm->predict_dwork);
}

/*
 * Free per-memcg tier2 state.
 * Called from mem_cgroup_css_free().
 */
void tier2_wmark_memcg_free(struct mem_cgroup *memcg)
{
	struct tier2_wmark_memcg *tm;

	if (!memcg)
		return;

	tm = READ_ONCE(memcg->tier2_wmark);
	if (!tm)
		return;

	/* Make new queue attempts fail, then wait for any running work. */
	spin_lock_irq(&tm->reclaim_lock);
	WRITE_ONCE(tm->enabled, 0);
	WRITE_ONCE(tm->offline, true);
	spin_unlock_irq(&tm->reclaim_lock);
	cancel_work_sync(&tm->reclaim_work);
	cancel_delayed_work_sync(&tm->predict_dwork);

	WRITE_ONCE(memcg->tier2_wmark, NULL);
	kfree(tm);
}

/*
 * Update per-memcg computed watermarks.
 * Called when limit or scale factors change.
 */
void tier2_wmark_memcg_update(struct mem_cgroup *memcg)
{
	struct tier2_wmark_memcg *tm;

	if (!memcg || !memcg->tier2_wmark)
		return;

	tm = memcg->tier2_wmark;
	tm->limit_bytes = tier2_memcg_limit_bytes(memcg);
	tm->alloc_wmark_bytes = tier2_memcg_alloc_wmark_bytes(memcg);
	tm->demote_wmark_bytes = tier2_memcg_demote_wmark_bytes(memcg);
}

/*
 * Check per-memcg tier2 watermark status.
 * Called from try_charge path after a successful charge.
 *
 * Returns true if headroom is below demote watermark
 * (signals that proactive reclaim may be beneficial).
 */
bool tier2_wmark_memcg_check(struct mem_cgroup *memcg)
{
	struct tier2_wmark_memcg *tm;
	unsigned long headroom, alloc_wmark, demote_wmark;
	bool below_alloc, below_demote;

	if (!memcg || !memcg->tier2_wmark)
		return false;

	tm = memcg->tier2_wmark;

	/* Skip if per-memcg tier2 is not enabled */
	if (!tm->enabled)
		return false;

	/* Skip root memcg */
	if (mem_cgroup_is_root(memcg))
		return false;

	/* Update watermarks (limit might have changed) */
	tm->limit_bytes = tier2_memcg_limit_bytes(memcg);
	if (tm->limit_bytes == PAGE_COUNTER_MAX || tm->limit_bytes == 0) {
		/* Unlimited cgroup: per-memcg tier2 is meaningless */
		return false;
	}

	tm->alloc_wmark_bytes = tier2_memcg_alloc_wmark_bytes(memcg);
	tm->demote_wmark_bytes = tier2_memcg_demote_wmark_bytes(memcg);

	if (tm->alloc_wmark_bytes == 0)
		return false;

	headroom = tier2_memcg_headroom_bytes(memcg);
	alloc_wmark = tm->alloc_wmark_bytes;
	demote_wmark = tm->demote_wmark_bytes;

	below_alloc = (headroom < alloc_wmark);
	below_demote = (headroom < demote_wmark);

	if (below_alloc)
		atomic64_inc(&tm->below_alloc);
	if (below_demote) {
		atomic64_inc(&tm->below_demote);
		atomic64_inc(&tm->reclaim_triggered);
	}

	return below_demote;
}

/*
 * Async reclaim work handler.
 * Called from workqueue when per-memcg tier2 detects memory pressure.
 * This runs asynchronously to avoid blocking the charge path.
 */
static void tier2_wmark_reclaim_work_fn(struct work_struct *work)
{
	struct tier2_wmark_memcg *tm =
		container_of(work, struct tier2_wmark_memcg, reclaim_work);
	struct mem_cgroup *memcg = tm->memcg;
	unsigned long headroom, demote_wmark, nr_to_reclaim;
	unsigned long usage_bytes;

	if (!memcg || !READ_ONCE(tm->enabled) || READ_ONCE(tm->offline))
		return;

	/* Refresh watermarks */
	tm->limit_bytes = READ_ONCE(memcg->memory.max);
	if (tm->limit_bytes == PAGE_COUNTER_MAX || tm->limit_bytes == 0)
		return;
	tm->limit_bytes *= PAGE_SIZE;

	tm->alloc_wmark_bytes = max((unsigned long)PAGE_SIZE,
		tm->limit_bytes * (unsigned long)tm->alloc_scale / 10000UL);
	tm->demote_wmark_bytes = max(tm->alloc_wmark_bytes,
		tm->limit_bytes * (unsigned long)tm->demote_scale / 10000UL);

	usage_bytes = page_counter_read(&memcg->memory) * PAGE_SIZE;
	headroom = (usage_bytes < tm->limit_bytes) ?
		(tm->limit_bytes - usage_bytes) : 0;
	demote_wmark = tm->demote_wmark_bytes;

	/* Re-check: still below demote watermark? */
	if (headroom >= demote_wmark || demote_wmark == 0)
		return;

	/* Calculate reclaim target */
	nr_to_reclaim = (demote_wmark - headroom) / PAGE_SIZE;
	if (nr_to_reclaim < SWAP_CLUSTER_MAX)
		nr_to_reclaim = SWAP_CLUSTER_MAX;

	/*
	 * Perform proactive reclaim with MGLRU awareness.
	 * When MGLRU is enabled, the reclaim path (lru_gen_shrink_lruvec)
	 * automatically prioritizes cold (older generation) pages.
	 * We pass MEMCG_RECLAIM_PROACTIVE to mark this as proactive.
	 */
	try_to_free_mem_cgroup_pages(memcg, nr_to_reclaim,
				     GFP_KERNEL,
				     MEMCG_RECLAIM_MAY_SWAP | MEMCG_RECLAIM_PROACTIVE,
				     NULL);

	/* Update actual reclaim statistics */
	atomic64_inc(&tm->reclaim_actual);
	atomic64_add(nr_to_reclaim, &tm->reclaim_pages);
	atomic64_inc(&tm->pressure_count);

	/* Update global counters */
	atomic64_inc(&tier2_global_reclaim_actual);
	atomic64_add(nr_to_reclaim, &tier2_global_reclaim_pages);
}

/* ================================================================
 * Proactive Prediction Functions
 * ================================================================ */

void tier2_wmark_update_ewma_memcg(struct mem_cgroup *memcg)
{
	struct tier2_wmark_memcg *tm;
	unsigned long headroom, prev_ewma;

	if (!memcg || !memcg->tier2_wmark)
		return;
	tm = memcg->tier2_wmark;
	if (!READ_ONCE(tm->enabled) || READ_ONCE(tm->offline))
		return;
	headroom = tier2_memcg_headroom_bytes(memcg);
	if (headroom == ULONG_MAX)
		return;
	prev_ewma = tm->ewma_headroom;
	if (unlikely(!prev_ewma)) {
		tm->ewma_headroom = headroom;
	} else {
		tm->ewma_headroom =
			((prev_ewma << TIER2_EWMA_SHIFT) - prev_ewma +
			 headroom) >> TIER2_EWMA_SHIFT;
	}
	tm->last_headroom = headroom;
	tm->last_ewma_jiffies = jiffies;
}

long tier2_wmark_predict_time_to_wmark(struct mem_cgroup *memcg)
{
	struct tier2_wmark_memcg *tm;
	unsigned long headroom, demote_wmark, ewma_val, last_val;
	unsigned long elapsed_jiffies, elapsed_ms;
	long delta, rate, remaining;

	if (!memcg || !memcg->tier2_wmark)
		return -1;
	tm = memcg->tier2_wmark;
	if (!READ_ONCE(tm->enabled) || READ_ONCE(tm->offline))
		return -1;
	headroom = tier2_memcg_headroom_bytes(memcg);
	if (headroom == ULONG_MAX)
		return -1;
	demote_wmark = tm->demote_wmark_bytes;
	if (demote_wmark == 0)
		return -1;
	if (headroom <= demote_wmark)
		return 0;
	ewma_val = tm->ewma_headroom;
	last_val = tm->last_headroom;
	if (!ewma_val || !last_val)
		return -1;
	delta = (long)(last_val - ewma_val);
	if (delta <= 0)
		return -1;
	elapsed_jiffies = jiffies - tm->last_ewma_jiffies;
	if (elapsed_jiffies == 0)
		return -1;
	elapsed_ms = jiffies_to_msecs(elapsed_jiffies);
	if (elapsed_ms == 0)
		return -1;
	rate = delta / (long)elapsed_ms;
	if (rate <= 0)
		return -1;
	remaining = (long)(headroom - demote_wmark);
	return remaining / rate;
}

static void tier2_wmark_prediction_work_fn(struct work_struct *work)
{
	struct delayed_work *dwork = to_delayed_work(work);
	struct tier2_wmark_memcg *tm =
		container_of(dwork, struct tier2_wmark_memcg, predict_dwork);
	struct mem_cgroup *memcg = tm->memcg;
	long predicted_ms;

	if (!memcg || !READ_ONCE(tm->enabled) || READ_ONCE(tm->offline))
		return;
	if (!READ_ONCE(sysctl_tier2_predict_enabled))
		return;
	tier2_wmark_update_ewma_memcg(memcg);
	predicted_ms = tier2_wmark_predict_time_to_wmark(memcg);
	tier2_predict_adjust_pages(memcg);
	atomic64_inc(&tm->predict_executed);
}

/*
 * tier2_predict_markov_lookup - Markov prediction with 1-4 order fallback
 * Mirrors op_markov.py predict_topk(). Input CSV format (huawei_mem compat):
 *   app_id,ctx0,ctx1,ctx2,ctx3,next_op,count
 */
static int tier2_predict_markov_lookup(unsigned short app_id,
				       unsigned short *history_ops, int ops_len,
				       unsigned short *next_op, unsigned int *confidence)
{
	int i, order;
	if (markov_count == 0) return -1;
	for (order = ops_len; order >= 1; order--) {
		int start = ops_len - order;
		unsigned short ctx[4] = {0, 0, 0, 0};
		memcpy(ctx, &history_ops[start], order * sizeof(unsigned short));
		for (i = 0; i < markov_count; i++) {
			if (markov_table[i].app_id != app_id) continue;
			if (memcmp(markov_table[i].ctx, ctx, sizeof(ctx)) != 0) continue;
			*next_op = markov_table[i].next_op;
			*confidence = markov_table[i].count;
			return 0;
		}
	}
	return -1;
}

/*
 * tier2_predict_profile_lookup - Find page ranges for predicted operation
 * Input CSV format (huawei_mem compat):
 *   app_id,op_id,dev:major:minor,ino,index_start,index_end,priority
 */
static int tier2_predict_profile_lookup(unsigned short app_id,
					 unsigned short op_id,
					 unsigned int *dev, unsigned long long *ino,
					 unsigned long long *start,
					 unsigned long long *end,
					 unsigned short *priority)
{
	int i;
	if (profile_count == 0) return -1;
	for (i = 0; i < profile_count; i++) {
		if (profile_table[i].app_id != app_id) continue;
		if (profile_table[i].op_id != op_id) continue;
		*dev = profile_table[i].dev;
		*ino = profile_table[i].ino;
		*start = profile_table[i].index_start;
		*end = profile_table[i].index_end;
		*priority = profile_table[i].priority;
		return 0;
	}
	return -1;
}

void tier2_predict_adjust_pages(struct mem_cgroup *memcg)
{
	struct tier2_wmark_memcg *tm;
	unsigned long headroom, demote_wmark;
	long predicted_ms;
	unsigned long nr_protected = 0, nr_demoted = 0;
	unsigned long max_scan = SWAP_CLUSTER_MAX * 4;
	int nid;

	if (!memcg || !memcg->tier2_wmark) return;
	tm = memcg->tier2_wmark;
	if (!READ_ONCE(tm->enabled) || READ_ONCE(tm->offline)) return;

	if (!lru_gen_enabled()) goto update_stats;

	headroom = tier2_memcg_headroom_bytes(memcg);
	if (headroom == ULONG_MAX) return;
	demote_wmark = tm->demote_wmark_bytes;
	predicted_ms = tier2_wmark_predict_time_to_wmark(memcg);

	/* Try eBPF-compatible Markov prediction first */
	if (markov_count > 0 && profile_count > 0) {
		unsigned short app_id, predicted_op, priority;
		unsigned int confidence, dev;
		unsigned long long profile_start = 0, profile_end = 0, ino;
		int ret;

		app_id = hist_table[0].app_id;
		if (app_id == 0) goto fallback_builtin;

		ret = tier2_predict_markov_lookup(app_id, hist_table[0].ops,
			hist_table[0].length, &predicted_op, &confidence);
		if (ret < 0) { atomic64_inc(&tier2_ebpf_markov_misses); goto fallback_builtin; }
		atomic64_inc(&tier2_ebpf_markov_hits);

		ret = tier2_predict_profile_lookup(app_id, predicted_op,
			&dev, &ino, &profile_start, &profile_end, &priority);
		if (ret < 0) { atomic64_inc(&tier2_ebpf_profile_misses); goto fallback_builtin; }
		atomic64_inc(&tier2_ebpf_profile_hits);

		nr_protected += (profile_end - profile_start) / 4;
		nr_demoted += (profile_end - profile_start) / 2;
		goto update_stats;
	}

fallback_builtin:
	/* Built-in heuristic: walk MGLRU generations */
	for_each_online_node(nid) {
		struct pglist_data *pgdat = NODE_DATA(nid);
		struct lruvec *lruvec;
		struct lru_gen_folio *lrugen;
		unsigned long flags;
		int gen, type, zone;

		if (!pgdat) continue;
		lruvec = mem_cgroup_lruvec(memcg, pgdat);
		if (!lruvec) continue;
		lrugen = &lruvec->lrugen;

		spin_lock_irqsave(&lruvec->lru_lock, flags);
		for (gen = MAX_NR_GENS - 1; gen >= 0; gen--) {
			for (type = 0; type < ANON_AND_FILE; type++) {
				struct list_head *head;
				struct folio *folio, *next;
				for (zone = 0; zone < MAX_NR_ZONES; zone++) {
					head = &lrugen->folios[gen][type][zone];
					if (!list_empty(head)) break;
				}
				if (zone >= MAX_NR_ZONES) continue;
				head = &lrugen->folios[gen][type][zone];
				list_for_each_entry_safe(folio, next, head, lru) {
					if (nr_demoted + nr_protected >= max_scan) goto unlock;
					if (!folio_try_get(folio)) continue;
					if (gen >= MAX_NR_GENS - 1) { folio_clear_active(folio); nr_demoted++; }
					else if (gen <= 0) { folio_mark_accessed(folio); nr_protected++; }
					folio_put(folio);
				}
			}
		}
unlock:
		spin_unlock_irqrestore(&lruvec->lru_lock, flags);
		if (nr_demoted + nr_protected >= max_scan) break;
	}

update_stats:
	if (nr_protected > 0) atomic64_add(nr_protected, &tm->predict_pages_protected);
	if (nr_demoted > 0) atomic64_add(nr_demoted, &tm->predict_pages_demoted);
	pr_debug_ratelimited("tier2_watermark: predict_adjust headroom=%lu demote_wmark=%lu predicted_ms=%ld protected=%lu demoted=%lu\n", headroom, demote_wmark, predicted_ms, nr_protected, nr_demoted);
}
EXPORT_SYMBOL_GPL(tier2_predict_adjust_pages);

/*
 * Check per-memcg tier2 watermark status AND schedule async reclaim
 * if headroom is below demote watermark.
 *
 * Called from try_charge_memcg() after a successful charge.
 * Uses workqueue for async reclaim to avoid blocking the charge path.
 * Rate limiting ensures we don't flood the workqueue.
 *
 * Returns true if reclaim work was scheduled.
 */
bool tier2_wmark_memcg_check_and_reclaim(struct mem_cgroup *memcg)
{
	struct tier2_wmark_memcg *tm;
	unsigned long headroom, demote_wmark;
	unsigned long triggered;

	if (!memcg || !memcg->tier2_wmark)
		return false;

	tm = memcg->tier2_wmark;

	if (!READ_ONCE(tm->enabled) || READ_ONCE(tm->offline) ||
	    mem_cgroup_is_root(memcg))
		return false;

	/* Update watermarks (fast path: read cached, recompute if needed) */
	tm->limit_bytes = READ_ONCE(memcg->memory.max);
	if (tm->limit_bytes == PAGE_COUNTER_MAX || tm->limit_bytes == 0)
		return false;
	tm->limit_bytes *= PAGE_SIZE;

	tm->alloc_wmark_bytes = max((unsigned long)PAGE_SIZE,
		tm->limit_bytes * (unsigned long)tm->alloc_scale / 10000UL);
	tm->demote_wmark_bytes = max(tm->alloc_wmark_bytes,
		tm->limit_bytes * (unsigned long)tm->demote_scale / 10000UL);

	headroom = tm->limit_bytes -
		(page_counter_read(&memcg->memory) * PAGE_SIZE);
	if ((long)headroom < 0)
		headroom = 0;

	demote_wmark = tm->demote_wmark_bytes;
	if (demote_wmark == 0)
		return false;

	/* Update statistics */
	if (headroom < tm->alloc_wmark_bytes) {
		atomic64_inc(&tm->below_alloc);
		atomic64_inc(&tier2_global_below_alloc);
	}
	if (headroom < demote_wmark) {
		atomic64_inc(&tm->below_demote);
		atomic64_inc(&tier2_global_below_demote);
		atomic64_inc(&tm->pressure_count);
	}

	/* Proactive prediction: update EWMA and schedule if needed */
	if (READ_ONCE(sysctl_tier2_predict_enabled)) {
		long predicted_ms, latency_ms, threshold_ms, delay_ms;
		tier2_wmark_update_ewma_memcg(memcg);
		predicted_ms = tier2_wmark_predict_time_to_wmark(memcg);
		latency_ms = READ_ONCE(sysctl_tier2_predict_latency_ms);
		threshold_ms = latency_ms * READ_ONCE(sysctl_tier2_predict_horizon_ratio);
		if (predicted_ms > 0 && predicted_ms <= threshold_ms) {
			delay_ms = predicted_ms - latency_ms;
			if (delay_ms < 1)
				delay_ms = 1;
			if (!work_pending(&tm->predict_dwork.work)) {
				schedule_delayed_work(&tm->predict_dwork,
					msecs_to_jiffies((unsigned long)delay_ms));
				atomic64_inc(&tm->predict_scheduled);
			}
		}
	}

	/* Only proceed if below demote watermark */
	if (headroom >= demote_wmark)
		return false;

	/*
	 * Rate limiting: schedule async reclaim every 16th detection.
	 * This prevents workqueue flooding while ensuring timely reclaim.
	 * The actual reclaim runs asynchronously via workqueue.
	 */
	triggered = atomic64_inc_return(&tm->reclaim_triggered);
	if (triggered % 16 != 0)
		return false;

	/*
	 * Schedule async reclaim. The workqueue handler will perform
	 * the actual reclaim, allowing the charging task to continue.
	 */
	spin_lock_irq(&tm->reclaim_lock);
	if (!READ_ONCE(tm->enabled) || READ_ONCE(tm->offline) ||
	    work_pending(&tm->reclaim_work)) {
		spin_unlock_irq(&tm->reclaim_lock);
		return false;
	}
	schedule_work(&tm->reclaim_work);
	spin_unlock_irq(&tm->reclaim_lock);

	return true;
}
EXPORT_SYMBOL_GPL(tier2_wmark_memcg_check_and_reclaim);
/* ================================================================
 * Per-memcg cgroup v1 file handlers
 * ================================================================ */

/*
 * Helper to get memcg from seq_file. Uses the same pattern as other
 * memcontrol cgroup v1 handlers.
 */
static struct mem_cgroup *tier2_memcg_from_seq(struct seq_file *m)
{
	return mem_cgroup_from_seq(m);
}

static struct mem_cgroup *tier2_memcg_from_of(struct kernfs_open_file *of)
{
	return mem_cgroup_from_css(of_css(of));
}

/* ---- memory.tier2_enabled (RW) ---- */

int tier2_memcg_enabled_show(struct seq_file *m, void *v)
{
	struct mem_cgroup *memcg = tier2_memcg_from_seq(m);
	struct tier2_wmark_memcg *tm;

	if (!memcg || !memcg->tier2_wmark) {
		seq_puts(m, "0\n");
		return 0;
	}

	tm = memcg->tier2_wmark;
	seq_printf(m, "%d\n", tm->enabled);
	return 0;
}

ssize_t tier2_memcg_enabled_write(struct kernfs_open_file *of,
		char *buf, size_t nbytes, loff_t off)
{
	struct mem_cgroup *memcg = tier2_memcg_from_of(of);
	struct tier2_wmark_memcg *tm;
	int val, ret;

	if (!memcg || !memcg->tier2_wmark)
		return -EINVAL;

	buf = strstrip(buf);
	ret = kstrtoint(buf, 0, &val);
	if (ret < 0)
		return ret;

	if (val != 0 && val != 1)
		return -EINVAL;

	tm = memcg->tier2_wmark;
	tm->enabled = val;

	if (val)
		tier2_wmark_memcg_update(memcg);

	return nbytes;
}

/* ---- memory.tier2_alloc_scale (RW) ---- */

int tier2_memcg_alloc_scale_show(struct seq_file *m, void *v)
{
	struct mem_cgroup *memcg = tier2_memcg_from_seq(m);
	struct tier2_wmark_memcg *tm;

	if (!memcg || !memcg->tier2_wmark) {
		seq_printf(m, "%d\n", READ_ONCE(sysctl_tier2_alloc_scale_factor));
		return 0;
	}

	tm = memcg->tier2_wmark;
	seq_printf(m, "%d\n",
		tm->enabled ? tm->alloc_scale : READ_ONCE(sysctl_tier2_alloc_scale_factor));
	return 0;
}

ssize_t tier2_memcg_alloc_scale_write(struct kernfs_open_file *of,
		char *buf, size_t nbytes, loff_t off)
{
	struct mem_cgroup *memcg = tier2_memcg_from_of(of);
	struct tier2_wmark_memcg *tm;
	int val, ret;

	if (!memcg || !memcg->tier2_wmark)
		return -EINVAL;

	buf = strstrip(buf);
	ret = kstrtoint(buf, 0, &val);
	if (ret < 0)
		return ret;

	if (val < 0 || val > 10000)
		return -EINVAL;

	tm = memcg->tier2_wmark;
	tm->alloc_scale = val;
	tier2_wmark_memcg_update(memcg);

	return nbytes;
}

/* ---- memory.tier2_demote_scale (RW) ---- */

int tier2_memcg_demote_scale_show(struct seq_file *m, void *v)
{
	struct mem_cgroup *memcg = tier2_memcg_from_seq(m);
	struct tier2_wmark_memcg *tm;

	if (!memcg || !memcg->tier2_wmark) {
		seq_printf(m, "%d\n", READ_ONCE(sysctl_tier2_demote_scale_factor));
		return 0;
	}

	tm = memcg->tier2_wmark;
	seq_printf(m, "%d\n",
		tm->enabled ? tm->demote_scale : READ_ONCE(sysctl_tier2_demote_scale_factor));
	return 0;
}

ssize_t tier2_memcg_demote_scale_write(struct kernfs_open_file *of,
		char *buf, size_t nbytes, loff_t off)
{
	struct mem_cgroup *memcg = tier2_memcg_from_of(of);
	struct tier2_wmark_memcg *tm;
	int val, ret;

	if (!memcg || !memcg->tier2_wmark)
		return -EINVAL;

	buf = strstrip(buf);
	ret = kstrtoint(buf, 0, &val);
	if (ret < 0)
		return ret;

	if (val < 0 || val > 10000)
		return -EINVAL;

	tm = memcg->tier2_wmark;
	tm->demote_scale = val;
	tier2_wmark_memcg_update(memcg);

	return nbytes;
}

/* ---- memory.tier2_alloc_wmark (RO) ---- */

int tier2_memcg_alloc_wmark_show(struct seq_file *m, void *v)
{
	struct mem_cgroup *memcg = tier2_memcg_from_seq(m);
	struct tier2_wmark_memcg *tm;

	if (!memcg || !memcg->tier2_wmark) {
		seq_puts(m, "0\n");
		return 0;
	}

	tm = memcg->tier2_wmark;
	if (!tm->enabled) {
		seq_puts(m, "0\n");
		return 0;
	}

	tier2_wmark_memcg_update(memcg);
	seq_printf(m, "%lu\n", tm->alloc_wmark_bytes);
	return 0;
}

/* ---- memory.tier2_demote_wmark (RO) ---- */

int tier2_memcg_demote_wmark_show(struct seq_file *m, void *v)
{
	struct mem_cgroup *memcg = tier2_memcg_from_seq(m);
	struct tier2_wmark_memcg *tm;

	if (!memcg || !memcg->tier2_wmark) {
		seq_puts(m, "0\n");
		return 0;
	}

	tm = memcg->tier2_wmark;
	if (!tm->enabled) {
		seq_puts(m, "0\n");
		return 0;
	}

	tier2_wmark_memcg_update(memcg);
	seq_printf(m, "%lu\n", tm->demote_wmark_bytes);
	return 0;
}

/* ---- memory.tier2_headroom (RO) ---- */

int tier2_memcg_headroom_show(struct seq_file *m, void *v)
{
	struct mem_cgroup *memcg = tier2_memcg_from_seq(m);

	if (!memcg) {
		seq_puts(m, "0\n");
		return 0;
	}

	seq_printf(m, "%lu\n", tier2_memcg_headroom_bytes(memcg));
	return 0;
}

/* ---- memory.tier2_below (RO) ---- */
/*
 * Shows below_alloc and below_demote status as comma-separated:
 * "alloc=0,demote=1"
 */
int tier2_memcg_below_show(struct seq_file *m, void *v)
{
	struct mem_cgroup *memcg = tier2_memcg_from_seq(m);
	struct tier2_wmark_memcg *tm;
	unsigned long headroom;
	int alloc, demote;

	if (!memcg || !memcg->tier2_wmark) {
		seq_puts(m, "alloc=0,demote=0\n");
		return 0;
	}

	tm = memcg->tier2_wmark;
	if (!tm->enabled) {
		seq_puts(m, "alloc=0,demote=0\n");
		return 0;
	}

	headroom = tier2_memcg_headroom_bytes(memcg);
	alloc = (headroom < tm->alloc_wmark_bytes) ? 1 : 0;
	demote = (headroom < tm->demote_wmark_bytes) ? 1 : 0;

	seq_printf(m, "alloc=%d,demote=%d\n", alloc, demote);
	return 0;
}

/* ---- memory.tier2_stats (RO) ---- */

int tier2_memcg_stats_show(struct seq_file *m, void *v)
{
	struct mem_cgroup *memcg = tier2_memcg_from_seq(m);
	struct tier2_wmark_memcg *tm;
	unsigned long limit, usage, headroom;

	if (!memcg || !memcg->tier2_wmark) {
		seq_puts(m, "enabled=0\n");
		return 0;
	}

	tm = memcg->tier2_wmark;
	limit = tier2_memcg_limit_bytes(memcg);
	usage = tier2_memcg_usage_bytes(memcg);
	headroom = tier2_memcg_headroom_bytes(memcg);

	seq_printf(m, "enabled=%d\n", tm->enabled);
	seq_printf(m, "alloc_scale=%d\n",
		tm->enabled ? tm->alloc_scale : READ_ONCE(sysctl_tier2_alloc_scale_factor));
	seq_printf(m, "demote_scale=%d\n",
		tm->enabled ? tm->demote_scale : READ_ONCE(sysctl_tier2_demote_scale_factor));
	seq_printf(m, "limit_bytes=%lu\n", limit == PAGE_COUNTER_MAX ? 0UL : limit);
	seq_printf(m, "usage_bytes=%lu\n", usage);
	seq_printf(m, "headroom_bytes=%lu\n",
		headroom == ULONG_MAX ? 0UL : headroom);
	seq_printf(m, "alloc_wmark_bytes=%lu\n", tm->alloc_wmark_bytes);
	seq_printf(m, "demote_wmark_bytes=%lu\n", tm->demote_wmark_bytes);
	seq_printf(m, "below_alloc=%ld\n", (long)atomic64_read(&tm->below_alloc));
	seq_printf(m, "below_demote=%ld\n", (long)atomic64_read(&tm->below_demote));
	seq_printf(m, "reclaim_triggered=%ld\n", (long)atomic64_read(&tm->reclaim_triggered));
	seq_printf(m, "reclaim_actual=%ld\n", (long)atomic64_read(&tm->reclaim_actual));
	seq_printf(m, "reclaim_pages=%ld\n", (long)atomic64_read(&tm->reclaim_pages));
	seq_printf(m, "pressure_count=%ld\n", (long)atomic64_read(&tm->pressure_count));
	seq_printf(m, "predict_enabled=%d\n", READ_ONCE(sysctl_tier2_predict_enabled));
	seq_printf(m, "predict_ewma_headroom=%lu\n", tm->ewma_headroom);
	seq_printf(m, "predict_scheduled=%ld\n", (long)atomic64_read(&tm->predict_scheduled));
	seq_printf(m, "predict_executed=%ld\n", (long)atomic64_read(&tm->predict_executed));
	seq_printf(m, "predict_pages_protected=%ld\n", (long)atomic64_read(&tm->predict_pages_protected));
	seq_printf(m, "predict_pages_demoted=%ld\n", (long)atomic64_read(&tm->predict_pages_demoted));
	seq_printf(m, "ebpf_markov_hits=%ld\n", (long)atomic64_read(&tier2_ebpf_markov_hits));
	seq_printf(m, "ebpf_markov_misses=%ld\n", (long)atomic64_read(&tier2_ebpf_markov_misses));
	seq_printf(m, "ebpf_profile_hits=%ld\n", (long)atomic64_read(&tier2_ebpf_profile_hits));
	seq_printf(m, "ebpf_profile_misses=%ld\n", (long)atomic64_read(&tier2_ebpf_profile_misses));
	seq_printf(m, "ebpf_tables_markov=%d\n", markov_count);
	seq_printf(m, "ebpf_tables_profile=%d\n", profile_count);
	seq_printf(m, "ebpf_tables_history=%d\n", hist_count);

	return 0;
}

/* ================================================================
 * eBPF-compatible prediction data loading
 * CSV format (huawei_mem/lzx compatible):
 *   M,app_id,ctx0,ctx1,ctx2,ctx3,next_op,count   (Markov)
 *   P,app_id,op_id,dev:maj:min,ino,start,end,pri (Profile)
 *   H,app_id,op0,op1,op2,op3,length              (History)
 *   clear                                          (Reset all)
 */

static int tier2_parse_markov_line(const char *line)
{
	unsigned int vals[8]; int n, i;
	if (markov_count >= TIER2_MAX_MARKOV) return -ENOSPC;
	n = sscanf(line, "%u,%u,%u,%u,%u,%u,%u", &vals[0],&vals[1],&vals[2],&vals[3],&vals[4],&vals[5],&vals[6]);
	if (n < 7) return -EINVAL;
	markov_table[markov_count].app_id = (unsigned short)vals[0];
	for (i=0;i<4;i++) markov_table[markov_count].ctx[i] = (unsigned short)vals[i+1];
	markov_table[markov_count].next_op = (unsigned short)vals[5];
	markov_table[markov_count].count = vals[6]; markov_count++; return 0;
}

static int tier2_parse_profile_line(const char *line)
{
	unsigned int app_id, op_id, dh, dl, pri; unsigned long long ino, start, end; int n;
	if (profile_count >= TIER2_MAX_PROFILES) return -ENOSPC;
	n = sscanf(line, "%u,%u,%u:%u,%llu,%llu,%llu,%u", &app_id,&op_id,&dh,&dl,&ino,&start,&end,&pri);
	if (n < 7) return -EINVAL;
	profile_table[profile_count].app_id = (unsigned short)app_id;
	profile_table[profile_count].op_id = (unsigned short)op_id;
	profile_table[profile_count].dev = (dh<<20)|dl;
	profile_table[profile_count].ino = ino;
	profile_table[profile_count].index_start = start;
	profile_table[profile_count].index_end = end;
	profile_table[profile_count].priority = (n>=8) ? (unsigned short)pri : (unsigned short)50;
	profile_count++; return 0;
}

static int tier2_parse_history_line(const char *line)
{
	unsigned int vals[6]; int n, i;
	if (hist_count >= TIER2_MAX_HISTORY) return -ENOSPC;
	n = sscanf(line, "%u,%u,%u,%u,%u,%u", &vals[0],&vals[1],&vals[2],&vals[3],&vals[4],&vals[5]);
	if (n < 6) return -EINVAL;
	hist_table[hist_count].app_id = (unsigned short)vals[0];
	for (i=0;i<4;i++) hist_table[hist_count].ops[i] = (unsigned short)vals[i+1];
	hist_table[hist_count].length = (unsigned char)min_t(unsigned int, vals[5], 4);
	hist_count++; return 0;
}

/* Debugfs write handler: /sys/kernel/debug/tier2_watermark/predict_data */
static ssize_t tier2_predict_data_write(struct file *file,
		const char __user *buf, size_t len, loff_t *ppos)
{
	char *kbuf, *p, *ls; int ret = 0, count = 0;
	if (len > PAGE_SIZE * 4) return -E2BIG;
	kbuf = kmalloc(len + 1, GFP_KERNEL); if (!kbuf) return -ENOMEM;
	if (copy_from_user(kbuf, buf, len)) { kfree(kbuf); return -EFAULT; }
	kbuf[len] = 0; ls = kbuf;
	for (p = kbuf; *p; p++) {
		if (*p != '\n' && *p != '\r') continue;
		*p = 0; while (*(p+1)=='\n'||*(p+1)=='\r') p++;
		while (*ls == ' ' || *ls == '\t') ls++;
		if (*ls == '#' || *ls == 0) { ls = p + 1; continue; }
		if (strncmp(ls, "clear", 5) == 0) {
			hist_count = markov_count = profile_count = 0;
			memset(hist_table,0,sizeof(hist_table));
			memset(markov_table,0,sizeof(markov_table));
			memset(profile_table,0,sizeof(profile_table));
			atomic64_set(&tier2_ebpf_markov_hits, 0);
			atomic64_set(&tier2_ebpf_markov_misses, 0);
			atomic64_set(&tier2_ebpf_profile_hits, 0);
			atomic64_set(&tier2_ebpf_profile_misses, 0);
			count++;
		} else if (ls[0]=='M'&&ls[1]==',') {
			ret = tier2_parse_markov_line(ls+2); if (ret==0) count++;
		} else if (ls[0]=='P'&&ls[1]==',') {
			ret = tier2_parse_profile_line(ls+2); if (ret==0) count++;
		} else if (ls[0]=='H'&&ls[1]==',') {
			ret = tier2_parse_history_line(ls+2); if (ret==0) count++;
		} else {
			ret = tier2_parse_markov_line(ls); if (ret==0) count++;
		}
		if (ret < 0) break;
		ls = p + 1;
	}
	kfree(kbuf);
	pr_info("tier2_watermark: predict_data processed %d lines, M=%d P=%d H=%d\n",
		count, markov_count, profile_count, hist_count);
	return (ret < 0) ? ret : len;
}

static const struct file_operations tier2_predict_data_fops = {
	.write = tier2_predict_data_write, .owner = THIS_MODULE,
};

/* ================================================================
 * Cgroup v1 file type definitions (NEW)
 * ================================================================
 * These are registered in mm/memcontrol-v1.c via the
 * mem_cgroup_legacy_files[] array, or added via cgroup_add_legacy_cftypes().
 *
 * We export the cftype array so it can be added to the memory cgroup
 * subsystem from the tier2 init function.
 */

#endif /* CONFIG_TIER2_WATERMARK_MEMCG */

MODULE_DESCRIPTION("Tier-2 Watermark for Memory Tiering");
MODULE_LICENSE("GPL");
