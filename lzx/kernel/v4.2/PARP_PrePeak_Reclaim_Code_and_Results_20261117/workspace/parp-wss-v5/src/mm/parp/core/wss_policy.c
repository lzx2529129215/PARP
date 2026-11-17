// SPDX-License-Identifier: GPL-2.0
/* Pure arithmetic shared by the kernel controller, KUnit and host tests. */
#include <linux/parp_wss.h>

#define WSS_MAX (~(u64)0)
static u64 wss_min(u64 a, u64 b) { return a < b ? a : b; }
static u64 wss_max(u64 a, u64 b) { return a > b ? a : b; }
static u64 wss_sub(u64 a, u64 b) { return a > b ? a - b : 0; }

bool parp_wss_metrics_pass(const struct parp_wss_metrics *m)
{
	u64 positives = (u64)m->tp + m->fn;
	u64 negatives = (u64)m->fp + m->tn;

	/* FPR <= 5%, recall >= 80%; both classes must exist in held-out data. */
	return positives && negatives && (u64)m->fp * 20 <= negatives &&
	       (u64)m->tp * 5 >= positives * 4;
}

u32 parp_wss_profile_mask(const struct parp_wss_profile *p)
{
	u32 mask = 0;

	if (!p || !p->id || !p->model_version || !p->workload_id ||
	    (p->live_verified_mask & ~(PARP_WSS_REUSE | PARP_WSS_TURNOVER)))
		return 0;
	if (parp_wss_metrics_pass(&p->reuse))
		mask |= PARP_WSS_REUSE;
	if (p->turnover_burst_min && parp_wss_metrics_pass(&p->turnover))
		mask |= PARP_WSS_TURNOVER;
	return mask;
}

bool parp_wss_profile_matches(const struct parp_wss_forecast *f,
			      const struct parp_wss_profile *p)
{
	return f && p && p->id && f->profile_id == p->id &&
	       f->model_version == p->model_version &&
	       f->workload_id == p->workload_id;
}

bool parp_wss_forecast_valid(const struct parp_wss_forecast *f, u64 now_ns)
{
	unsigned int i;

	if (!f || !f->timestamp_ns || f->timestamp_ns > now_ns ||
	    now_ns - f->timestamp_ns >= PARP_WSS_HORIZON_NS ||
	    !f->generation || !f->model_version || !f->workload_id ||
	    !f->rss_bytes || !f->confidence_q15 || f->confidence_q15 > 32767)
		return false;
	for (i = 0; i < PARP_WSS_STEPS; i++)
		if (!f->result.step[i].wss_bytes)
			return false;
	return f->timestamp_ns <= WSS_MAX - PARP_WSS_HORIZON_NS;
}

/* Exact test for maximum <= minimum * 1.1, without multiplication overflow. */
static bool wss_stable(const struct parp_wss_forecast *f, unsigned int first)
{
	u64 low = WSS_MAX, high = 0;
	unsigned int i;

	for (i = first; i < PARP_WSS_STEPS; i++) {
		low = wss_min(low, f->result.step[i].wss_bytes);
		high = wss_max(high, f->result.step[i].wss_bytes);
	}
	return low && high - low <= low / 10;
}

bool parp_wss_turnover_shape(const struct parp_wss_forecast *f)
{
	u64 low = f->result.step[0].wss_bytes;
	u64 plateau = f->result.step[1].wss_bytes;

	return low && plateau > low && plateau - low > low / 10 &&
	       wss_stable(f, 1);
}

bool parp_wss_target(u64 wss, u64 page_size, u64 *target)
{
	u64 margin = wss / 10 + !!(wss % 10);
	u64 value, remainder;

	if (!wss || !page_size || wss > WSS_MAX - margin)
		return false;
	value = wss + margin;
	remainder = value % page_size;
	if (remainder) {
		u64 extra = page_size - remainder;

		if (value > WSS_MAX - extra)
			return false;
		value += extra;
	}
	*target = value;
	return true;
}

