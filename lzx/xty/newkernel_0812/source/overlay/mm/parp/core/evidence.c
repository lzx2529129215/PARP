// SPDX-License-Identifier: GPL-2.0
#include <linux/module.h>
#include <linux/overflow.h>
#include <linux/slab.h>
#include <linux/sort.h>
#include "../internal.h"

struct parp_time_bucket {
	u64 second;
	u64 evidence;
	u32 active_intervals;
};

struct parp_file_builder {
	struct parp_file_observation observation;
	struct parp_time_bucket buckets[PARP_WINDOW_BUCKETS];
	u64 newest_ns;
	u64 first_seen_ns;
	u64 last_access_ns;
	u64 last_sample_id;
};

struct parp_anon_builder {
	struct parp_anon_observation observation;
	struct parp_time_bucket buckets[PARP_WINDOW_BUCKETS];
	u64 newest_ns;
	u64 first_seen_ns;
	u64 last_access_ns;
	u64 last_sample_id;
};

static DEFINE_MUTEX(parp_evidence_lock);
static struct parp_file_builder *parp_file_builders;
static struct parp_anon_builder *parp_anon_builders;
static unsigned int parp_file_capacity;
static unsigned int parp_anon_capacity;
static unsigned int parp_nr_file_builders;
static unsigned int parp_nr_anon_builders;
static unsigned int max_file_regions_per_domain = 4096;
static unsigned int max_anon_regions_per_domain = 2048;
static unsigned int max_total_regions = 65536;
static u64 parp_last_publish_ns;
static u64 parp_evidence_version;
static struct parp_evidence_snapshot __rcu *parp_active_evidence;

module_param(max_file_regions_per_domain, uint, 0644);
module_param(max_anon_regions_per_domain, uint, 0644);
module_param(max_total_regions, uint, 0644);

struct parp_evidence_stats parp_evidence_stats;

static int parp_grow_file_builders(void)
{
	struct parp_file_builder *new;
	unsigned int capacity = parp_file_capacity ?
		min(parp_file_capacity * 2, max_total_regions) :
		min(64U, max_total_regions);

	if (capacity <= parp_file_capacity)
		return -ENOSPC;
	new = kvrealloc(parp_file_builders, sizeof(*new) * capacity,
			GFP_KERNEL | __GFP_ZERO);
	if (!new)
		return -ENOMEM;
	parp_file_builders = new;
	parp_file_capacity = capacity;
	return 0;
}

static int parp_grow_anon_builders(void)
{
	struct parp_anon_builder *new;
	unsigned int capacity = parp_anon_capacity ?
		min(parp_anon_capacity * 2, max_total_regions) :
		min(64U, max_total_regions);

	if (capacity <= parp_anon_capacity)
		return -ENOSPC;
	new = kvrealloc(parp_anon_builders, sizeof(*new) * capacity,
			GFP_KERNEL | __GFP_ZERO);
	if (!new)
		return -ENOMEM;
	parp_anon_builders = new;
	parp_anon_capacity = capacity;
	return 0;
}

static bool parp_sample_valid(const struct parp_damon_sample *sample)
{
	return sample->timestamp_ns && sample->sample_id &&
	       sample->region_start < sample->region_end &&
	       sample->sample_interval_us && sample->aggregation_interval_us;
}

static int parp_update_bucket(struct parp_time_bucket *buckets, u64 timestamp_ns,
			      u32 evidence)
{
	u64 second = div_u64(timestamp_ns, NSEC_PER_SEC);
	struct parp_time_bucket *bucket = &buckets[second % PARP_WINDOW_BUCKETS];

	if (bucket->second != second)
		*bucket = (struct parp_time_bucket) { .second = second };
	if (check_add_overflow(bucket->evidence, (u64)evidence,
			       &bucket->evidence))
		bucket->evidence = U64_MAX;
	if (evidence && bucket->active_intervals != U32_MAX)
		bucket->active_intervals++;
	return 0;
}

static void parp_collect_windows(const struct parp_time_bucket *buckets,
				 u64 now_ns, struct parp_window_stats *windows)
{
	u64 now_second = div_u64(now_ns, NSEC_PER_SEC);
	unsigned int i;

	for (i = 0; i < PARP_WINDOW_BUCKETS; i++) {
		u64 age;

		if (!buckets[i].second || buckets[i].second > now_second)
			continue;
		age = now_second - buckets[i].second;
		if (age >= 60)
			continue;
		windows->access_evidence_60s += buckets[i].evidence;
		windows->active_intervals_60s += buckets[i].active_intervals;
		if (age < 30) {
			windows->access_evidence_30s += buckets[i].evidence;
			windows->active_intervals_30s += buckets[i].active_intervals;
		}
		if (age < 10) {
			windows->access_evidence_10s += buckets[i].evidence;
			windows->active_intervals_10s += buckets[i].active_intervals;
		}
	}
}

