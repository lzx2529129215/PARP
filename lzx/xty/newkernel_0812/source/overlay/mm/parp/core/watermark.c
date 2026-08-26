// SPDX-License-Identifier: GPL-2.0
/* xty-test */
/*
 * Per-memcg secondary watermarks and EWMA arrival prediction.
 *
 * The two headroom watermarks follow the original Huawei prototype:
 *
 *   alloc  = max(PAGE_SIZE, limit * alloc_scale / 10000)
 *   demote = max(alloc, limit * demote_scale / 10000)
 *
 * Unlike that prototype, every memcg owns its EWMA, sampling timestamp,
 * delayed work and counters.  No workload can change a sibling's slope.
 */

#include <linux/cgroup.h>
#include <linux/jiffies.h>
#include <linux/kernel.h>
#include <linux/math64.h>
#include <linux/memcontrol.h>
#include <linux/mm.h>
#include <linux/page_counter.h>
#include <linux/parp_tier2.h>
#include <linux/swap.h>
#include <linux/sysctl.h>

#include "../internal.h"

#define PARP_TIER2_SCALE_BASE		10000U
#define PARP_TIER2_DEFAULT_ALLOC_SCALE	100U
#define PARP_TIER2_DEFAULT_DEMOTE_SCALE	300U
#define PARP_TIER2_MAX_RECLAIM_PAGES	4096UL

static int sysctl_tier2_predict_enabled __read_mostly;
static int sysctl_tier2_predict_latency_ms __read_mostly = 100;
static int sysctl_tier2_predict_horizon_ratio __read_mostly = 3;
static int parp_tier2_one = 1;
static int parp_tier2_thousand = 1000;
static int parp_tier2_hundred = 100;

static const struct ctl_table parp_tier2_sysctls[] = {
	{
		.procname = "tier2_predict_enabled",
		.data = &sysctl_tier2_predict_enabled,
		.maxlen = sizeof(sysctl_tier2_predict_enabled),
		.mode = 0644,
		.proc_handler = proc_dointvec_minmax,
		.extra1 = SYSCTL_ZERO,
		.extra2 = &parp_tier2_one,
	},
	{
		.procname = "tier2_predict_latency_ms",
		.data = &sysctl_tier2_predict_latency_ms,
		.maxlen = sizeof(sysctl_tier2_predict_latency_ms),
		.mode = 0644,
		.proc_handler = proc_dointvec_minmax,
		.extra1 = SYSCTL_ZERO,
		.extra2 = &parp_tier2_thousand,
	},
	{
		.procname = "tier2_predict_horizon_ratio",
		.data = &sysctl_tier2_predict_horizon_ratio,
		.maxlen = sizeof(sysctl_tier2_predict_horizon_ratio),
		.mode = 0644,
		.proc_handler = proc_dointvec_minmax,
		.extra1 = SYSCTL_ZERO,
		.extra2 = &parp_tier2_hundred,
	},
}; /* #lzx */

bool parp_tier2_runtime_enabled(void)
{
	return READ_ONCE(sysctl_tier2_predict_enabled);
}

u64 parp_tier2_scaled_wmark(u64 limit_bytes, u32 scale, u64 floor)
{
	u64 scaled;

	if (!limit_bytes || !scale)
		return 0;

	scaled = mul_u64_u32_div(limit_bytes, scale,
				 PARP_TIER2_SCALE_BASE);
	return max(scaled, floor);
}

u64 parp_tier2_ewma_next(u64 previous, u64 sample)
{
	u64 quotient;
	u64 remainder;

	/* Exact floor((previous * 15 + sample) / 16), without overflow. */
	quotient = (previous >> 4) * 15 + (sample >> 4);
	remainder = (previous & 15) * 15 + (sample & 15);
	return quotient + (remainder >> 4);
}

