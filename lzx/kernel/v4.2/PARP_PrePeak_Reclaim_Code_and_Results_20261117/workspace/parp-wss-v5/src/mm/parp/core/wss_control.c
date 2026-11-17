// SPDX-License-Identifier: GPL-2.0
/* Four five-second windows; one sleepable, serialized controller per memcg. */
#include <linux/cgroup.h>
#include <linux/export.h>
#include <linux/jiffies.h>
#include <linux/kernel.h>
#include <linux/ktime.h>
#include <linux/memcontrol.h>
#include <linux/mm.h>
#include <linux/overflow.h>
#include <linux/page_counter.h>
#include <linux/parp_tier2.h>
#include <linux/seq_file.h>
#include <linux/swap.h>
#include <linux/sysctl.h>
#ifndef PARP_WSS_HOST_TEST
#include "../internal.h"
#endif

static int tier2_enabled;
static int tier2_one = 1;

u64 parp_tier2_limit_bytes(struct mem_cgroup *memcg)
{
	u64 pages = READ_ONCE(memcg->memory.max);

	if (pages == PAGE_COUNTER_MAX || pages > (U64_MAX >> PAGE_SHIFT))
		return U64_MAX;
	return (u64)pages << PAGE_SHIFT;
}

u64 parp_tier2_usage_bytes(struct mem_cgroup *memcg)
{
	return (u64)page_counter_read(&memcg->memory) << PAGE_SHIFT;
}

bool parp_tier2_runtime_enabled(void)
{
	return READ_ONCE(tier2_enabled);
}

static bool tier2_leaf(struct mem_cgroup *memcg)
{
	bool leaf;

	rcu_read_lock();
	leaf = !css_has_online_children(&memcg->css);
	rcu_read_unlock();
	return leaf && !mem_cgroup_is_root(memcg) && cgroup_subsys_on_dfl(memory_cgrp_subsys);
}

/* All functions below require control_mutex, unless stated otherwise. */
static void tier2_restore_high(struct parp_tier2_memcg *s)
{
	if (!s->high_owned)
		return;
	if (s->saved_admin_seq == s->admin_write_seq &&
	    READ_ONCE(s->memcg->memory.high) == s->applied_high) {
		mem_cgroup_update_high(s->memcg, s->saved_high);
		s->high_restores++;
	}
	s->high_owned = false;
}

static void tier2_cancel(struct parp_tier2_memcg *s, enum parp_wss_reason reason)
{
	tier2_restore_high(s);
	s->cycle.active = false;
	s->decision.mode = PARP_WSS_OBSERVE;
	s->decision.reason = reason;
}

static bool tier2_set_high(struct parp_tier2_memcg *s, u64 target)
{
	u64 pages = target >> PAGE_SHIFT;
	unsigned long admin_high = s->high_owned ? s->saved_high :
		READ_ONCE(s->memcg->memory.high);

	if (!pages || pages > PAGE_COUNTER_MAX ||
	    pages > READ_ONCE(s->memcg->memory.max) || pages > admin_high ||
	    pages < READ_ONCE(s->memcg->memory.min) ||
	    pages < READ_ONCE(s->memcg->memory.low))
		return false;
	if (!s->high_owned) {
		s->saved_high = admin_high;
		s->saved_admin_seq = s->admin_write_seq;
		s->high_owned = true;
	}
	if (READ_ONCE(s->memcg->memory.high) != pages) {
		mem_cgroup_update_high(s->memcg, (unsigned long)pages);
		s->high_updates++;
	}
	s->applied_high = (unsigned long)pages;
	return true;
}

/* Native memory.high writes and the controller share this same mutex. The
 * native writer retains its ordinary blocking/O_NONBLOCK reclaim behavior.
 */
void parp_tier2_high_user_begin(struct mem_cgroup *memcg)
{
	struct parp_tier2_memcg *s = &memcg->parp_tier2;

	mutex_lock(&s->control_mutex);
	s->admin_write_seq++;
	s->high_owned = false; /* Never restore over the incoming admin value. */
	s->enabled = false;
	s->cycle.active = false;
	s->decision.mode = PARP_WSS_OBSERVE;
	s->decision.reason = PARP_WSS_ADMIN_OVERRIDE;
	cancel_delayed_work(&s->predict_work);
}