static unsigned int parp_file_count_domain(u64 domain_id)
{
	unsigned int i, count = 0;

	for (i = 0; i < parp_nr_file_builders; i++)
		if (parp_file_builders[i].observation.owner.domain_id == domain_id)
			count++;
	return count;
}

static unsigned int parp_anon_count_domain(u64 domain_id)
{
	unsigned int i, count = 0;

	for (i = 0; i < parp_nr_anon_builders; i++)
		if (parp_anon_builders[i].observation.key.domain_id == domain_id)
			count++;
	return count;
}

static u64 parp_file_builder_score(const struct parp_file_builder *builder)
{
	return (u64)builder->observation.alignment_confidence_q15 * 1024 +
		min_t(u32, builder->observation.sample.nr_accesses, 1023) * 32 +
		div_u64(builder->newest_ns, NSEC_PER_SEC);
}

static u64 parp_anon_builder_score(const struct parp_anon_builder *builder)
{
	return (u64)builder->observation.identity_confidence_q15 * 1024 +
		min_t(u32, builder->observation.sample.nr_accesses, 1023) * 32 +
		div_u64(builder->newest_ns, NSEC_PER_SEC);
}

static struct parp_file_builder *
parp_file_victim(const struct parp_file_observation *observation,
		 bool same_domain)
{
	struct parp_file_builder *victim = NULL;
	u64 weakest = U64_MAX;
	u64 incoming = (u64)observation->alignment_confidence_q15 * 1024 +
		min_t(u32, observation->sample.nr_accesses, 1023) * 32 +
		div_u64(observation->sample.timestamp_ns, NSEC_PER_SEC);
	unsigned int i;

	for (i = 0; i < parp_nr_file_builders; i++) {
		u64 score;

		if (same_domain &&
		    parp_file_builders[i].observation.owner.domain_id !=
			observation->owner.domain_id)
			continue;
		score = parp_file_builder_score(&parp_file_builders[i]);
		if (score < weakest) {
			weakest = score;
			victim = &parp_file_builders[i];
		}
	}
	return victim && incoming > weakest ? victim : NULL;
}

static struct parp_anon_builder *
parp_anon_victim(const struct parp_anon_observation *observation,
		 bool same_domain)
{
	struct parp_anon_builder *victim = NULL;
	u64 weakest = U64_MAX;
	u64 incoming = (u64)observation->identity_confidence_q15 * 1024 +
		min_t(u32, observation->sample.nr_accesses, 1023) * 32 +
		div_u64(observation->sample.timestamp_ns, NSEC_PER_SEC);
	unsigned int i;

	for (i = 0; i < parp_nr_anon_builders; i++) {
		u64 score;

		if (same_domain &&
		    parp_anon_builders[i].observation.key.domain_id !=
			observation->key.domain_id)
			continue;
		score = parp_anon_builder_score(&parp_anon_builders[i]);
		if (score < weakest) {
			weakest = score;
			victim = &parp_anon_builders[i];
		}
	}
	return victim && incoming > weakest ? victim : NULL;
}

static int parp_cmp_u64(u64 left, u64 right)
{
	if (left == right)
		return 0;
	return left < right ? -1 : 1;
}

static int parp_file_compare(const void *left, const void *right)
{
	const struct parp_file_evidence *a = left, *b = right;
	int ret;

	ret = parp_cmp_u64(a->domain_id, b->domain_id);
	if (!ret)
		ret = parp_cmp_u64(a->key.dev_major, b->key.dev_major);
	if (!ret)
		ret = parp_cmp_u64(a->key.dev_minor, b->key.dev_minor);
	if (!ret)
		ret = parp_cmp_u64(a->key.inode, b->key.inode);
	if (!ret)
		ret = parp_cmp_u64(a->key.file_version, b->key.file_version);
	if (!ret)
		ret = parp_cmp_u64(a->key.start_index, b->key.start_index);
	return ret;
}

static int parp_anon_compare(const void *left, const void *right)
{
	const struct parp_anon_evidence *a = left, *b = right;

	return memcmp(&a->key, &b->key, sizeof(a->key));
}

