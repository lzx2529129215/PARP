// SPDX-License-Identifier: GPL-2.0
#include <linux/slab.h>
#include <linux/mutex.h>
#include "../internal.h"

static DEFINE_SPINLOCK(parp_snapshot_lock);
static DEFINE_MUTEX(parp_snapshot_update_lock);
static struct parp_snapshot __rcu *parp_active_snapshot;

static void parp_free_snapshot(struct rcu_head *head)
{
	kfree(container_of(head, struct parp_snapshot, rcu));
}

const struct parp_snapshot *parp_snapshot_acquire(void)
{
	rcu_read_lock();
	return rcu_dereference(parp_active_snapshot);
}

void parp_snapshot_release(void)
{
	rcu_read_unlock();
}

static struct parp_snapshot *parp_clone_snapshot(gfp_t gfp)
{
	struct parp_snapshot *old;
	struct parp_snapshot *new;

	new = kzalloc(sizeof(*new), gfp);
	if (!new)
		return NULL;
	rcu_read_lock();
	old = rcu_dereference(parp_active_snapshot);
	if (old)
		memcpy(new, old, sizeof(*new));
	rcu_read_unlock();
	memset(&new->rcu, 0, sizeof(new->rcu));
	new->version++;
	new->created_ns = ktime_get_mono_fast_ns();
	if (!old || !new->expires_ns)
		new->expires_ns = new->created_ns + 300ULL * NSEC_PER_SEC;
	return new;
}

static void parp_publish_snapshot(struct parp_snapshot *new)
{
	struct parp_snapshot *old;

	spin_lock(&parp_snapshot_lock);
	old = rcu_dereference_protected(parp_active_snapshot,
					lockdep_is_held(&parp_snapshot_lock));
	rcu_assign_pointer(parp_active_snapshot, new);
	spin_unlock(&parp_snapshot_lock);
	if (old)
		call_rcu(&old->rcu, parp_free_snapshot);
}

int parp_snapshot_update_binding(const struct parp_binding *binding)
{
	struct parp_snapshot *new;
	unsigned int i, slot;

	mutex_lock(&parp_snapshot_update_lock);
	new = parp_clone_snapshot(GFP_KERNEL);
	if (!new)
		goto nomem;
	slot = min_t(u32, new->nr_bindings, PARP_MAX_DOMAINS - 1);
	for (i = 0; i < new->nr_bindings; i++)
		if (new->bindings[i].domain_id == binding->domain_id) {
			slot = i;
			break;
		}
	new->bindings[slot] = *binding;
	new->nr_bindings = max_t(u32, new->nr_bindings, slot + 1);
	parp_publish_snapshot(new);
	mutex_unlock(&parp_snapshot_update_lock);
	return 0;
nomem:
	mutex_unlock(&parp_snapshot_update_lock);
	return -ENOMEM;
}

int parp_snapshot_update_prior(const struct parp_app_prior *prior)
{
	struct parp_snapshot *new;
	unsigned int i, slot;

	mutex_lock(&parp_snapshot_update_lock);
	rcu_read_lock();
	new = rcu_dereference(parp_active_snapshot);
	if (new && new->prediction_generation) {
		rcu_read_unlock();
		mutex_unlock(&parp_snapshot_update_lock);
		return -EBUSY;
	}
	rcu_read_unlock();
	new = parp_clone_snapshot(GFP_KERNEL);
	if (!new)
		goto nomem;
	slot = min_t(u32, new->nr_priors, PARP_MAX_APPS - 1);
	for (i = 0; i < new->nr_priors; i++)
		if (new->priors[i].app_id == prior->app_id) {
			slot = i;
			break;
		}
	new->priors[slot] = *prior;
	new->nr_priors = max_t(u32, new->nr_priors, slot + 1);
	parp_publish_snapshot(new);
	mutex_unlock(&parp_snapshot_update_lock);
	return 0;
nomem:
	mutex_unlock(&parp_snapshot_update_lock);
	return -ENOMEM;
}