void parp_tier2_high_user_end(struct mem_cgroup *memcg)
{
	mutex_unlock(&memcg->parp_tier2.control_mutex);
}

static u64 tier2_refaults(struct mem_cgroup *memcg)
{
	mem_cgroup_flush_stats(memcg);
	return (u64)memcg_page_state(memcg, WORKINGSET_REFAULT_ANON) +
	       memcg_page_state(memcg, WORKINGSET_REFAULT_FILE);
}

static bool tier2_refault_stop(struct parp_tier2_memcg *s, u64 now)
{
	u64 value, delta;
	bool stop;

	if (!s->refault_window_ns) {
		s->refault_window_ns = now;
		s->refault_baseline = tier2_refaults(s->memcg);
		return false;
	}
	if (now - s->refault_window_ns < NSEC_PER_SEC)
		return false;
	value = tier2_refaults(s->memcg);
	delta = value >= s->refault_baseline ? value - s->refault_baseline : 0;
	/* Conservative feedback, not attribution to any particular old folio. */
	stop = s->window_reclaimed_pages && delta > s->window_reclaimed_pages / 10;
	/* Native charge-triggered high reclaim runs outside this worker. */
	if (!s->window_reclaimed_pages && s->high_owned &&
	    delta > max_t(u64, page_counter_read(&s->memcg->memory) / 10, 1))
		stop = true;
	s->refault_baseline = value;
	s->refault_window_ns = now;
	s->window_reclaimed_pages = 0;
	return stop;
}

static bool tier2_profile_allows(struct parp_tier2_memcg *s, u32 action)
{
	return s->profile_present && parp_wss_profile_matches(&s->forecast, &s->profile) &&
	       (parp_wss_profile_mask(&s->profile) & action) &&
	       (s->profile.live_verified_mask & action);
}

static unsigned long tier2_reclaim(struct mem_cgroup *memcg, unsigned long pages,
				 bool proactive)
{
	unsigned int options = MEMCG_RECLAIM_MAY_SWAP;

	if (proactive)
		options |= MEMCG_RECLAIM_PROACTIVE;
	return try_to_free_mem_cgroup_pages(memcg, pages, GFP_KERNEL, options, NULL);
}