static void parp_free_evidence_snapshot(struct rcu_head *head)
{
	struct parp_evidence_snapshot *snapshot =
		container_of(head, struct parp_evidence_snapshot, rcu);

	kfree(snapshot->files);
	kfree(snapshot->anons);
	kfree(snapshot->domains);
	kfree(snapshot);
}

static int parp_publish_evidence_locked(u64 now_ns)
{
	struct parp_evidence_snapshot *new, *old;
	unsigned int i;

	new = kzalloc(sizeof(*new), GFP_KERNEL);
	if (!new)
		return -ENOMEM;
	new->files = kcalloc(parp_nr_file_builders, sizeof(*new->files),
			     GFP_KERNEL);
	new->anons = kcalloc(parp_nr_anon_builders, sizeof(*new->anons),
			     GFP_KERNEL);
	new->domains = kcalloc(parp_nr_anon_builders, sizeof(*new->domains),
			       GFP_KERNEL);
	if ((parp_nr_file_builders && !new->files) ||
	    (parp_nr_anon_builders && (!new->anons || !new->domains)))
		goto nomem;
	new->version = ++parp_evidence_version;
	new->created_ns = now_ns;
	new->expires_ns = now_ns + PARP_EVIDENCE_TTL_NS;
	for (i = 0; i < parp_nr_file_builders; i++) {
		struct parp_file_builder *builder = &parp_file_builders[i];
		struct parp_file_evidence *entry;
		struct parp_app_context active_context;

		if (now_ns >= builder->newest_ns + PARP_EVIDENCE_TTL_NS)
			continue;
		if (!parp_context_lookup(builder->observation.owner.domain_id,
					 now_ns, &active_context) ||
		    active_context.app_id != builder->observation.owner.app_id ||
		    active_context.bind_generation !=
			builder->observation.owner.bind_generation ||
		    active_context.model_version !=
			builder->observation.owner.model_version)
			continue;
		entry = &new->files[new->nr_file_regions++];

		entry->domain_id = builder->observation.owner.domain_id;
		entry->app_id = builder->observation.owner.app_id;
		entry->bind_generation = builder->observation.owner.bind_generation;
		entry->key = builder->observation.key;
		entry->backing_class = builder->observation.backing_class;
		entry->version_source = builder->observation.version_source;
		entry->alignment_confidence_q15 =
			builder->observation.alignment_confidence_q15;
		entry->model_version = builder->observation.owner.model_version;
		entry->snapshot_version = new->version;
		entry->expires_ns = min(builder->observation.owner.bind_expiry_ns,
					builder->newest_ns + PARP_EVIDENCE_TTL_NS);
		entry->windows.first_seen_ns = builder->first_seen_ns;
		entry->windows.last_seen_ns = builder->newest_ns;
		entry->windows.last_access_ns = builder->last_access_ns;
		entry->windows.observed_duration_ns = builder->newest_ns -
			builder->first_seen_ns;
		entry->windows.age = builder->observation.sample.age;
		entry->windows.region_size_pages = builder->observation.key.nr_pages;
		parp_collect_windows(builder->buckets, now_ns, &entry->windows);
	}
	for (i = 0; i < parp_nr_anon_builders; i++) {
		struct parp_anon_builder *builder = &parp_anon_builders[i];
		struct parp_anon_evidence *entry;
		struct parp_domain_anon_evidence *domain = NULL;
		struct parp_app_context active_context;
		unsigned int j;

		if (now_ns >= builder->newest_ns + PARP_EVIDENCE_TTL_NS)
			continue;
		if (!parp_context_lookup(builder->observation.owner.domain_id,
					 now_ns, &active_context) ||
		    active_context.app_id != builder->observation.owner.app_id ||
		    active_context.bind_generation !=
			builder->observation.owner.bind_generation ||
		    active_context.foreground_epoch_id !=
			builder->observation.key.foreground_epoch_id ||
		    active_context.model_version !=
			builder->observation.owner.model_version)
			continue;
		entry = &new->anons[new->nr_anon_regions++];
		entry->key = builder->observation.key;
		entry->app_id = builder->observation.owner.app_id;
		entry->bind_generation = builder->observation.owner.bind_generation;
		entry->model_version = builder->observation.owner.model_version;
		entry->anon_class = builder->observation.anon_class;
		entry->identity_confidence_q15 =
			builder->observation.identity_confidence_q15;
		entry->snapshot_version = new->version;
		entry->expires_ns = min(builder->observation.owner.bind_expiry_ns,
					builder->newest_ns + PARP_EVIDENCE_TTL_NS);
		entry->windows.first_seen_ns = builder->first_seen_ns;
		entry->windows.last_seen_ns = builder->newest_ns;
		entry->windows.last_access_ns = builder->last_access_ns;
		entry->windows.observed_duration_ns = builder->newest_ns -
			builder->first_seen_ns;
		entry->windows.age = builder->observation.sample.age;
		entry->windows.region_size_pages = builder->observation.key.nr_pages;
		parp_collect_windows(builder->buckets, now_ns, &entry->windows);
		for (j = 0; j < new->nr_domains; j++)
			if (new->domains[j].domain_id == entry->key.domain_id) {
				domain = &new->domains[j];
				break;
			}
		if (!domain) {
			domain = &new->domains[new->nr_domains++];
			domain->domain_id = entry->key.domain_id;
			domain->app_id = entry->app_id;
			domain->bind_generation = entry->bind_generation;
			domain->model_version = entry->model_version;
		}
		domain->observed_pages += entry->key.nr_pages;
		if (entry->windows.access_evidence_10s)
			domain->active_pages_10s += entry->key.nr_pages;
		if (entry->windows.access_evidence_30s)
			domain->active_pages_30s += entry->key.nr_pages;
		if (entry->windows.access_evidence_60s)
			domain->active_pages_60s += entry->key.nr_pages;
		domain->confidence_q15 = max(domain->confidence_q15,
					     entry->identity_confidence_q15);
	}
	for (i = 0; i < new->nr_domains; i++)
		new->domains[i].cooling_pages =
			new->domains[i].observed_pages -
			new->domains[i].active_pages_30s;
	sort(new->files, new->nr_file_regions, sizeof(*new->files),
	     parp_file_compare, NULL);
	sort(new->anons, new->nr_anon_regions, sizeof(*new->anons),
	     parp_anon_compare, NULL);
	old = rcu_dereference_protected(parp_active_evidence,
					lockdep_is_held(&parp_evidence_lock));
	rcu_assign_pointer(parp_active_evidence, new);
	if (old)
		call_rcu(&old->rcu, parp_free_evidence_snapshot);
	parp_last_publish_ns = now_ns;
	atomic64_inc(&parp_evidence_stats.snapshots);
	return 0;
nomem:
	kfree(new->files);
	kfree(new->anons);
	kfree(new->domains);
	kfree(new);
	return -ENOMEM;
}