s64 parp_tier2_predict_ms(u64 previous_ewma, u64 ewma, u64 headroom,
			  u64 demote_wmark, u64 elapsed_ms)
{
	u64 delta, remaining, predicted;

	if (headroom <= demote_wmark)
		return 0;
	if (!elapsed_ms || ewma >= previous_ewma)
		return -1;

	delta = previous_ewma - ewma;
	remaining = headroom - demote_wmark;
	predicted = mul_u64_u64_div_u64(remaining, elapsed_ms, delta);
	return min_t(u64, predicted, S64_MAX);
}

static u64 parp_tier2_limit_bytes(struct mem_cgroup *memcg)
{
	u64 pages = READ_ONCE(memcg->memory.max);

	if (pages == PAGE_COUNTER_MAX || pages > (U64_MAX >> PAGE_SHIFT))
		return U64_MAX;
	return pages << PAGE_SHIFT;
}

static u64 parp_tier2_usage_bytes(struct mem_cgroup *memcg)
{
	u64 pages = page_counter_read(&memcg->memory);

	if (pages > (U64_MAX >> PAGE_SHIFT))
		return U64_MAX;
	return pages << PAGE_SHIFT;
}

static u64 parp_tier2_headroom(u64 limit, u64 usage)
{
	if (limit == U64_MAX)
		return U64_MAX;
	return usage >= limit ? 0 : limit - usage;
}

static void parp_tier2_watermarks(u64 limit, u32 alloc_scale,
				  u32 demote_scale, u64 *alloc, u64 *demote)
{
	if (!limit || limit == U64_MAX) {
		*alloc = 0;
		*demote = 0;
		return;
	}

	*alloc = parp_tier2_scaled_wmark(limit, alloc_scale, PAGE_SIZE);
	*demote = max(*alloc, parp_tier2_scaled_wmark(limit, demote_scale,
						      PAGE_SIZE));
}

static unsigned long parp_tier2_reclaim(struct mem_cgroup *memcg,
					unsigned long nr_pages)
{
	unsigned int options = MEMCG_RECLAIM_MAY_SWAP |
			       MEMCG_RECLAIM_PROACTIVE;

	return try_to_free_mem_cgroup_pages(memcg, nr_pages, GFP_KERNEL,
					     options, NULL);
}

static void parp_tier2_predict_work(struct work_struct *work)
{
	struct parp_tier2_memcg *state = container_of(to_delayed_work(work),
			struct parp_tier2_memcg, predict_work);
	struct mem_cgroup *memcg = state->memcg;
	unsigned long flags;
	unsigned long nr_to_reclaim;
	unsigned long reclaimed;
	u64 limit, usage, headroom, demote, gap;
	s64 predicted;
	bool active;

	spin_lock_irqsave(&state->lock, flags);
	state->scheduled_for_ns = 0;
	active = state->online && state->enabled &&
		 parp_tier2_runtime_enabled();
	demote = state->demote_wmark_bytes;
	predicted = state->predicted_ms;
	spin_unlock_irqrestore(&state->lock, flags);

	if (!active || mem_cgroup_is_root(memcg) || !mem_cgroup_online(memcg))
		return;

	atomic64_inc(&state->predict_executed);
	limit = parp_tier2_limit_bytes(memcg);
	usage = parp_tier2_usage_bytes(memcg);
	headroom = parp_tier2_headroom(limit, usage);
	if (!demote || headroom == U64_MAX)
		return;

	/* A recovered workload must not be reclaimed because of a stale timer. */
	if (headroom > demote &&
	    (predicted < 0 || headroom > READ_ONCE(state->ewma_headroom_bytes)))
		return;

	gap = headroom < demote ? demote - headroom : 0;
	nr_to_reclaim = gap ? DIV_ROUND_UP_ULL(gap, PAGE_SIZE) :
		SWAP_CLUSTER_MAX;
	nr_to_reclaim = clamp_t(unsigned long, nr_to_reclaim,
				SWAP_CLUSTER_MAX, PARP_TIER2_MAX_RECLAIM_PAGES);

	reclaimed = parp_tier2_reclaim(memcg, nr_to_reclaim);
	atomic64_inc(&state->reclaim_invocations);
	atomic64_add(reclaimed, &state->reclaim_pages);

	/* Re-seed/cancel the next prediction from the post-reclaim headroom. */
	parp_tier2_memcg_sample(memcg);
}