static void parp_tier2_predict_work(struct work_struct *work)
{
	struct parp_tier2_memcg *s = container_of(to_delayed_work(work),
					struct parp_tier2_memcg, predict_work);
	struct parp_wss_decision d;
	u64 now, usage, request = 0, limit, maximum = 0;
	unsigned long pages, actual;
	unsigned int i;
	bool proactive = false;

	mutex_lock(&s->control_mutex);
	now = ktime_get_ns();
	if (!s->online || !s->enabled || !parp_tier2_runtime_enabled()) {
		tier2_cancel(s, PARP_WSS_DISABLED);
		goto out;
	}
	if (!tier2_leaf(s->memcg)) {
		tier2_cancel(s, PARP_WSS_NON_LEAF);
		goto out;
	}
	if (!s->forecast_present || !parp_wss_forecast_valid(&s->forecast, now)) {
		tier2_cancel(s, s->forecast_present ? PARP_WSS_EXPIRED : PARP_WSS_NO_FORECAST);
		goto out;
	}
	parp_wss_classify(&s->forecast, s->profile_present ? &s->profile : NULL,
			  now, PAGE_SIZE, &d);
	if (s->high_owned && d.mode == PARP_WSS_HIGH && d.reason == PARP_WSS_UNKNOWN &&
	    tier2_profile_allows(s, PARP_WSS_REUSE)) {
		d.reason = PARP_WSS_OK;
		d.may_apply = true;
	}
	s->decision = d;
	if (parp_get_mode() != PARP_MODE_APPLY) {
		tier2_restore_high(s);
		s->cycle.active = false;
		s->decision.reason = PARP_WSS_OBSERVE_ONLY;
		goto requeue;
	}
	if (now < s->cooldown_until_ns || s->blocked_generation == s->generation) {
		tier2_cancel(s, s->stop_reason);
		goto requeue;
	}
	if (tier2_refault_stop(s, now)) {
		s->refault_stops++;
		s->blocked_generation = s->generation;
		s->stop_reason = PARP_WSS_REFAULT;
		s->cooldown_until_ns = now + NSEC_PER_SEC;
		tier2_cancel(s, PARP_WSS_REFAULT);
		goto requeue;
	}
	usage = parp_tier2_usage_bytes(s->memcg);
	limit = parp_tier2_limit_bytes(s->memcg);
	for (i = 0; i < PARP_WSS_STEPS; i++)
		maximum = max(maximum, s->forecast.result.step[i].wss_bytes);
	/* Growth releases a stale low high before any new reclaim decision. */
	if (s->high_owned && (d.mode != PARP_WSS_HIGH ||
			       d.target_bytes > (u64)s->applied_high << PAGE_SHIFT))
		tier2_restore_high(s);
	if (maximum > limit) {
		tier2_cancel(s, PARP_WSS_LIMIT_CONFLICT);
		goto requeue;
	}
	/* Finish an already-budgeted turnover before adopting a new stable high.
	 * A new generation never refills its budget or extends its switch time.
	 */
	if (s->cycle.active) {
		if (now - s->cycle.start_ns >= PARP_WSS_HORIZON_NS) {
			tier2_cancel(s, PARP_WSS_EXPIRED);
			goto requeue;
		}
		if (s->forecast.confidence_q15 < 16384 ||
		    !tier2_profile_allows(s, PARP_WSS_TURNOVER) ||
		    maximum < s->cycle.plateau_bytes - s->cycle.plateau_bytes / 10) {
			tier2_cancel(s, PARP_WSS_UNKNOWN);
			goto requeue;
		}
		tier2_restore_high(s);
		request = parp_wss_cycle_request(&s->cycle, &s->forecast, now,
						 usage, PARP_WSS_BATCH_BYTES);
		s->decision.mode = s->cycle.second_stage ? PARP_WSS_RECLAIM_REST :
						       PARP_WSS_RECLAIM_LOW;
		s->decision.reason = request ? PARP_WSS_OK : PARP_WSS_WAIT_REBOUND;
		proactive = true;
		if (!request && s->cycle.second_stage &&
		    s->forecast.rss_bytes >= s->cycle.plateau_bytes &&
		    usage <= s->cycle.plateau_bytes) {
			s->cycle.active = false;
			s->decision.reason = PARP_WSS_COMPLETE;
		}
	} else if (d.may_apply && d.mode == PARP_WSS_RECLAIM_LOW && s->cycle.armed) {
		if (now - s->forecast.timestamp_ns >= PARP_WSS_WINDOW_NS) {
			s->decision.reason = PARP_WSS_EXPIRED;
			goto requeue;
		}
		tier2_restore_high(s);
		parp_wss_cycle_begin(&s->cycle, &s->forecast, &d, usage);
		request = parp_wss_cycle_request(&s->cycle, &s->forecast, now,
						 usage, PARP_WSS_BATCH_BYTES);
		proactive = true;
	} else if (d.may_apply && d.mode == PARP_WSS_HIGH) {
		if (!tier2_set_high(s, d.target_bytes)) {
			tier2_cancel(s, PARP_WSS_LIMIT_CONFLICT);
			goto requeue;
		}
		request = usage > d.target_bytes ?
			min_t(u64, usage - d.target_bytes, PARP_WSS_BATCH_BYTES) : 0;
	} else {
		tier2_restore_high(s);
	}
	/* Floor rounding keeps the requested count within the byte budget. */
	pages = (unsigned long)(request >> PAGE_SHIFT);
	if (!pages)
		goto requeue;
	/* control_mutex prevents forecast/admin races; an in-flight kernel
	 * reclaim cannot be cancelled and has no hard wall-clock time bound.
	 */
	s->requested_pages += pages;
	s->reclaim_invocations++;
	actual = tier2_reclaim(s->memcg, pages, proactive);
	s->reclaimed_pages += actual;
	s->window_reclaimed_pages += actual;
	if (proactive)
		parp_wss_cycle_account(&s->cycle, (u64)actual << PAGE_SHIFT);
	if (actual)
		s->no_progress = 0;
	else if (++s->no_progress >= 3) {
		s->no_progress_stops++;
		s->blocked_generation = s->generation;
		s->stop_reason = PARP_WSS_NO_PROGRESS;
		s->cooldown_until_ns = ktime_get_ns() + NSEC_PER_SEC;
		tier2_cancel(s, PARP_WSS_NO_PROGRESS);
	}
requeue:
	/* Also expires held high when the producer disappears or the group is idle. */
	if (s->online && s->enabled)
		mod_delayed_work(system_unbound_wq, &s->predict_work,
				 msecs_to_jiffies(PARP_WSS_PERIOD_MS));
out:
	mutex_unlock(&s->control_mutex);
}

