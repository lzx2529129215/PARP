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

enum parp_page_type {
	PARP_PAGE_ANON,
	PARP_PAGE_FILE,
};

struct parp_file_key {
	u32 dev_major;
	u32 dev_minor;
	u64 inode;
	u64 file_version;
	u64 start_index;
	u32 nr_pages;
};

struct parp_anon_key {
	u64 domain_id;
	u64 foreground_epoch_id;
	u64 mm_cookie;
	u32 process_role;
	u64 vma_signature;
	u32 relative_start_pages;
	u32 nr_pages;
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
	bool valid;
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
	struct parp_app_prior priors[PARP_MAX_APPS];
	struct parp_binding bindings[PARP_MAX_DOMAINS];
};

struct parp_stats {
	atomic64_t prepare;
	atomic64_t scored;
	atomic64_t proposed[4];
	atomic64_t fallback[8];
	atomic64_t finish;
};

extern struct parp_stats parp_stats;

u16 parp_q15_mul(u16 a, u16 b);
s16 parp_q15_sat_add(s16 a, s16 b);
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
void parp_stats_account(const struct parp_decision *decision);
enum parp_action parp_policy_applied(enum parp_mode mode,
				     enum parp_action original,
				     enum parp_action proposed);
bool parp_budget_allow(unsigned int used, unsigned int limit);
bool parp_fallback_is_native(enum parp_fallback_reason reason);
unsigned int parp_app_prior_bin(u16 score);
bool parp_file_key_equal(const struct parp_file_key *a,
			 const struct parp_file_key *b);
bool parp_anon_key_valid(const struct parp_anon_key *key, u64 epoch);
bool parp_not_expired(u64 expires_ns, u64 now_ns);
int parp_control_init(void);
void parp_control_exit(void);

#endif
