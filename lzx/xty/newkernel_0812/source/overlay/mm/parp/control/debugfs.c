// SPDX-License-Identifier: GPL-2.0
#include <linux/debugfs.h>
#include <linux/fs.h>
#include <linux/init.h>
#include <linux/module.h>
#include <linux/uaccess.h>
#include "../internal.h"

static struct dentry *parp_dir;
static atomic_t parp_bind_generation = ATOMIC_INIT(0);

static ssize_t mode_read(struct file *file, char __user *buf, size_t len,
			 loff_t *ppos)
{
	char text[16];
	int size = scnprintf(text, sizeof(text), "%u\n", parp_get_mode());

	return simple_read_from_buffer(buf, len, ppos, text, size);
}

static ssize_t mode_write(struct file *file, const char __user *buf,
			  size_t len, loff_t *ppos)
{
	char text[16];
	unsigned int mode;

	if (!len || len >= sizeof(text) || copy_from_user(text, buf, len))
		return -EINVAL;
	text[len] = '\0';
	if (kstrtouint(strim(text), 0, &mode) || parp_set_mode(mode))
		return -EINVAL;
	return len;
}

static const struct file_operations mode_fops = {
	.owner = THIS_MODULE,
	.read = mode_read,
	.write = mode_write,
	.llseek = default_llseek,
};

static ssize_t evidence_mode_read(struct file *file, char __user *buf,
				  size_t len, loff_t *ppos)
{
	char text[16];
	int size = scnprintf(text, sizeof(text), "%u\n",
			     parp_get_evidence_mode());

	return simple_read_from_buffer(buf, len, ppos, text, size);
}

static ssize_t evidence_mode_write(struct file *file, const char __user *buf,
				   size_t len, loff_t *ppos)
{
	char text[16];
	unsigned int mode;

	if (!len || len >= sizeof(text) || copy_from_user(text, buf, len))
		return -EINVAL;
	text[len] = '\0';
	if (kstrtouint(strim(text), 0, &mode) || parp_set_evidence_mode(mode))
		return -EINVAL;
	return len;
}

static const struct file_operations evidence_mode_fops = {
	.owner = THIS_MODULE,
	.read = evidence_mode_read,
	.write = evidence_mode_write,
	.llseek = default_llseek,
};

static ssize_t evidence_stats_read(struct file *file, char __user *buf,
				   size_t len, loff_t *ppos)
{
	char text[1024];
	int size;

	size = scnprintf(text, sizeof(text),
		"samples_queued %lld\n"
		"samples_dropped %lld\n"
		"queue_depth %lld\n"
		"queue_high_water %lld\n"
		"target_to_mm_ok %lld\n"
		"mm_to_domain_ok %lld\n"
		"domain_to_bind_ok %lld\n"
		"exact %lld\npartial %lld\nambiguous %lld\nunresolved %lld\n"
		"unresolved_bytes %lld\nduplicates %lld\nout_of_order %lld\n"
		"table_full %lld\nentries_evicted %lld\nentries_rejected %lld\n"
		"snapshots %lld\n"
		"file_folio_queries %lld\nfile_folio_matches %lld\n"
		"file_folio_domain_mismatch %lld\nfile_folio_no_mapping %lld\n"
		"file_folio_no_region %lld\nfile_folio_expired %lld\n",
		atomic64_read(&parp_evidence_stats.samples_queued),
		atomic64_read(&parp_evidence_stats.samples_dropped),
		atomic64_read(&parp_evidence_stats.queue_depth),
		atomic64_read(&parp_evidence_stats.queue_high_water),
		atomic64_read(&parp_evidence_stats.target_to_mm_ok),
		atomic64_read(&parp_evidence_stats.mm_to_domain_ok),
		atomic64_read(&parp_evidence_stats.domain_to_bind_ok),
		atomic64_read(&parp_evidence_stats.exact),
		atomic64_read(&parp_evidence_stats.partial),
		atomic64_read(&parp_evidence_stats.ambiguous),
		atomic64_read(&parp_evidence_stats.unresolved),
		atomic64_read(&parp_evidence_stats.unresolved_bytes),
		atomic64_read(&parp_evidence_stats.duplicates),
		atomic64_read(&parp_evidence_stats.out_of_order),
		atomic64_read(&parp_evidence_stats.table_full),
		atomic64_read(&parp_evidence_stats.entries_evicted),
		atomic64_read(&parp_evidence_stats.entries_rejected),
		atomic64_read(&parp_evidence_stats.snapshots),
		atomic64_read(&parp_evidence_stats.file_folio_queries),
		atomic64_read(&parp_evidence_stats.file_folio_matches),
		atomic64_read(&parp_evidence_stats.file_folio_domain_mismatch),
		atomic64_read(&parp_evidence_stats.file_folio_no_mapping),
		atomic64_read(&parp_evidence_stats.file_folio_no_region),
		atomic64_read(&parp_evidence_stats.file_folio_expired));
	return simple_read_from_buffer(buf, len, ppos, text, size);
}