void parp_tier2_memcg_init(struct mem_cgroup *memcg)
{
	struct parp_tier2_memcg *s = &memcg->parp_tier2;

	mutex_init(&s->control_mutex);
	INIT_DELAYED_WORK(&s->predict_work, parp_tier2_predict_work);
	s->memcg = memcg;
	s->cycle.armed = true;
	s->decision.reason = PARP_WSS_NO_FORECAST;
}

void parp_tier2_memcg_online(struct mem_cgroup *memcg)
{
	memcg->parp_tier2.online = true;
}

static void tier2_stop(struct mem_cgroup *memcg, bool offline)
{
	struct parp_tier2_memcg *s = &memcg->parp_tier2;

	mutex_lock(&s->control_mutex);
	s->enabled = false;
	if (offline)
		s->online = false;
	tier2_cancel(s, PARP_WSS_DISABLED);
	s->forecast_present = false;
	s->profile_present = false;
	s->model_prediction_present = false;
	s->generation = 0;
	s->cycle.armed = true;
	mutex_unlock(&s->control_mutex);
	cancel_delayed_work_sync(&s->predict_work);
}

void parp_tier2_memcg_offline(struct mem_cgroup *memcg) { tier2_stop(memcg, true); }
void parp_tier2_memcg_reset(struct mem_cgroup *memcg) { tier2_stop(memcg, false); }
void parp_tier2_memcg_destroy(struct mem_cgroup *memcg)
{
	cancel_delayed_work_sync(&memcg->parp_tier2.predict_work);
}

#ifdef CONFIG_PARP_TIER2_WSS_PREDICT
int parp_tier2_submit_forecast(struct mem_cgroup *memcg,
			     const struct parp_wss_forecast *forecast)
{
	struct parp_tier2_memcg *s;
	u64 now = ktime_get_ns();
	int ret = 0;

	if (!memcg || !forecast || !tier2_leaf(memcg))
		return -EINVAL;
	if (!parp_wss_forecast_valid(forecast, now) ||
	    now - forecast->timestamp_ns >= PARP_WSS_WINDOW_NS)
		return -ESTALE;
	s = &memcg->parp_tier2;
	mutex_lock(&s->control_mutex);
	if (!s->online)
		ret = -ENODEV;
	else if (forecast->generation <= s->generation ||
		 (s->forecast_present && forecast->timestamp_ns < s->forecast.timestamp_ns))
		ret = -ESTALE;
	else {
		if (s->blocked_generation && now >= s->cooldown_until_ns)
			s->no_progress = 0;
		s->forecast = *forecast;
		s->forecast_present = true;
		s->generation = forecast->generation;
		s->forecasts_received++;
		if (!s->cycle.active && !parp_wss_turnover_shape(forecast))
			s->cycle.armed = true;
		if (s->enabled)
			mod_delayed_work(system_unbound_wq, &s->predict_work, 0);
	}
	mutex_unlock(&s->control_mutex);
	return ret;
}
EXPORT_SYMBOL_GPL(parp_tier2_submit_forecast);