void parp_tier2_memcg_init(struct mem_cgroup *memcg)
{
	struct parp_tier2_memcg *state = &memcg->parp_tier2;

	spin_lock_init(&state->lock);
	INIT_DELAYED_WORK(&state->predict_work, parp_tier2_predict_work);
	state->memcg = memcg;
	state->online = true;
	state->enabled = false;
	state->alloc_scale = PARP_TIER2_DEFAULT_ALLOC_SCALE;
	state->demote_scale = PARP_TIER2_DEFAULT_DEMOTE_SCALE;
	state->predicted_ms = -1;
	atomic64_set(&state->samples, 0);
	atomic64_set(&state->below_alloc, 0);
	atomic64_set(&state->below_demote, 0);
	atomic64_set(&state->predict_scheduled, 0);
	atomic64_set(&state->predict_executed, 0);
	atomic64_set(&state->predict_cancelled, 0);
	atomic64_set(&state->reclaim_invocations, 0);
	atomic64_set(&state->reclaim_pages, 0);
}

void parp_tier2_memcg_online(struct mem_cgroup *memcg)
{
	struct parp_tier2_memcg *state = &memcg->parp_tier2;
	unsigned long flags;

	spin_lock_irqsave(&state->lock, flags);
	state->online = true;
	state->ewma_valid = false;
	state->predicted_ms = -1;
	spin_unlock_irqrestore(&state->lock, flags);
}

void parp_tier2_memcg_offline(struct mem_cgroup *memcg)
{
	struct parp_tier2_memcg *state = &memcg->parp_tier2;
	unsigned long flags;

	spin_lock_irqsave(&state->lock, flags);
	state->online = false;
	state->scheduled_for_ns = 0;
	spin_unlock_irqrestore(&state->lock, flags);
	cancel_delayed_work_sync(&state->predict_work);
}

void parp_tier2_memcg_reset(struct mem_cgroup *memcg)
{
	struct parp_tier2_memcg *state = &memcg->parp_tier2;
	unsigned long flags;

	spin_lock_irqsave(&state->lock, flags);
	state->enabled = false;
	state->ewma_valid = false;
	state->predicted_ms = -1;
	state->scheduled_for_ns = 0;
	spin_unlock_irqrestore(&state->lock, flags);
	cancel_delayed_work_sync(&state->predict_work);
}

void parp_tier2_memcg_destroy(struct mem_cgroup *memcg)
{
	cancel_delayed_work_sync(&memcg->parp_tier2.predict_work);
}