int parp_evidence_publish(u64 now_ns)
{
	int result;

	mutex_lock(&parp_evidence_lock);
	result = parp_publish_evidence_locked(now_ns);
	mutex_unlock(&parp_evidence_lock);
	return result;
}

void parp_evidence_domain_offline(u64 domain_id)
{
	unsigned int i = 0;
	u64 now = ktime_get_mono_fast_ns();

	mutex_lock(&parp_evidence_lock);
	while (i < parp_nr_file_builders) {
		if (parp_file_builders[i].observation.owner.domain_id != domain_id) {
			i++;
			continue;
		}
		parp_file_builders[i] =
			parp_file_builders[--parp_nr_file_builders];
		memset(&parp_file_builders[parp_nr_file_builders], 0,
		       sizeof(*parp_file_builders));
	}
	i = 0;
	while (i < parp_nr_anon_builders) {
		if (parp_anon_builders[i].observation.key.domain_id != domain_id) {
			i++;
			continue;
		}
		parp_anon_builders[i] =
			parp_anon_builders[--parp_nr_anon_builders];
		memset(&parp_anon_builders[parp_nr_anon_builders], 0,
		       sizeof(*parp_anon_builders));
	}
	parp_publish_evidence_locked(now);
	mutex_unlock(&parp_evidence_lock);
}

void parp_evidence_set_limits_for_test(unsigned int file_limit,
				       unsigned int anon_limit,
				       unsigned int total_limit)
{
	mutex_lock(&parp_evidence_lock);
	max_file_regions_per_domain = max(file_limit, 1U);
	max_anon_regions_per_domain = max(anon_limit, 1U);
	max_total_regions = max(total_limit, 1U);
	mutex_unlock(&parp_evidence_lock);
}