int parp_tier2_set_profile(struct mem_cgroup *memcg,
			 const struct parp_wss_profile *profile)
{
	struct parp_tier2_memcg *s;

	if (!memcg || !profile || !tier2_leaf(memcg) || !profile->id ||
	    !profile->model_version || !profile->workload_id ||
	    (profile->live_verified_mask & ~3U))
		return -EINVAL;
	s = &memcg->parp_tier2;
	mutex_lock(&s->control_mutex);
	if (!s->online) {
		mutex_unlock(&s->control_mutex);
		return -ENODEV;
	}
	tier2_cancel(s, PARP_WSS_NO_FORECAST);
	s->profile = *profile;
	s->profile_present = true;
	/* A changed profile requires a new forecast and cannot refill a cycle. */
	s->forecast_present = false;
	mutex_unlock(&s->control_mutex);
	return 0;
}
EXPORT_SYMBOL_GPL(parp_tier2_set_profile);

/* The original scalar producer remains observational: it cannot assert four
 * future time windows or silently acquire permission for stronger reclaim.
 */
int parp_tier2_submit_prediction(struct mem_cgroup *memcg,
		const struct parp_tier2_model_prediction *prediction)
{
	struct parp_tier2_memcg *s;
	const struct parp_tier2_predictor_context *c;
	u64 now = ktime_get_ns(), expiry;

	if (!memcg || !prediction || mem_cgroup_is_root(memcg))
		return -EINVAL;
	c = &prediction->context;
	if (!prediction->result.wss || c->predicted_resident_bytes > prediction->result.wss ||
	    !c->generation || !c->estimator_version || !c->confidence_q15 ||
	    c->confidence_q15 > 32767)
		return -EINVAL;
	if (!c->timestamp_ns || c->timestamp_ns > now || !c->ttl_ns ||
	    c->ttl_ns > 10 * NSEC_PER_SEC ||
	    check_add_overflow(c->timestamp_ns, c->ttl_ns, &expiry) || now >= expiry)
		return -ESTALE;
	s = &memcg->parp_tier2;
	mutex_lock(&s->control_mutex);
	if (!s->online) {
		mutex_unlock(&s->control_mutex);
		return -ENODEV;
	}
	if (s->model_prediction_present && c->generation <=
	    s->model_prediction.context.generation) {
		mutex_unlock(&s->control_mutex);
		return -ESTALE;
	}
	s->model_prediction = *prediction;
	s->model_prediction_present = true;
	mutex_unlock(&s->control_mutex);
	return 0;
}
EXPORT_SYMBOL_GPL(parp_tier2_submit_prediction);

int parp_tier2_call_predictor(struct mem_cgroup *memcg,
		const struct parp_tier2_predictor_input *input,
		const struct parp_tier2_predictor_context *context,
		parp_tier2_predictor_fn predict)
{
	struct parp_tier2_model_prediction prediction = { };
	int ret;

	if (!memcg || !input || !context)
		return -EINVAL;
	if (!predict)
		return -EOPNOTSUPP;
	might_sleep();
	prediction.input = *input;
	prediction.context = *context;
	ret = predict(input->list_num, input->op_id, input->dram_num,
		      &prediction.result.wss, &prediction.result.pgfault);
	return ret ? ret : parp_tier2_submit_prediction(memcg, &prediction);
}
EXPORT_SYMBOL_GPL(parp_tier2_call_predictor);

bool parp_tier2_model_prediction_lookup(struct mem_cgroup *memcg, u64 now,
				       struct parp_workingset_prediction *p)
{
	struct parp_tier2_memcg *s = &memcg->parp_tier2;
	struct parp_tier2_model_prediction m;
	bool present;

	memset(p, 0, sizeof(*p));
	mutex_lock(&s->control_mutex);
	present = s->model_prediction_present;
	m = s->model_prediction;
	mutex_unlock(&s->control_mutex);
	if (!present || now < m.context.timestamp_ns ||
	    now - m.context.timestamp_ns >= m.context.ttl_ns)
		return false;
	p->predicted_workingset_bytes = m.result.wss;
	p->predicted_resident_bytes = m.context.predicted_resident_bytes;
	p->predicted_pgfault = m.result.pgfault;
	p->pgfault_valid = true;
	p->timestamp_ns = m.context.timestamp_ns;
	p->expiry_ns = m.context.timestamp_ns + m.context.ttl_ns;
	p->generation = m.context.generation;
	p->confidence_q15 = m.context.confidence_q15;
	p->estimator_version = m.context.estimator_version;
	return true;
}
#endif