static const struct file_operations evidence_stats_fops = {
	.owner = THIS_MODULE,
	.read = evidence_stats_read,
	.llseek = default_llseek,
};

#ifdef CONFIG_PARP_FRONTIER_SCORE
static ssize_t frontier_mode_read(struct file *file, char __user *buf,
				  size_t len, loff_t *ppos)
{
	char text[16];
	int size = scnprintf(text, sizeof(text), "%u\n",
			     parp_frontier_get_mode());

	return simple_read_from_buffer(buf, len, ppos, text, size);
}

static ssize_t frontier_mode_write(struct file *file, const char __user *buf,
				   size_t len, loff_t *ppos)
{
	char text[16];
	unsigned int mode;
	int error;

	if (!len || len >= sizeof(text) || copy_from_user(text, buf, len))
		return -EINVAL;
	text[len] = '\0';
	if (kstrtouint(strim(text), 0, &mode))
		return -EINVAL;
	error = parp_frontier_set_mode(mode);
	return error ? error : len;
}

static const struct file_operations frontier_mode_fops = {
	.owner = THIS_MODULE,
	.read = frontier_mode_read,
	.write = frontier_mode_write,
	.llseek = default_llseek,
};

static ssize_t frontier_stats_read(struct file *file, char __user *buf,
				   size_t len, loff_t *ppos)
{
	char text[1024];
	ssize_t size = parp_frontier_format_stats(text, sizeof(text));

	return simple_read_from_buffer(buf, len, ppos, text, size);
}

static const struct file_operations frontier_stats_fops = {
	.owner = THIS_MODULE,
	.read = frontier_stats_read,
	.llseek = default_llseek,
};
#endif

#ifdef CONFIG_PARP_EFFECTIVE_TIER
static ssize_t effective_tier_mode_read(struct file *file, char __user *buf,
					size_t len, loff_t *ppos)
{
	char text[16];
	int size = scnprintf(text, sizeof(text), "%u\n",
			     parp_effective_tier_get_mode());

	return simple_read_from_buffer(buf, len, ppos, text, size);
}

static ssize_t effective_tier_mode_write(struct file *file,
					 const char __user *buf, size_t len,
					 loff_t *ppos)
{
	char text[16];
	unsigned int mode;
	int error;

	if (!len || len >= sizeof(text) || copy_from_user(text, buf, len))
		return -EINVAL;
	text[len] = '\0';
	error = kstrtouint(strim(text), 0, &mode);
	if (error)
		return error;
	error = parp_effective_tier_set_mode(mode);
	return error ? error : len;
}

static const struct file_operations effective_tier_mode_fops = {
	.owner = THIS_MODULE,
	.read = effective_tier_mode_read,
	.write = effective_tier_mode_write,
	.llseek = default_llseek,
};

static ssize_t effective_tier_config_read(struct file *file,
					  char __user *buf, size_t len,
					  loff_t *ppos)
{
	char *text;
	ssize_t result;
	ssize_t size;

	text = kzalloc(PAGE_SIZE, GFP_KERNEL);
	if (!text)
		return -ENOMEM;
	size = parp_effective_tier_format_config(text, PAGE_SIZE);
	result = simple_read_from_buffer(buf, len, ppos, text, size);
	kfree(text);
	return result;
}

static ssize_t effective_tier_config_write(struct file *file,
					   const char __user *buf, size_t len,
					   loff_t *ppos)
{
	char *text;
	int error;

	if (!len || len > 511)
		return -E2BIG;
	text = memdup_user_nul(buf, len);
	if (IS_ERR(text))
		return PTR_ERR(text);
	error = parp_effective_tier_set_config(strim(text));
	kfree(text);
	return error ? error : len;
}