int parp_evidence_update_file(const struct parp_file_observation *observation)
{
	struct parp_file_builder *builder = NULL;
	unsigned int i;
	int result = 0;

	if (!parp_sample_valid(&observation->sample) ||
	    !observation->key.nr_pages)
		return -EINVAL;
	mutex_lock(&parp_evidence_lock);
	if (!parp_file_builders && parp_grow_file_builders()) {
		result = -ENOMEM;
		goto out;
	}
	for (i = 0; i < parp_nr_file_builders; i++)
		if (parp_file_builders[i].observation.owner.domain_id ==
		    observation->owner.domain_id &&
		    parp_file_key_equal(&parp_file_builders[i].observation.key,
					&observation->key)) {
			builder = &parp_file_builders[i];
			break;
		}
	if (!builder) {
		bool domain_full = parp_file_count_domain(
			observation->owner.domain_id) >= max_file_regions_per_domain;
		bool total_full = parp_nr_file_builders + parp_nr_anon_builders >=
			max_total_regions;

		if (domain_full || total_full) {
			atomic64_inc(&parp_evidence_stats.table_full);
			builder = parp_file_victim(observation, domain_full);
			if (!builder) {
				atomic64_inc(&parp_evidence_stats.entries_rejected);
				result = -ENOSPC;
				goto out;
			}
			memset(builder, 0, sizeof(*builder));
			atomic64_inc(&parp_evidence_stats.entries_evicted);
		}
		if (!builder && parp_nr_file_builders == parp_file_capacity) {
			result = parp_grow_file_builders();
			if (result)
				goto out;
		}
		if (!builder)
			builder = &parp_file_builders[parp_nr_file_builders++];
		builder->first_seen_ns = observation->sample.timestamp_ns;
	}
	if (builder->last_sample_id == observation->sample.sample_id) {
		atomic64_inc(&parp_evidence_stats.duplicates);
		result = -EALREADY;
		goto out;
	}
	if (builder->newest_ns && observation->sample.timestamp_ns +
	    2 * NSEC_PER_SEC < builder->newest_ns) {
		atomic64_inc(&parp_evidence_stats.out_of_order);
		result = -ESTALE;
		goto out;
	}
	builder->observation = *observation;
	builder->newest_ns = max(builder->newest_ns,
				 observation->sample.timestamp_ns);
	builder->last_sample_id = observation->sample.sample_id;
	if (observation->sample.nr_accesses)
		builder->last_access_ns = observation->sample.timestamp_ns;
	parp_update_bucket(builder->buckets, observation->sample.timestamp_ns,
			   observation->sample.nr_accesses);
	if (observation->sample.timestamp_ns - parp_last_publish_ns >=
	    NSEC_PER_SEC)
		result = parp_publish_evidence_locked(observation->sample.timestamp_ns);
out:
	mutex_unlock(&parp_evidence_lock);
	return result;
}

int parp_evidence_update_anon(const struct parp_anon_observation *observation)
{
	struct parp_anon_builder *builder = NULL;
	unsigned int i;
	int result = 0;

	if (!parp_sample_valid(&observation->sample) ||
	    !observation->key.nr_pages)
		return -EINVAL;
	mutex_lock(&parp_evidence_lock);
	if (!parp_anon_builders && parp_grow_anon_builders()) {
		result = -ENOMEM;
		goto out;
	}
	for (i = 0; i < parp_nr_anon_builders; i++)
		if (!memcmp(&parp_anon_builders[i].observation.key,
			    &observation->key, sizeof(observation->key))) {
			builder = &parp_anon_builders[i];
			break;
		}
	if (!builder) {
		bool domain_full = parp_anon_count_domain(
			observation->key.domain_id) >= max_anon_regions_per_domain;
		bool total_full = parp_nr_file_builders + parp_nr_anon_builders >=
			max_total_regions;

		if (domain_full || total_full) {
			atomic64_inc(&parp_evidence_stats.table_full);
			builder = parp_anon_victim(observation, domain_full);
			if (!builder) {
				atomic64_inc(&parp_evidence_stats.entries_rejected);
				result = -ENOSPC;
				goto out;
			}
			memset(builder, 0, sizeof(*builder));
			atomic64_inc(&parp_evidence_stats.entries_evicted);
		}
		if (!builder && parp_nr_anon_builders == parp_anon_capacity) {
			result = parp_grow_anon_builders();
			if (result)
				goto out;
		}
		if (!builder)
			builder = &parp_anon_builders[parp_nr_anon_builders++];
		builder->first_seen_ns = observation->sample.timestamp_ns;
	}
	if (builder->last_sample_id == observation->sample.sample_id) {
		atomic64_inc(&parp_evidence_stats.duplicates);
		result = -EALREADY;
		goto out;
	}
	if (builder->newest_ns && observation->sample.timestamp_ns +
	    2 * NSEC_PER_SEC < builder->newest_ns) {
		atomic64_inc(&parp_evidence_stats.out_of_order);
		result = -ESTALE;
		goto out;
	}
	builder->observation = *observation;
	builder->newest_ns = max(builder->newest_ns,
				 observation->sample.timestamp_ns);
	builder->last_sample_id = observation->sample.sample_id;
	if (observation->sample.nr_accesses)
		builder->last_access_ns = observation->sample.timestamp_ns;
	parp_update_bucket(builder->buckets, observation->sample.timestamp_ns,
			   observation->sample.nr_accesses);
	if (observation->sample.timestamp_ns - parp_last_publish_ns >=
	    NSEC_PER_SEC)
		result = parp_publish_evidence_locked(observation->sample.timestamp_ns);
out:
	mutex_unlock(&parp_evidence_lock);
	return result;
}

