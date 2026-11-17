/* SPDX-License-Identifier: GPL-2.0 */
#ifndef _LINUX_PARP_WSS_H
#define _LINUX_PARP_WSS_H

#include <linux/types.h>

#define PARP_WSS_STEPS 4
#define PARP_WSS_WINDOW_NS 5000000000ULL
#define PARP_WSS_HISTORY_NS 60000000000ULL
#define PARP_WSS_HORIZON_NS (PARP_WSS_STEPS * PARP_WSS_WINDOW_NS)
#define PARP_WSS_BATCH_BYTES (32ULL * 1024 * 1024)
#define PARP_WSS_PERIOD_MS 100
#define PARP_WSS_REUSE 1U
#define PARP_WSS_TURNOVER 2U

struct parp_pred_step {
	u64 wss_bytes;
	u64 pgfault_delta;
};

struct parp_pred_result {
	struct parp_pred_step step[PARP_WSS_STEPS];
};

/* No operation ID: every step covers a disjoint five-second time window. */
struct parp_wss_forecast {
	u64 timestamp_ns; /* CLOCK_MONOTONIC, beginning of window 0 */
	u64 rss_bytes; /* Same-scope producer observation; not memory.current. */
	u64 profile_id;
	u32 generation;
	u32 model_version;
	u32 workload_id;
	u16 confidence_q15;
	struct parp_pred_result result;
};

struct parp_wss_metrics {
	u32 tp;
	u32 fn;
	u32 fp;
	u32 tn;
};

/* Installed by the trusted prediction owner, scoped to one live memcg. */
struct parp_wss_profile {
	u64 id;
	u64 reuse_fault_max;
	u64 turnover_burst_min;
	u32 model_version;
	u32 workload_id;
	u32 live_verified_mask; /* Separate real-kernel verification, default 0. */
	struct parp_wss_metrics reuse;
	struct parp_wss_metrics turnover;
};

enum parp_wss_mode {
	PARP_WSS_OBSERVE,
	PARP_WSS_HIGH,
	PARP_WSS_RECLAIM_LOW,
	PARP_WSS_RECLAIM_REST,
};

enum parp_wss_reason {
	PARP_WSS_OK,
	PARP_WSS_NO_FORECAST,
	PARP_WSS_EXPIRED,
	PARP_WSS_NO_PROFILE,
	PARP_WSS_LOW_CONFIDENCE,
	PARP_WSS_UNKNOWN,
	PARP_WSS_UNVALIDATED,
	PARP_WSS_NOT_LIVE_VERIFIED,
	PARP_WSS_OBSERVE_ONLY,
	PARP_WSS_DISABLED,
	PARP_WSS_ADMIN_OVERRIDE,
	PARP_WSS_LIMIT_CONFLICT,
	PARP_WSS_NO_PROGRESS,
	PARP_WSS_REFAULT,
	PARP_WSS_NON_LEAF,
	PARP_WSS_OVERFLOW,
	PARP_WSS_COMPLETE,
	PARP_WSS_WAIT_REBOUND,
};

struct parp_wss_decision {
	enum parp_wss_mode mode; /* Candidate action; not permission to execute. */
	enum parp_wss_reason reason;
	u64 target_bytes;
	u64 plateau_bytes;
	u64 fault_total;
	u64 fault_burst;
	bool may_apply;
};

struct parp_wss_cycle {
	bool active;
	bool second_stage;
	bool armed; /* A repeated rolling forecast must not refill a cycle. */
	u64 start_ns;
	u64 switch_ns;
	u64 profile_id;
	u64 snapshot_rss_bytes;
	u64 snapshot_usage_bytes;
	u64 budget_bytes;
	u64 low_budget_bytes;
	u64 reclaimed_bytes;
	u64 low_floor_bytes;
	u64 plateau_bytes;
};

bool parp_wss_metrics_pass(const struct parp_wss_metrics *m);
u32 parp_wss_profile_mask(const struct parp_wss_profile *p);
bool parp_wss_profile_matches(const struct parp_wss_forecast *f,
			      const struct parp_wss_profile *p);
bool parp_wss_forecast_valid(const struct parp_wss_forecast *f, u64 now_ns);
bool parp_wss_turnover_shape(const struct parp_wss_forecast *f);
bool parp_wss_target(u64 wss, u64 page_size, u64 *target);
void parp_wss_classify(const struct parp_wss_forecast *f,
		      const struct parp_wss_profile *p, u64 now_ns,
		      u64 page_size, struct parp_wss_decision *d);
void parp_wss_cycle_begin(struct parp_wss_cycle *c,
			  const struct parp_wss_forecast *f,
			  const struct parp_wss_decision *d, u64 usage);
u64 parp_wss_cycle_request(struct parp_wss_cycle *c,
			   const struct parp_wss_forecast *f, u64 now_ns,
			   u64 usage, u64 batch);
void parp_wss_cycle_account(struct parp_wss_cycle *c, u64 actual_bytes);

#endif