int parp_tier2_enabled_show(struct seq_file *m, void *v)
{
	seq_printf(m, "%u\n", READ_ONCE(mem_cgroup_from_seq(m)->parp_tier2.enabled));
	return 0;
}

ssize_t parp_tier2_enabled_write(struct kernfs_open_file *of, char *buf,
				 size_t nbytes, loff_t off)
{
	struct mem_cgroup *memcg = mem_cgroup_from_css(of_css(of));
	struct parp_tier2_memcg *s = &memcg->parp_tier2;
	bool enabled;
	int ret = kstrtobool(strstrip(buf), &enabled);

	if (ret)
		return ret;
	if (enabled && !tier2_leaf(memcg))
		return -EINVAL;
	mutex_lock(&s->control_mutex);
	if (!s->online) {
		mutex_unlock(&s->control_mutex);
		return -ENODEV;
	}
	tier2_cancel(s, PARP_WSS_DISABLED);
	s->enabled = enabled;
	s->no_progress = 0;
	s->blocked_generation = 0;
	s->cooldown_until_ns = 0;
	s->cycle.armed = true;
	if (enabled)
		mod_delayed_work(system_unbound_wq, &s->predict_work, 0);
	else
		cancel_delayed_work(&s->predict_work);
	mutex_unlock(&s->control_mutex);
	return nbytes;
}

ssize_t parp_tier2_forecast_write(struct kernfs_open_file *of, char *buf,
				size_t nbytes, loff_t off)
{
	struct parp_wss_forecast f = { };
	unsigned int confidence;
	char extra;
	int ret;

	ret = sscanf(buf, "%llu %llu %u %u %u %llu %u "
		     "%llu %llu %llu %llu %llu %llu %llu %llu %c",
		&f.timestamp_ns, &f.rss_bytes, &f.generation, &f.model_version,
		&f.workload_id, &f.profile_id, &confidence,
		&f.result.step[0].wss_bytes, &f.result.step[0].pgfault_delta,
		&f.result.step[1].wss_bytes, &f.result.step[1].pgfault_delta,
		&f.result.step[2].wss_bytes, &f.result.step[2].pgfault_delta,
		&f.result.step[3].wss_bytes, &f.result.step[3].pgfault_delta, &extra);
	if (ret != 15 || confidence > 32767)
		return -EINVAL;
	f.confidence_q15 = confidence;
	ret = parp_tier2_submit_forecast(mem_cgroup_from_css(of_css(of)), &f);
	return ret ? ret : nbytes;
}

ssize_t parp_tier2_profile_write(struct kernfs_open_file *of, char *buf,
			       size_t nbytes, loff_t off)
{
	struct parp_wss_profile p = { };
	char extra;
	int ret;

	ret = sscanf(buf, "%llu %u %u %llu %llu %u "
		     "%u %u %u %u %u %u %u %u %c",
		&p.id, &p.model_version, &p.workload_id, &p.reuse_fault_max,
		&p.turnover_burst_min, &p.live_verified_mask,
		&p.reuse.tp, &p.reuse.fn, &p.reuse.fp, &p.reuse.tn,
		&p.turnover.tp, &p.turnover.fn, &p.turnover.fp, &p.turnover.tn, &extra);
	if (ret != 14)
		return -EINVAL;
	ret = parp_tier2_set_profile(mem_cgroup_from_css(of_css(of)), &p);
	return ret ? ret : nbytes;
}

int parp_tier2_profile_show(struct seq_file *m, void *v)
{
	struct parp_tier2_memcg *s = &mem_cgroup_from_seq(m)->parp_tier2;
	struct parp_wss_profile *p = &s->profile;

	mutex_lock(&s->control_mutex);
	seq_printf(m, "%llu %u %u %llu %llu %u %u %u %u %u %u %u %u %u\n",
		p->id, p->model_version, p->workload_id, p->reuse_fault_max,
		p->turnover_burst_min, p->live_verified_mask,
		p->reuse.tp, p->reuse.fn, p->reuse.fp, p->reuse.tn,
		p->turnover.tp, p->turnover.fn, p->turnover.fp, p->turnover.tn);
	mutex_unlock(&s->control_mutex);
	return 0;
}