void parp_evidence_account_unresolved(u64 bytes,
				      enum parp_alignment_status status)
{
	atomic64_inc(&parp_evidence_stats.unresolved);
	atomic64_add(bytes, &parp_evidence_stats.unresolved_bytes);
	if (status == PARP_ALIGN_PARTIAL)
		atomic64_inc(&parp_evidence_stats.partial);
	else if (status == PARP_ALIGN_AMBIGUOUS)
		atomic64_inc(&parp_evidence_stats.ambiguous);
}

static int parp_compare_file_point(const struct parp_file_evidence *entry,
				   u64 domain_id,
				   const struct parp_file_region_key *key,
				   u64 index)
{
	int ret;

	ret = parp_cmp_u64(entry->domain_id, domain_id);
	if (!ret)
		ret = parp_cmp_u64(entry->key.dev_major, key->dev_major);
	if (!ret)
		ret = parp_cmp_u64(entry->key.dev_minor, key->dev_minor);
	if (!ret)
		ret = parp_cmp_u64(entry->key.inode, key->inode);
	if (!ret)
		ret = parp_cmp_u64(entry->key.file_version, key->file_version);
	if (ret)
		return ret;
	if (index < entry->key.start_index)
		return 1;
	if (index >= entry->key.start_index + entry->key.nr_pages)
		return -1;
	return 0;
}

bool parp_evidence_lookup_file(u64 domain_id,
			       const struct parp_file_region_key *key,
			       u64 index, u32 nr_pages,
			       struct parp_file_evidence *result)
{
	const struct parp_evidence_snapshot *snapshot;
	unsigned int left = 0, right;
	bool found = false;
	u64 now = ktime_get_mono_fast_ns();

	atomic64_inc(&parp_evidence_stats.file_folio_queries);
	rcu_read_lock();
	snapshot = rcu_dereference(parp_active_evidence);
	if (!snapshot || now >= snapshot->expires_ns)
		goto out;
	right = snapshot->nr_file_regions;
	while (left < right) {
		unsigned int middle = left + (right - left) / 2;
		const struct parp_file_evidence *entry = &snapshot->files[middle];
		int comparison = parp_compare_file_point(entry, domain_id, key, index);

		if (!comparison) {
			if (index + nr_pages <= entry->key.start_index +
			    entry->key.nr_pages && now < entry->expires_ns) {
				*result = *entry;
				found = true;
			}
			break;
		}
		if (comparison < 0)
			left = middle + 1;
		else
			right = middle;
	}
out:
	rcu_read_unlock();
	if (found)
		atomic64_inc(&parp_evidence_stats.file_folio_matches);
	else
		atomic64_inc(&parp_evidence_stats.file_folio_no_region);
	return found;
}

bool parp_evidence_lookup_anon_domain(u64 domain_id,
				      struct parp_domain_anon_evidence *result)
{
	const struct parp_evidence_snapshot *snapshot;
	unsigned int i;
	bool found = false;
	u64 now = ktime_get_mono_fast_ns();

	rcu_read_lock();
	snapshot = rcu_dereference(parp_active_evidence);
	if (!snapshot || now >= snapshot->expires_ns)
		goto out;
	for (i = 0; i < snapshot->nr_domains; i++)
		if (snapshot->domains[i].domain_id == domain_id) {
			*result = snapshot->domains[i];
			found = true;
			break;
		}
out:
	rcu_read_unlock();
	return found;
}