static const struct file_operations effective_tier_config_fops = {
	.owner = THIS_MODULE,
	.read = effective_tier_config_read,
	.write = effective_tier_config_write,
	.llseek = default_llseek,
};

static ssize_t effective_tier_lock_observe_read(struct file *file,
		char __user *buf, size_t len, loff_t *ppos)
{
	char text[4];
	int size = scnprintf(text, sizeof(text), "%u\n",
			     parp_effective_tier_get_lock_observe());

	return simple_read_from_buffer(buf, len, ppos, text, size);
}

static ssize_t effective_tier_lock_observe_write(struct file *file,
		const char __user *buf, size_t len, loff_t *ppos)
{
	char text[16];
	bool enabled;
	int error;

	if (!len || len >= sizeof(text) || copy_from_user(text, buf, len))
		return -EINVAL;
	text[len] = '\0';
	error = kstrtobool(strim(text), &enabled);
	if (error)
		return error;
	error = parp_effective_tier_set_lock_observe(enabled);
	return error ? error : len;
}

static const struct file_operations effective_tier_lock_observe_fops = {
	.owner = THIS_MODULE,
	.read = effective_tier_lock_observe_read,
	.write = effective_tier_lock_observe_write,
	.llseek = default_llseek,
};

static ssize_t effective_tier_stats_read(struct file *file, char __user *buf,
					 size_t len, loff_t *ppos)
{
	char *text;
	ssize_t result;
	ssize_t size;

	text = kzalloc(PAGE_SIZE, GFP_KERNEL);
	if (!text)
		return -ENOMEM;
	size = parp_effective_tier_format_stats(text, PAGE_SIZE);
	result = simple_read_from_buffer(buf, len, ppos, text, size);
	kfree(text);
	return result;
}

static const struct file_operations effective_tier_stats_fops = {
	.owner = THIS_MODULE,
	.read = effective_tier_stats_read,
	.llseek = default_llseek,
};
#endif

static ssize_t scan_budget_mode_read(struct file *file, char __user *buf,
				     size_t len, loff_t *ppos)
{
	char text[16];
	int size = scnprintf(text, sizeof(text), "%u\n",
			     parp_get_scan_budget_mode());

	return simple_read_from_buffer(buf, len, ppos, text, size);
}

static ssize_t scan_budget_mode_write(struct file *file,
				      const char __user *buf, size_t len,
				      loff_t *ppos)
{
	char text[16];
	unsigned int mode;

	if (!len || len >= sizeof(text) || copy_from_user(text, buf, len))
		return -EINVAL;
	text[len] = '\0';
	if (kstrtouint(strim(text), 0, &mode) ||
	    parp_set_scan_budget_mode(mode))
		return -EINVAL;
	return len;
}

static const struct file_operations scan_budget_mode_fops = {
	.owner = THIS_MODULE,
	.read = scan_budget_mode_read,
	.write = scan_budget_mode_write,
	.llseek = default_llseek,
};

static ssize_t scan_budget_apply_domain_read(struct file *file,
		char __user *buf, size_t len, loff_t *ppos)
{
	char text[32];
	int size = scnprintf(text, sizeof(text), "%llu\n",
			     parp_get_scan_budget_apply_domain());

	return simple_read_from_buffer(buf, len, ppos, text, size);
}

static ssize_t scan_budget_apply_domain_write(struct file *file,
		const char __user *buf, size_t len, loff_t *ppos)
{
	char text[32];
	u64 domain_id;

	if (!len || len >= sizeof(text) || copy_from_user(text, buf, len))
		return -EINVAL;
	text[len] = '\0';
	if (kstrtou64(strim(text), 0, &domain_id) ||
	    parp_set_scan_budget_apply_domain(domain_id))
		return -EINVAL;
	return len;
}

static const struct file_operations scan_budget_apply_domain_fops = {
	.owner = THIS_MODULE,
	.read = scan_budget_apply_domain_read,
	.write = scan_budget_apply_domain_write,
	.llseek = default_llseek,
};