static int parp_validate_prior_batch(const struct parp_app_prior_batch *batch,
				     u64 now_ns)
{
	unsigned int i, j, foreground = 0;

	if (!batch || batch->schema_version != PARP_APP_PRIOR_BATCH_SCHEMA ||
	    !batch->model_version || !batch->prediction_generation ||
	    !batch->nr_entries || batch->nr_entries > PARP_MAX_APPS)
		return -EINVAL;
	if (!batch->timestamp_ns ||
	    batch->timestamp_ns > now_ns + 5ULL * NSEC_PER_SEC ||
	    batch->expiry_ns <= batch->timestamp_ns ||
	    batch->expiry_ns - batch->timestamp_ns >
		PARP_APP_PRIOR_MAX_TTL_NS ||
	    !batch->horizon_ns ||
	    batch->horizon_ns > PARP_APP_PRIOR_MAX_HORIZON_NS)
		return -ERANGE;
	for (i = 0; i < batch->nr_entries; i++) {
		const struct parp_app_prior *entry = &batch->entries[i];

		if (!entry->valid || !entry->app_id ||
		    entry->use_score_q15 > PARP_Q15_ONE || !entry->rank ||
		    entry->rank > batch->nr_entries)
			return -EINVAL;
		foreground += entry->foreground;
		for (j = 0; j < i; j++)
			if (batch->entries[j].app_id == entry->app_id ||
			    batch->entries[j].rank == entry->rank)
				return -EINVAL;
	}
	return foreground > 1 ? -EINVAL : 0;
}

int parp_snapshot_replace_prior_batch(const struct parp_app_prior_batch *batch)
{
	struct parp_snapshot *active;
	struct parp_snapshot *new;
	u64 now_ns = ktime_get_mono_fast_ns();
	unsigned int i;
	int error;

	error = parp_validate_prior_batch(batch, now_ns);
	if (error) {
		atomic64_inc(&parp_scan_budget_stats.invalid_batch);
		return error;
	}
	mutex_lock(&parp_snapshot_update_lock);
	rcu_read_lock();
	active = rcu_dereference(parp_active_snapshot);
	if (active && batch->prediction_generation <=
	    active->prediction_generation) {
		error = batch->prediction_generation ==
			active->prediction_generation ? -EALREADY : -ESTALE;
		rcu_read_unlock();
		mutex_unlock(&parp_snapshot_update_lock);
		atomic64_inc(&parp_scan_budget_stats.stale_batch);
		return error;
	}
	rcu_read_unlock();
	new = parp_clone_snapshot(GFP_KERNEL);
	if (!new) {
		mutex_unlock(&parp_snapshot_update_lock);
		return -ENOMEM;
	}
	memset(new->priors, 0, sizeof(new->priors));
	new->nr_priors = batch->nr_entries;
	new->prediction_schema_version = batch->schema_version;
	new->prediction_model_version = batch->model_version;
	new->prediction_generation = batch->prediction_generation;
	new->prediction_timestamp_ns = batch->timestamp_ns;
	new->prediction_horizon_ns = batch->horizon_ns;
	new->prediction_expiry_ns = batch->expiry_ns;
	new->expires_ns = batch->expiry_ns;
	for (i = 0; i < batch->nr_entries; i++) {
		new->priors[i] = batch->entries[i];
		new->priors[i].updated_ns = batch->timestamp_ns;
		new->priors[i].expires_ns = batch->expiry_ns;
		new->priors[i].horizon_ms = div_u64(batch->horizon_ns,
						     NSEC_PER_MSEC);
		new->priors[i].model_version = batch->model_version;
		new->priors[i].prediction_generation =
			batch->prediction_generation;
	}
	parp_publish_snapshot(new);
	mutex_unlock(&parp_snapshot_update_lock);
	return 0;
}

void parp_snapshot_fill_scan_budget_input(u64 domain_id, u64 now_ns,
					 struct parp_scan_budget_input *input)
{
	const struct parp_snapshot *snapshot;
	const struct parp_binding *binding = NULL;
	const struct parp_app_prior *prior = NULL;
	unsigned int i;