void parp_tier2_memcg_sample(struct mem_cgroup *memcg)
{
	struct parp_tier2_memcg *state;
	unsigned long flags;
	u64 limit, usage, headroom, alloc, demote;
	u64 now, elapsed_ms, previous, ewma;
	u64 threshold_ms, delay_ms = 0, due_ns = 0;
	s64 predicted = -1;
	u32 alloc_scale, demote_scale;
	bool enabled, online, schedule = false, cancel = false;

	if (!memcg || mem_cgroup_is_root(memcg) ||
	    !parp_tier2_runtime_enabled())
		return;

	state = &memcg->parp_tier2;
	enabled = READ_ONCE(state->enabled);
	online = READ_ONCE(state->online);
	if (!enabled || !online)
		return;

	limit = parp_tier2_limit_bytes(memcg);
	usage = parp_tier2_usage_bytes(memcg);
	headroom = parp_tier2_headroom(limit, usage);
	now = ktime_get_mono_fast_ns();

	spin_lock_irqsave(&state->lock, flags);
	if (!state->enabled || !state->online)
		goto unlock;

	alloc_scale = state->alloc_scale;
	demote_scale = state->demote_scale;
	parp_tier2_watermarks(limit, alloc_scale, demote_scale,
			      &alloc, &demote);
	if (state->limit_bytes && state->limit_bytes != limit) {
		state->ewma_valid = false;
		state->predicted_ms = -1;
		cancel = state->scheduled_for_ns != 0;
		state->scheduled_for_ns = 0;
	}
	state->limit_bytes = limit;
	state->alloc_wmark_bytes = alloc;
	state->demote_wmark_bytes = demote;
	state->headroom_bytes = headroom;

	if (!demote || headroom == U64_MAX) {
		state->ewma_valid = false;
		state->predicted_ms = -1;
		cancel = state->scheduled_for_ns != 0;
		state->scheduled_for_ns = 0;
		goto unlock;
	}

	if (!state->ewma_valid) {
		state->ewma_valid = true;
		state->previous_ewma_headroom_bytes = headroom;
		state->ewma_headroom_bytes = headroom;
		state->last_sample_ns = now;
		state->predicted_ms = headroom <= demote ? 0 : -1;
		atomic64_inc(&state->samples);
		goto decide;
	}

	previous = state->ewma_headroom_bytes;
	/* Charge hooks can run many times per millisecond; keep a real timebase. */
	if (headroom > demote &&
	    now - state->last_sample_ns < NSEC_PER_MSEC)
		goto unlock;

	ewma = parp_tier2_ewma_next(previous, headroom);
	elapsed_ms = div_u64(now - state->last_sample_ns, NSEC_PER_MSEC);
	predicted = parp_tier2_predict_ms(previous, ewma, headroom, demote, elapsed_ms);
	state->previous_ewma_headroom_bytes = previous;
	state->ewma_headroom_bytes = ewma;
	state->last_sample_ns = now;
	state->predicted_ms = predicted;
	atomic64_inc(&state->samples);

decide:
	if (headroom < alloc)
		atomic64_inc(&state->below_alloc);
	if (headroom < demote)
		atomic64_inc(&state->below_demote);

	predicted = state->predicted_ms;
	threshold_ms = (u64)READ_ONCE(sysctl_tier2_predict_latency_ms) *
		READ_ONCE(sysctl_tier2_predict_horizon_ratio);
	if (predicted == 0) {
		delay_ms = 1;
		schedule = true;
	} else if (predicted > 0 && predicted <= threshold_ms) {
		delay_ms = predicted > READ_ONCE(sysctl_tier2_predict_latency_ms) ?
			predicted - READ_ONCE(sysctl_tier2_predict_latency_ms) : 1;
		schedule = true;
	}

	if (schedule) {
		due_ns = now + delay_ms * NSEC_PER_MSEC;
		/* Never postpone work that is already due sooner. */
		if (!state->scheduled_for_ns || due_ns < state->scheduled_for_ns) {
			state->scheduled_for_ns = due_ns;
			atomic64_inc(&state->predict_scheduled);
		} else {
			schedule = false;
		}
	} else if (state->scheduled_for_ns) {
		state->scheduled_for_ns = 0;
		cancel = true;
		atomic64_inc(&state->predict_cancelled);
	}

unlock:
	spin_unlock_irqrestore(&state->lock, flags);
	if (schedule)
		mod_delayed_work(system_unbound_wq, &state->predict_work,
				 msecs_to_jiffies(delay_ms));
	else if (cancel)
		cancel_delayed_work(&state->predict_work);
}

void parp_tier2_memcg_charge(struct mem_cgroup *memcg)
{
	struct mem_cgroup *iter;

	if (!parp_tier2_runtime_enabled())
		return;

	/* Hierarchical charging changes every non-root ancestor's headroom. */
	for (iter = memcg; iter && !mem_cgroup_is_root(iter);
	     iter = parent_mem_cgroup(iter))
		parp_tier2_memcg_sample(iter);
}