static ssize_t scan_budget_stats_read(struct file *file, char __user *buf,
				      size_t len, loff_t *ppos)
{
	char text[1536];
	int size;

	size = scnprintf(text, sizeof(text),
		"scan_budget_queries %lld\n"
		"target_memcg_queries %lld\n"
		"global_kswapd_bypass %lld\n"
		"global_direct_bypass %lld\n"
		"unknown_scope_bypass %lld\n"
		"no_appbind %lld\nstale_bind %lld\nno_prior %lld\n"
		"expired_prior %lld\nstale_generation %lld\n"
		"model_version_mismatch %lld\n"
		"foreground_decisions %lld\nhigh_prior_decisions %lld\n"
		"medium_prior_decisions %lld\nlow_prior_decisions %lld\n"
		"pressure_bypass %lld\nclamp_min %lld\nclamp_max %lld\n"
		"observe_count %lld\napply_count %lld\n"
		"apply_domain_bypass %lld\n"
		"native_units_total %lld\nproposed_units_total %lld\n"
		"applied_units_total %lld\ndouble_scaling_reject %lld\n"
		"invalid_batch %lld\nstale_batch %lld\n"
		"circuit_breaker_count %lld\n",
		atomic64_read(&parp_scan_budget_stats.scan_budget_queries),
		atomic64_read(&parp_scan_budget_stats.target_memcg_queries),
		atomic64_read(&parp_scan_budget_stats.global_kswapd_bypass),
		atomic64_read(&parp_scan_budget_stats.global_direct_bypass),
		atomic64_read(&parp_scan_budget_stats.unknown_scope_bypass),
		atomic64_read(&parp_scan_budget_stats.no_appbind),
		atomic64_read(&parp_scan_budget_stats.stale_bind),
		atomic64_read(&parp_scan_budget_stats.no_prior),
		atomic64_read(&parp_scan_budget_stats.expired_prior),
		atomic64_read(&parp_scan_budget_stats.stale_generation),
		atomic64_read(&parp_scan_budget_stats.model_version_mismatch),
		atomic64_read(&parp_scan_budget_stats.foreground_decisions),
		atomic64_read(&parp_scan_budget_stats.high_prior_decisions),
		atomic64_read(&parp_scan_budget_stats.medium_prior_decisions),
		atomic64_read(&parp_scan_budget_stats.low_prior_decisions),
		atomic64_read(&parp_scan_budget_stats.pressure_bypass),
		atomic64_read(&parp_scan_budget_stats.clamp_min),
		atomic64_read(&parp_scan_budget_stats.clamp_max),
		atomic64_read(&parp_scan_budget_stats.observe_count),
		atomic64_read(&parp_scan_budget_stats.apply_count),
		atomic64_read(&parp_scan_budget_stats.apply_domain_bypass),
		atomic64_read(&parp_scan_budget_stats.native_units_total),
		atomic64_read(&parp_scan_budget_stats.proposed_units_total),
		atomic64_read(&parp_scan_budget_stats.applied_units_total),
		atomic64_read(&parp_scan_budget_stats.double_scaling_reject),
		atomic64_read(&parp_scan_budget_stats.invalid_batch),
		atomic64_read(&parp_scan_budget_stats.stale_batch),
		atomic64_read(&parp_scan_budget_stats.circuit_breaker_count));
	return simple_read_from_buffer(buf, len, ppos, text, size);
}

static const struct file_operations scan_budget_stats_fops = {
	.owner = THIS_MODULE,
	.read = scan_budget_stats_read,
	.llseek = default_llseek,
};

static int parp_parse_prior_entry(char **argv, struct parp_app_prior *entry)
{
	unsigned int foreground, valid;
	int error;

	error = kstrtou32(argv[0], 0, &entry->app_id);
	error |= kstrtou16(argv[1], 0, &entry->use_score_q15);
	error |= kstrtou16(argv[2], 0, &entry->rank);
	error |= kstrtouint(argv[3], 0, &foreground);
	error |= kstrtouint(argv[4], 0, &valid);
	error |= kstrtou32(argv[5], 0, &entry->flags);
	if (error || foreground > 1 || valid != 1)
		return -EINVAL;
	entry->foreground = foreground;
	entry->valid = valid;
	return 0;
}