int parp_tier2_stats_show(struct seq_file *m, void *v)
{
	struct mem_cgroup *memcg = mem_cgroup_from_seq(m);
	struct parp_tier2_memcg *s = &memcg->parp_tier2;
	unsigned int i;

	mutex_lock(&s->control_mutex);
	seq_printf(m, "enabled %u\nmode %u\nreason %u\ngeneration %u\n",
		s->enabled, s->decision.mode, s->decision.reason, s->generation);
	seq_printf(m, "domain_id %llu\nwindow_ns %llu\nhorizon_ns %llu\n",
		(u64)cgroup_ino(memcg->css.cgroup), PARP_WSS_WINDOW_NS, PARP_WSS_HORIZON_NS);
	seq_printf(m, "usage_bytes %llu\nproducer_rss_bytes %llu\nforecast_timestamp_ns %llu\n",
		parp_tier2_usage_bytes(memcg), s->forecast.rss_bytes, s->forecast.timestamp_ns);
	for (i = 0; i < PARP_WSS_STEPS; i++)
		seq_printf(m, "wss_%u %llu\npgfault_%u %llu\n", i,
			s->forecast.result.step[i].wss_bytes, i,
			s->forecast.result.step[i].pgfault_delta);
	seq_printf(m, "target_bytes %llu\nfault_total %llu\nfault_burst %llu\nprofile_permits_apply %u\n",
		s->decision.target_bytes, s->decision.fault_total,
		s->decision.fault_burst, s->decision.may_apply);
	seq_printf(m, "high_owned %u\nsaved_high_pages %lu\napplied_high_pages %lu\nadmin_write_seq %llu\n",
		s->high_owned, s->saved_high, s->applied_high, s->admin_write_seq);
	seq_printf(m, "cycle_active %u\ncycle_second_stage %u\ncycle_armed %u\n"
		"cycle_budget_bytes %llu\ncycle_low_budget_bytes %llu\ncycle_reclaimed_bytes %llu\n",
		s->cycle.active, s->cycle.second_stage, s->cycle.armed,
		s->cycle.budget_bytes, s->cycle.low_budget_bytes, s->cycle.reclaimed_bytes);
	seq_printf(m, "requested_pages %llu\nactual_reclaimed_pages %llu\nreclaim_calls %llu\n"
		"high_updates %llu\nhigh_restores %llu\nno_progress_stops %llu\nrefault_stops %llu\n",
		s->requested_pages, s->reclaimed_pages, s->reclaim_invocations,
		s->high_updates, s->high_restores, s->no_progress_stops, s->refault_stops);
	seq_printf(m, "legacy_prediction_present %u\nlegacy_wss %llu\nlegacy_pgfault %llu\n",
		s->model_prediction_present, s->model_prediction.result.wss,
		s->model_prediction.result.pgfault);
	mutex_unlock(&s->control_mutex);
	return 0;
}

static int tier2_switch_handler(const struct ctl_table *table, int write,
			       void *buffer, size_t *lenp, loff_t *ppos)
{
	struct mem_cgroup *memcg = NULL;
	int ret = proc_dointvec_minmax(table, write, buffer, lenp, ppos);

	if (ret || !write)
		return ret;
	/* References from the iterator and the mutex close offline/queue races. */
	while ((memcg = mem_cgroup_iter(NULL, memcg, NULL))) {
		struct parp_tier2_memcg *s = &memcg->parp_tier2;

		mutex_lock(&s->control_mutex);
		if (s->online && (s->enabled || s->high_owned))
			mod_delayed_work(system_unbound_wq, &s->predict_work, 0);
		mutex_unlock(&s->control_mutex);
	}
	return 0;
}

static const struct ctl_table tier2_sysctls[] = {
	{
		.procname = "tier2_predict_enabled", .data = &tier2_enabled,
		.maxlen = sizeof(tier2_enabled), .mode = 0644,
		.proc_handler = tier2_switch_handler,
		.extra1 = SYSCTL_ZERO, .extra2 = &tier2_one,
	},
};

static int __init tier2_init(void)
{
	register_sysctl_init("vm", tier2_sysctls);
	return 0;
}
subsys_initcall(tier2_init);