static struct mem_cgroup *parp_tier2_memcg_from_seq(struct seq_file *m)
{
	return mem_cgroup_from_seq(m);
}

static struct mem_cgroup *parp_tier2_memcg_from_of(struct kernfs_open_file *of)
{
	return mem_cgroup_from_css(of_css(of));
}

int parp_tier2_enabled_show(struct seq_file *m, void *v)
{
	struct mem_cgroup *memcg = parp_tier2_memcg_from_seq(m);

	seq_printf(m, "%u\n", READ_ONCE(memcg->parp_tier2.enabled));
	return 0;
}

ssize_t parp_tier2_enabled_write(struct kernfs_open_file *of, char *buf,
				 size_t nbytes, loff_t off)
{
	struct mem_cgroup *memcg = parp_tier2_memcg_from_of(of);
	struct parp_tier2_memcg *state = &memcg->parp_tier2;
	unsigned long flags;
	bool enabled;
	int ret;

	ret = kstrtobool(strstrip(buf), &enabled);
	if (ret)
		return ret;

	spin_lock_irqsave(&state->lock, flags);
	state->enabled = enabled;
	state->ewma_valid = false;
	state->predicted_ms = -1;
	state->scheduled_for_ns = 0;
	spin_unlock_irqrestore(&state->lock, flags);
	if (!enabled)
		cancel_delayed_work_sync(&state->predict_work);
	else
		parp_tier2_memcg_sample(memcg);
	return nbytes;
}

static int parp_tier2_scale_show(struct seq_file *m, bool demote)
{
	struct mem_cgroup *memcg = parp_tier2_memcg_from_seq(m);
	struct parp_tier2_memcg *state = &memcg->parp_tier2;

	seq_printf(m, "%u\n", demote ? READ_ONCE(state->demote_scale) :
				       READ_ONCE(state->alloc_scale));
	return 0;
}

int parp_tier2_alloc_scale_show(struct seq_file *m, void *v)
{
	return parp_tier2_scale_show(m, false);
}

int parp_tier2_demote_scale_show(struct seq_file *m, void *v)
{
	return parp_tier2_scale_show(m, true);
}

static ssize_t parp_tier2_scale_write(struct kernfs_open_file *of, char *buf,
				      size_t nbytes, bool demote)
{
	struct mem_cgroup *memcg = parp_tier2_memcg_from_of(of);
	struct parp_tier2_memcg *state = &memcg->parp_tier2;
	unsigned long flags;
	u32 scale;
	int ret;

	ret = kstrtou32(strstrip(buf), 0, &scale);
	if (ret)
		return ret;
	if (scale > PARP_TIER2_SCALE_BASE)
		return -ERANGE;

	spin_lock_irqsave(&state->lock, flags);
	if (demote)
		state->demote_scale = scale;
	else
		state->alloc_scale = scale;
	state->ewma_valid = false;
	spin_unlock_irqrestore(&state->lock, flags);
	parp_tier2_memcg_sample(memcg);
	return nbytes;
}

ssize_t parp_tier2_alloc_scale_write(struct kernfs_open_file *of, char *buf,
				     size_t nbytes, loff_t off)
{
	return parp_tier2_scale_write(of, buf, nbytes, false);
}

ssize_t parp_tier2_demote_scale_write(struct kernfs_open_file *of, char *buf,
				      size_t nbytes, loff_t off)
{
	return parp_tier2_scale_write(of, buf, nbytes, true);
}

static void parp_tier2_live_values(struct mem_cgroup *memcg, u64 *limit,
				   u64 *usage, u64 *headroom,
				   u64 *alloc, u64 *demote)
{
	struct parp_tier2_memcg *state = &memcg->parp_tier2;
	u32 alloc_scale = READ_ONCE(state->alloc_scale);
	u32 demote_scale = READ_ONCE(state->demote_scale);

	*limit = parp_tier2_limit_bytes(memcg);
	*usage = parp_tier2_usage_bytes(memcg);
	*headroom = parp_tier2_headroom(*limit, *usage);
	parp_tier2_watermarks(*limit, alloc_scale, demote_scale, alloc, demote);
}