static ssize_t prior_batch_write(struct file *file, const char __user *buf,
				 size_t len, loff_t *ppos)
{
	struct parp_app_prior_batch *batch;
	char **argv;
	char *text;
	int argc, error = 0;
	unsigned int i;

	if (!len || len > 4095)
		return -E2BIG;
	text = memdup_user_nul(buf, len);
	if (IS_ERR(text))
		return PTR_ERR(text);
	argv = argv_split(GFP_KERNEL, text, &argc);
	if (!argv) {
		kfree(text);
		return -ENOMEM;
	}
	batch = kzalloc(sizeof(*batch), GFP_KERNEL);
	if (!batch) {
		error = -ENOMEM;
		goto out;
	}
	if (argc < 7) {
		error = -EINVAL;
		goto free_batch;
	}
	error = kstrtou32(argv[0], 0, &batch->schema_version);
	error |= kstrtou32(argv[1], 0, &batch->model_version);
	error |= kstrtou32(argv[2], 0, &batch->prediction_generation);
	error |= kstrtou64(argv[3], 0, &batch->timestamp_ns);
	error |= kstrtou64(argv[4], 0, &batch->horizon_ns);
	error |= kstrtou64(argv[5], 0, &batch->expiry_ns);
	error |= kstrtou32(argv[6], 0, &batch->nr_entries);
	if (error || batch->nr_entries > PARP_MAX_APPS ||
	    argc != 7 + batch->nr_entries * 6) {
		error = -EINVAL;
		goto free_batch;
	}
	for (i = 0; i < batch->nr_entries; i++) {
		error = parp_parse_prior_entry(&argv[7 + i * 6],
					       &batch->entries[i]);
		if (error)
			goto free_batch;
	}
	error = parp_snapshot_replace_prior_batch(batch);
free_batch:
	kfree(batch);
out:
	argv_free(argv);
	kfree(text);
	return error ? error : len;
}

static ssize_t prior_batch_read(struct file *file, char __user *buf,
				size_t len, loff_t *ppos)
{
	const struct parp_snapshot *snapshot;
	char text[160];
	int size = 0;

	snapshot = parp_snapshot_acquire();
	if (snapshot)
		size = scnprintf(text, sizeof(text),
			"schema=%u model=%u generation=%u timestamp_ns=%llu horizon_ns=%llu expiry_ns=%llu entries=%u\n",
			snapshot->prediction_schema_version,
			snapshot->prediction_model_version,
			snapshot->prediction_generation,
			snapshot->prediction_timestamp_ns,
			snapshot->prediction_horizon_ns,
			snapshot->prediction_expiry_ns, snapshot->nr_priors);
	parp_snapshot_release();
	return simple_read_from_buffer(buf, len, ppos, text, size);
}

static const struct file_operations prior_batch_fops = {
	.owner = THIS_MODULE,
	.read = prior_batch_read,
	.write = prior_batch_write,
	.llseek = default_llseek,
};

static ssize_t scan_budget_clear_write(struct file *file,
				       const char __user *buf, size_t len,
				       loff_t *ppos)
{
	char text[32];
	u64 domain_id;

	if (!len || len >= sizeof(text) || copy_from_user(text, buf, len))
		return -EINVAL;
	text[len] = '\0';
	if (kstrtou64(strim(text), 0, &domain_id) || !domain_id)
		return -EINVAL;
	parp_scan_budget_guard_clear(domain_id);
	return len;
}

static const struct file_operations scan_budget_clear_fops = {
	.owner = THIS_MODULE,
	.write = scan_budget_clear_write,
	.llseek = noop_llseek,
};

static ssize_t scan_budget_circuits_read(struct file *file, char __user *buf,
					 size_t len, loff_t *ppos)
{
	struct parp_scan_guard_view views[PARP_MAX_DOMAINS];
	char *text;
	unsigned int i, nr;
	int size = 0;
	ssize_t result;

	text = kzalloc(PAGE_SIZE, GFP_KERNEL);
	if (!text)
		return -ENOMEM;
	nr = parp_scan_budget_guard_snapshot(views, ARRAY_SIZE(views));
	for (i = 0; i < nr && size < PAGE_SIZE; i++)
		size += scnprintf(text + size, PAGE_SIZE - size,
			"domain=%llu generation=%u failures=%u tripped=%u\n",
			views[i].domain_id, views[i].generation,
			views[i].failures, views[i].tripped);
	result = simple_read_from_buffer(buf, len, ppos, text, size);
	kfree(text);
	return result;
}

static const struct file_operations scan_budget_circuits_fops = {
	.owner = THIS_MODULE,
	.read = scan_budget_circuits_read,
	.llseek = default_llseek,
};