void parp_wss_classify(const struct parp_wss_forecast *f,
		      const struct parp_wss_profile *p, u64 now_ns,
		      u64 page_size, struct parp_wss_decision *d)
{
	u64 maximum = 0, plateau = 0, head, tail;
	u32 mask;
	unsigned int i;

	*d = (struct parp_wss_decision){ .reason = PARP_WSS_NO_FORECAST };
	if (!f)
		return;
	if (!parp_wss_forecast_valid(f, now_ns)) {
		d->reason = PARP_WSS_EXPIRED;
		return;
	}
	for (i = 0; i < PARP_WSS_STEPS; i++) {
		u64 faults = f->result.step[i].pgfault_delta;

		if (d->fault_total > WSS_MAX - faults) {
			d->reason = PARP_WSS_OVERFLOW;
			return;
		}
		d->fault_total += faults;
		maximum = wss_max(maximum, f->result.step[i].wss_bytes);
		if (i)
			plateau = wss_max(plateau, f->result.step[i].wss_bytes);
	}
	head = wss_max(f->result.step[0].pgfault_delta,
		       f->result.step[1].pgfault_delta);
	tail = wss_max(f->result.step[2].pgfault_delta,
		       f->result.step[3].pgfault_delta);
	d->fault_burst = wss_sub(head, tail); /* Never wrap a negative burst. */
	if (f->confidence_q15 < 16384) {
		d->reason = PARP_WSS_LOW_CONFIDENCE;
		return;
	}
	if (!parp_wss_profile_matches(f, p)) {
		d->reason = PARP_WSS_NO_PROFILE;
		return;
	}
	d->reason = PARP_WSS_UNKNOWN;
	if (wss_stable(f, 0) && d->fault_total <= p->reuse_fault_max) {
		d->mode = PARP_WSS_HIGH;
		if (!parp_wss_target(maximum, page_size, &d->target_bytes))
			goto overflow;
		/* An existing owner may retain this high after successful shrinking. */
		if (maximum >= f->rss_bytes)
			return;
		mask = PARP_WSS_REUSE;
	} else if (parp_wss_turnover_shape(f) && p->turnover_burst_min &&
		   maximum < f->rss_bytes && d->fault_burst >= p->turnover_burst_min) {
		d->mode = PARP_WSS_RECLAIM_LOW;
		d->plateau_bytes = plateau;
		if (!parp_wss_target(f->result.step[0].wss_bytes, page_size,
				     &d->target_bytes))
			goto overflow;
		mask = PARP_WSS_TURNOVER;
	} else {
		return;
	}
	if (!(parp_wss_profile_mask(p) & mask))
		d->reason = PARP_WSS_UNVALIDATED;
	else if (!(p->live_verified_mask & mask))
		d->reason = PARP_WSS_NOT_LIVE_VERIFIED;
	else {
		d->reason = PARP_WSS_OK;
		d->may_apply = true;
	}
	return;
overflow:
	d->mode = PARP_WSS_OBSERVE;
	d->reason = PARP_WSS_OVERFLOW;
}

void parp_wss_cycle_begin(struct parp_wss_cycle *c,
			  const struct parp_wss_forecast *f,
			  const struct parp_wss_decision *d, u64 usage)
{
	*c = (struct parp_wss_cycle){
		.active = true, .start_ns = f->timestamp_ns,
		.switch_ns = f->timestamp_ns + PARP_WSS_WINDOW_NS,
		.profile_id = f->profile_id,
		.snapshot_rss_bytes = f->rss_bytes,
		.snapshot_usage_bytes = usage,
		.budget_bytes = wss_min(f->rss_bytes, usage),
		.low_budget_bytes = wss_min(f->rss_bytes, wss_sub(usage, d->target_bytes)),
		.low_floor_bytes = d->target_bytes,
		.plateau_bytes = d->plateau_bytes,
	};
}

u64 parp_wss_cycle_request(struct parp_wss_cycle *c,
			   const struct parp_wss_forecast *f, u64 now_ns,
			   u64 usage, u64 batch)
{
	u64 floor = c->low_floor_bytes, current, budget;
	unsigned int index;

	if (!c->active || !parp_wss_forecast_valid(f, now_ns) ||
	    f->profile_id != c->profile_id || now_ns < c->start_ns ||
	    now_ns - c->start_ns >= PARP_WSS_HORIZON_NS)
		return 0;
	index = (unsigned int)((now_ns - f->timestamp_ns) / PARP_WSS_WINDOW_NS);
	current = f->result.step[index].wss_bytes;
	/* A five-second deadline is only a prediction, not an operation boundary.
	 * Require a fresh post-transition forecast before retiring the low floor.
	 */
	if (now_ns >= c->switch_ns && f->timestamp_ns >= c->switch_ns &&
	    current >= c->plateau_bytes - c->plateau_bytes / 10)
		c->second_stage = true;
	if (now_ns >= c->switch_ns && !c->second_stage &&
	    f->timestamp_ns < c->switch_ns)
		return 0;
	if (c->second_stage)
		floor = wss_max(current, c->plateau_bytes);
	budget = c->second_stage ? c->budget_bytes : c->low_budget_bytes;
	return wss_min(batch, wss_min(wss_sub(budget, c->reclaimed_bytes),
				     wss_sub(usage, floor)));
}

void parp_wss_cycle_account(struct parp_wss_cycle *c, u64 actual_bytes)
{
	if (c->reclaimed_bytes > WSS_MAX - actual_bytes)
		c->reclaimed_bytes = WSS_MAX;
	else
		c->reclaimed_bytes += actual_bytes;
	if (c->reclaimed_bytes >= c->budget_bytes)
		c->active = false;
}