static int parp_tier2_value_show(struct seq_file *m, unsigned int which)
{
	struct mem_cgroup *memcg = parp_tier2_memcg_from_seq(m);
	u64 limit, usage, headroom, alloc, demote, value;

	parp_tier2_live_values(memcg, &limit, &usage, &headroom, &alloc, &demote);
	value = which == 0 ? alloc : which == 1 ? demote : headroom;
	seq_printf(m, "%llu\n", value == U64_MAX ? 0 : value);
	return 0;
}

int parp_tier2_alloc_wmark_show(struct seq_file *m, void *v)
{
	return parp_tier2_value_show(m, 0);
}

int parp_tier2_demote_wmark_show(struct seq_file *m, void *v)
{
	return parp_tier2_value_show(m, 1);
}

int parp_tier2_headroom_show(struct seq_file *m, void *v)
{
	return parp_tier2_value_show(m, 2);
}

int parp_tier2_below_show(struct seq_file *m, void *v)
{
	struct mem_cgroup *memcg = parp_tier2_memcg_from_seq(m);
	u64 limit, usage, headroom, alloc, demote;

	parp_tier2_live_values(memcg, &limit, &usage, &headroom, &alloc, &demote);
	seq_printf(m, "alloc=%u demote=%u\n",
		   alloc && headroom < alloc, demote && headroom < demote);
	return 0;
}

int parp_tier2_stats_show(struct seq_file *m, void *v)
{
	struct mem_cgroup *memcg = parp_tier2_memcg_from_seq(m);
	struct parp_tier2_memcg *state = &memcg->parp_tier2;
	u64 limit, usage, headroom, alloc, demote;

	parp_tier2_live_values(memcg, &limit, &usage, &headroom, &alloc, &demote);
	seq_printf(m, "enabled %u\n", READ_ONCE(state->enabled));
	seq_printf(m, "domain_id %llu\n",
		   (u64)cgroup_ino(memcg->css.cgroup));
	seq_printf(m, "limit_bytes %llu\n", limit == U64_MAX ? 0 : limit);
	seq_printf(m, "usage_bytes %llu\n", usage);
	seq_printf(m, "headroom_bytes %llu\n", headroom == U64_MAX ? 0 : headroom);
	seq_printf(m, "alloc_wmark_bytes %llu\n", alloc);
	seq_printf(m, "demote_wmark_bytes %llu\n", demote);
	seq_printf(m, "ewma_headroom_bytes %llu\n",
		   READ_ONCE(state->ewma_headroom_bytes));
	seq_printf(m, "predicted_ms %lld\n", READ_ONCE(state->predicted_ms));
	seq_printf(m, "samples %lld\n", atomic64_read(&state->samples));
	seq_printf(m, "below_alloc %lld\n", atomic64_read(&state->below_alloc));
	seq_printf(m, "below_demote %lld\n", atomic64_read(&state->below_demote));
	seq_printf(m, "predict_scheduled %lld\n",
		   atomic64_read(&state->predict_scheduled));
	seq_printf(m, "predict_executed %lld\n",
		   atomic64_read(&state->predict_executed));
	seq_printf(m, "predict_cancelled %lld\n",
		   atomic64_read(&state->predict_cancelled));
	seq_printf(m, "reclaim_invocations %lld\n",
		   atomic64_read(&state->reclaim_invocations));
	seq_printf(m, "reclaim_pages %lld\n", atomic64_read(&state->reclaim_pages));
	return 0;
}

static int __init parp_tier2_init(void)
{
	register_sysctl_init("vm", parp_tier2_sysctls);
	pr_info("PARP tier2: per-memcg watermarks and EWMA predictor ready (disabled)\n");
	return 0;
}
subsys_initcall(parp_tier2_init);