	snapshot = parp_snapshot_acquire();
	if (!snapshot)
		goto out;
	for (i = 0; i < min_t(u32, snapshot->nr_bindings,
			      PARP_MAX_DOMAINS); i++)
		if (snapshot->bindings[i].active &&
		    snapshot->bindings[i].domain_id == domain_id) {
			binding = &snapshot->bindings[i];
			break;
		}
	if (!binding)
		goto out;
	input->flags |= PARP_SCAN_INPUT_BIND_PRESENT;
	input->app_id = binding->app_id;
	input->bind_generation = binding->bind_generation;
	input->bind_expiry_ns = binding->expires_ns;
	input->foreground_epoch_id = binding->epoch_id;
	if (binding->bind_generation && now_ns < binding->expires_ns)
		input->flags |= PARP_SCAN_INPUT_BIND_VALID;
	for (i = 0; i < min_t(u32, snapshot->nr_priors,
			      PARP_MAX_APPS); i++)
		if (snapshot->priors[i].app_id == binding->app_id) {
			prior = &snapshot->priors[i];
			break;
		}
	if (!prior)
		goto metadata;
	input->flags |= PARP_SCAN_INPUT_PRIOR_PRESENT;
	input->foreground = prior->foreground;
	input->app_use_score_q15 = prior->use_score_q15;
	input->app_rank = prior->rank;
	if (prior->valid && now_ns < snapshot->prediction_expiry_ns &&
	    prior->prediction_generation == snapshot->prediction_generation)
		input->flags |= PARP_SCAN_INPUT_PRIOR_VALID;
metadata:
	input->prediction_timestamp_ns = snapshot->prediction_timestamp_ns;
	input->prediction_expiry_ns = snapshot->prediction_expiry_ns;
	input->prediction_generation = snapshot->prediction_generation;
	input->model_version = snapshot->prediction_model_version;
	if (snapshot->prediction_generation)
		input->flags |= PARP_SCAN_INPUT_GENERATION_VALID;
	if (prior && binding->model_version == snapshot->prediction_model_version &&
	    prior->model_version == snapshot->prediction_model_version)
		input->flags |= PARP_SCAN_INPUT_MODEL_COMPATIBLE;
out:
	parp_snapshot_release();
}

bool parp_context_lookup(u64 domain_id, u64 now_ns,
			 struct parp_app_context *context)
{
	const struct parp_snapshot *snapshot;
	const struct parp_binding *binding = NULL;
	const struct parp_app_prior *prior = NULL;
	unsigned int i;
	bool found = false;

	memset(context, 0, sizeof(*context));
	snapshot = parp_snapshot_acquire();
	if (!snapshot || now_ns >= snapshot->expires_ns)
		goto out;
	for (i = 0; i < min_t(u32, snapshot->nr_bindings,
			      PARP_MAX_DOMAINS); i++)
		if (snapshot->bindings[i].active &&
		    snapshot->bindings[i].domain_id == domain_id) {
			binding = &snapshot->bindings[i];
			break;
		}
	if (!binding || now_ns >= binding->expires_ns)
		goto out;
	for (i = 0; i < min_t(u32, snapshot->nr_priors, PARP_MAX_APPS); i++)
		if (snapshot->priors[i].valid &&
		    snapshot->priors[i].app_id == binding->app_id) {
			prior = &snapshot->priors[i];
			break;
		}
	if (!prior || now_ns >= prior->expires_ns ||
	    prior->model_version != binding->model_version)
		goto out;
	*context = (struct parp_app_context) {
		.domain_id = domain_id,
		.app_id = binding->app_id,
		.bind_generation = binding->bind_generation,
		.foreground_epoch_id = binding->epoch_id,
		.bind_expiry_ns = binding->expires_ns,
		.model_version = binding->model_version,
		.app_prior_q15 = prior->use_score_q15,
		.owner_confidence_q15 = PARP_Q15_ONE,
		.owner_source = 1,
	};
	found = true;
out:
	parp_snapshot_release();
	return found;
}