static ssize_t bind_write(struct file *file, const char __user *buf,
			  size_t len, loff_t *ppos)
{
	struct parp_binding binding = { .active = true };
	char text[128];
	unsigned long ttl_ms;
	u64 now = ktime_get_mono_fast_ns();

	if (!len || len >= sizeof(text) || copy_from_user(text, buf, len))
		return -EINVAL;
	text[len] = '\0';
	if (sscanf(text, "%llu %u %lu %llu %llu",
		   &binding.domain_id, &binding.app_id, &ttl_ms,
		   &binding.epoch_id, &binding.model_version) != 5)
		return -EINVAL;
	binding.updated_ns = now;
	binding.expires_ns = now + ttl_ms * NSEC_PER_MSEC;
	binding.bind_generation = atomic_inc_return(&parp_bind_generation);
	if (!binding.bind_generation)
		binding.bind_generation = atomic_inc_return(&parp_bind_generation);
	if (parp_snapshot_update_binding(&binding))
		return -ENOMEM;
	return len;
}

static ssize_t prior_write(struct file *file, const char __user *buf,
			   size_t len, loff_t *ppos)
{
	struct parp_app_prior prior = { .valid = true };
	char text[128];
	unsigned long ttl_ms;
	u64 now = ktime_get_mono_fast_ns();

	if (!len || len >= sizeof(text) || copy_from_user(text, buf, len))
		return -EINVAL;
	text[len] = '\0';
	if (sscanf(text, "%u %hu %hu %u %lu %llu", &prior.app_id,
		   &prior.use_score_q15, &prior.rank, &prior.horizon_ms,
		   &ttl_ms, &prior.model_version) != 6)
		return -EINVAL;
	if (prior.use_score_q15 > PARP_Q15_ONE)
		return -ERANGE;
	prior.updated_ns = now;
	prior.expires_ns = now + ttl_ms * NSEC_PER_MSEC;
	if (parp_snapshot_update_prior(&prior))
		return -ENOMEM;
	return len;
}

static const struct file_operations bind_fops = {
	.owner = THIS_MODULE,
	.write = bind_write,
	.llseek = noop_llseek,
};

static const struct file_operations prior_fops = {
	.owner = THIS_MODULE,
	.write = prior_write,
	.llseek = noop_llseek,
};

static int __init parp_init(void)
{
	parp_dir = debugfs_create_dir("parp", NULL);
	debugfs_create_file("mode", 0600, parp_dir, NULL, &mode_fops);
	debugfs_create_file("evidence_mode", 0600, parp_dir, NULL,
			    &evidence_mode_fops);
	debugfs_create_file("evidence_stats", 0400, parp_dir, NULL,
			    &evidence_stats_fops);
#ifdef CONFIG_PARP_FRONTIER_SCORE
	debugfs_create_file("frontier_score_mode", 0600, parp_dir, NULL,
			    &frontier_mode_fops);
	debugfs_create_file("frontier_score_stats", 0400, parp_dir, NULL,
			    &frontier_stats_fops);
#endif
#ifdef CONFIG_PARP_EFFECTIVE_TIER
	debugfs_create_file("effective_tier_mode", 0600, parp_dir, NULL,
			    &effective_tier_mode_fops);
	debugfs_create_file("effective_tier_config", 0600, parp_dir, NULL,
			    &effective_tier_config_fops);
	debugfs_create_file("effective_tier_lock_observe", 0600, parp_dir, NULL,
			    &effective_tier_lock_observe_fops);
	debugfs_create_file("effective_tier_stats", 0400, parp_dir, NULL,
			    &effective_tier_stats_fops);
#endif
	debugfs_create_file("scan_budget_mode", 0600, parp_dir, NULL,
			    &scan_budget_mode_fops);
	debugfs_create_file("scan_budget_apply_domain", 0600, parp_dir, NULL,
			    &scan_budget_apply_domain_fops);
	debugfs_create_file("scan_budget_stats", 0400, parp_dir, NULL,
			    &scan_budget_stats_fops);
	debugfs_create_file("app_prior_batch", 0600, parp_dir, NULL,
			    &prior_batch_fops);
	debugfs_create_file("scan_budget_circuit_clear", 0200, parp_dir, NULL,
			    &scan_budget_clear_fops);
	debugfs_create_file("scan_budget_circuits", 0400, parp_dir, NULL,
			    &scan_budget_circuits_fops);
	debugfs_create_file("app_bind", 0200, parp_dir, NULL, &bind_fops);
	debugfs_create_file("app_prior", 0200, parp_dir, NULL, &prior_fops);
	return 0;
}
subsys_initcall(parp_init);
