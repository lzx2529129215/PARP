// SPDX-License-Identifier: GPL-2.0
#include <linux/damon.h>
#include <linux/dcache.h>
#include <linux/fs.h>
#include <linux/iversion.h>
#include <linux/kdev_t.h>
#include <linux/memcontrol.h>
#include <linux/mm.h>
#include <linux/pid.h>
#include <linux/sched/mm.h>
#include <linux/sched/signal.h>
#include <linux/slab.h>
#include <linux/workqueue.h>
#include <trace/events/parp.h>
#include "../internal.h"
#include "adapter.h"

struct parp_damon_work {
	struct work_struct work;
	struct pid *pid;
	struct parp_damon_sample sample;
};

static atomic64_t parp_sample_sequence = ATOMIC64_INIT(0);
static unsigned int max_pending_damon_work = 4096;

module_param(max_pending_damon_work, uint, 0644);

static bool parp_damon_queue_reserve(void)
{
	s64 depth = atomic64_inc_return(&parp_evidence_stats.queue_depth);
	s64 high = atomic64_read(&parp_evidence_stats.queue_high_water);

	if (depth > max_pending_damon_work) {
		atomic64_dec(&parp_evidence_stats.queue_depth);
		atomic64_inc(&parp_evidence_stats.samples_dropped);
		return false;
	}
	while (depth > high) {
		s64 previous = atomic64_cmpxchg(
			&parp_evidence_stats.queue_high_water, high, depth);

		if (previous == high)
			break;
		high = previous;
	}
	return true;
}

static u64 parp_session_cookie(struct task_struct *task,
			       struct mm_struct *mm)
{
	return parp_vma_signature(0, task->group_leader->start_boottime,
			  task_tgid_nr(task), task_pid_nr(task),
			  READ_ONCE(mm->start_code) ^ READ_ONCE(mm->start_data));
}

static bool parp_task_context(struct task_struct *task, u64 now_ns,
			      struct parp_app_context *context)
{
	struct mem_cgroup *memcg;
	u64 domain_id;

	rcu_read_lock();
	memcg = mem_cgroup_from_task(task);
	if (!memcg) {
		rcu_read_unlock();
		return false;
	}
	domain_id = parp_memcg_domain_id(memcg);
	rcu_read_unlock();
	atomic64_inc(&parp_evidence_stats.mm_to_domain_ok);
	if (!parp_context_lookup(domain_id, now_ns, context))
		return false;
	atomic64_inc(&parp_evidence_stats.domain_to_bind_ok);
	return true;
}

bool parp_damon_mm_task_budget_exhausted(struct mm_struct *candidate,
					 struct mm_struct *expected,
					 unsigned int *checked)
{
	if (candidate != expected)
		return false;
	return ++*checked > 256;
}

static bool parp_shared_mm_ambiguous(struct mm_struct *mm,
				     const struct parp_app_context *expected,
				     u64 now_ns)
{
	struct task_struct *group, *task;
	unsigned int checked = 0;
	bool ambiguous = false;

	rcu_read_lock();
	for_each_process_thread(group, task) {
		struct parp_app_context context;

		if (READ_ONCE(task->mm) != mm)
			continue;
		if (parp_damon_mm_task_budget_exhausted(mm, mm, &checked)) {
			ambiguous = true;
			break;
		}
		if (!parp_task_context(task, now_ns, &context) ||
		    context.domain_id != expected->domain_id ||
		    context.app_id != expected->app_id ||
		    context.bind_generation != expected->bind_generation)
			ambiguous = true;
		if (ambiguous)
			break;
	}
	rcu_read_unlock();
	return ambiguous;
}

static u64 parp_file_version(struct inode *inode, u32 *source,
			     u32 *reason_flags, u32 *confidence)
{
	if (IS_I_VERSION(inode)) {
		*source = PARP_FILE_VERSION_IVERSION;
		*confidence = PARP_Q15_ONE;
		return inode_peek_iversion(inode);
	}
	if (inode->i_generation) {
		*source = PARP_FILE_VERSION_GENERATION;
		*confidence = 28672;
		return inode->i_generation;
	}
	*source = PARP_FILE_VERSION_WEAK;
	*reason_flags |= PARP_REASON_FILE_VERSION_WEAK;
	*confidence = 16384;
	return parp_vma_signature(inode->i_mode, inode->i_ino,
				  i_size_read(inode), 0, 0);
}

static u32 parp_backing_class(struct vm_area_struct *vma)
{
	return parp_backing_classify(vma->vm_file, vma_is_shmem(vma),
		vma->vm_file && d_unlinked(vma->vm_file->f_path.dentry),
		vma->vm_flags & VM_EXEC);
}

static u32 parp_anon_classify(struct mm_struct *mm,
			      struct vm_area_struct *vma, u64 start, u64 end)
{
	if (start <= READ_ONCE(mm->start_stack) &&
	    READ_ONCE(mm->start_stack) < end)
		return PARP_ANON_STACK;
	if (start < READ_ONCE(mm->brk) && end > READ_ONCE(mm->start_brk))
		return PARP_ANON_HEAP;
	if (vma_is_shmem(vma))
		return PARP_ANON_SHMEM_STYLE;
	if (vma_is_anonymous(vma))
		return vma->vm_flags & VM_SHARED ? PARP_ANON_SHARED :
			PARP_ANON_PRIVATE;
	return PARP_ANON_SPECIAL;
}

static void parp_record_file(struct parp_damon_sample *sample,
			     const struct parp_app_context *context,
			     struct vm_area_struct *vma, u64 start, u64 end,
			     bool split)
{
	struct inode *inode = file_inode(vma->vm_file);
	struct parp_file_observation observation = {
		.owner = *context,
		.sample = *sample,
		.alignment_confidence_q15 = PARP_Q15_ONE,
	};
	u32 pages;

	observation.sample.region_start = start;
	observation.sample.region_end = end;
	observation.key.dev_major = MAJOR(inode->i_sb->s_dev);
	observation.key.dev_minor = MINOR(inode->i_sb->s_dev);
	observation.key.inode = inode->i_ino;
	if (!parp_file_range_from_vma(vma->vm_start, vma->vm_pgoff,
				      start, end, PAGE_SIZE,
				      &observation.key.start_index, &pages)) {
		parp_evidence_account_unresolved(end - start,
						 PARP_ALIGN_UNRESOLVED);
		return;
	}
	observation.key.nr_pages = pages;
	observation.key.file_version = parp_file_version(inode,
			&observation.version_source, &observation.flags,
			&observation.alignment_confidence_q15);
	observation.backing_class = parp_backing_class(vma);
	if (observation.backing_class == PARP_BACKING_SHMEM) {
		observation.flags |= PARP_REASON_SHMEM_UNSAFE;
		observation.alignment_confidence_q15 =
			min(observation.alignment_confidence_q15, 24576U);
	}
	if (!parp_evidence_update_file(&observation)) {
		atomic64_inc(&parp_evidence_stats.exact);
		trace_parp_region_evidence(&(struct parp_region_trace) {
			.sample_id = sample->sample_id,
			.sample_timestamp_ns = sample->timestamp_ns,
			.pid = sample->pid,
			.tgid = sample->tgid,
			.domain_id = context->domain_id,
			.app_id = context->app_id,
			.bind_generation = context->bind_generation,
			.foreground_epoch_id = context->foreground_epoch_id,
			.model_version = context->model_version,
			.mm_cookie = sample->mm_cookie,
			.region_type = PARP_REGION_FILE,
			.alignment = split ? PARP_ALIGN_SPLIT_EXACT :
				PARP_ALIGN_EXACT,
			.region_start = observation.sample.region_start,
			.region_end = observation.sample.region_end,
			.logical_start = observation.key.start_index,
			.nr_pages = observation.key.nr_pages,
			.dev_major = observation.key.dev_major,
			.dev_minor = observation.key.dev_minor,
			.inode = observation.key.inode,
			.file_version = observation.key.file_version,
			.file_size_bytes = i_size_read(inode),
			.file_page_count = DIV_ROUND_UP_ULL(i_size_read(inode),
							 PAGE_SIZE),
			.sample_interval_us = sample->sample_interval_us,
			.aggregation_interval_us = sample->aggregation_interval_us,
			.nr_accesses = sample->nr_accesses,
			.age = sample->age,
			.confidence_q15 = observation.alignment_confidence_q15,
			.reason_flags = observation.flags,
		});
	} else {
		parp_evidence_account_unresolved(end - start,
				split ? PARP_ALIGN_PARTIAL : PARP_ALIGN_UNRESOLVED);
	}
}

static void parp_record_anon(struct parp_damon_sample *sample,
			     const struct parp_app_context *context,
			     struct mm_struct *mm, struct vm_area_struct *vma,
			     u64 start, u64 end, bool split)
{
	const u64 semantic_mask = VM_READ | VM_WRITE | VM_EXEC | VM_SHARED |
		VM_GROWSDOWN | VM_DONTCOPY | VM_DONTDUMP;
	struct parp_anon_observation observation = {
		.owner = *context,
		.sample = *sample,
		.identity_confidence_q15 = PARP_Q15_ONE,
	};
	u32 relative, pages;

	observation.sample.region_start = start;
	observation.sample.region_end = end;
	observation.anon_class = parp_anon_classify(mm, vma, start, end);
	if (!parp_anon_range_from_vma(vma->vm_start, start, end, PAGE_SIZE,
				      &relative, &pages)) {
		parp_evidence_account_unresolved(end - start,
						 PARP_ALIGN_UNRESOLVED);
		return;
	}
	observation.key = (struct parp_anon_region_key) {
		.domain_id = context->domain_id,
		.foreground_epoch_id = context->foreground_epoch_id,
		.mm_cookie = sample->mm_cookie,
		.process_role = 0,
		.vma_signature = parp_vma_signature(observation.anon_class,
			vma->vm_flags & semantic_mask,
			(vma->vm_end - vma->vm_start) >> PAGE_SHIFT, 0, 0),
		.relative_start_pages = relative,
		.nr_pages = pages,
	};
	if (!parp_evidence_update_anon(&observation)) {
		atomic64_inc(&parp_evidence_stats.exact);
		trace_parp_region_evidence(&(struct parp_region_trace) {
			.sample_id = sample->sample_id,
			.sample_timestamp_ns = sample->timestamp_ns,
			.pid = sample->pid,
			.tgid = sample->tgid,
			.domain_id = context->domain_id,
			.app_id = context->app_id,
			.bind_generation = context->bind_generation,
			.foreground_epoch_id = context->foreground_epoch_id,
			.model_version = context->model_version,
			.mm_cookie = sample->mm_cookie,
			.region_type = PARP_REGION_ANON,
			.alignment = split ? PARP_ALIGN_SPLIT_EXACT :
				PARP_ALIGN_EXACT,
			.region_start = observation.sample.region_start,
			.region_end = observation.sample.region_end,
			.logical_start = observation.key.relative_start_pages,
			.nr_pages = observation.key.nr_pages,
			.vma_signature = observation.key.vma_signature,
			.sample_interval_us = sample->sample_interval_us,
			.aggregation_interval_us = sample->aggregation_interval_us,
			.nr_accesses = sample->nr_accesses,
			.age = sample->age,
			.confidence_q15 = observation.identity_confidence_q15,
			.reason_flags = observation.flags,
		});
	} else {
		parp_evidence_account_unresolved(end - start,
				split ? PARP_ALIGN_PARTIAL : PARP_ALIGN_UNRESOLVED);
	}
}

static void parp_resolve_region(struct parp_damon_sample *sample,
				struct mm_struct *mm,
				const struct parp_app_context *context)
{
	u64 start, end, cursor;
	unsigned int splits = 0;
	bool split;

	if (!parp_align_interval(sample->region_start, sample->region_end,
				 PAGE_SIZE, &start, &end)) {
		parp_evidence_account_unresolved(sample->region_end -
				sample->region_start, PARP_ALIGN_UNRESOLVED);
		return;
	}
	split = start != sample->region_start || end != sample->region_end;
	cursor = start;
	mmap_read_lock(mm);
	while (cursor < end && splits < PARP_MAX_SPLITS_PER_DAMON_REGION) {
		struct vm_area_struct *vma = find_vma(mm, cursor);
		u64 segment_end;

		splits++;
		if (!vma) {
			parp_evidence_account_unresolved(end - cursor,
						 PARP_ALIGN_UNRESOLVED);
			cursor = end;
			break;
		}
		if (cursor < vma->vm_start) {
			segment_end = min_t(u64, end, vma->vm_start);
			parp_evidence_account_unresolved(segment_end - cursor,
						 PARP_ALIGN_UNRESOLVED);
			cursor = segment_end;
			split = true;
			continue;
		}
		segment_end = min_t(u64, end, vma->vm_end);
		split |= cursor != start || segment_end != end;
		if (vma->vm_file)
			parp_record_file(sample, context, vma, cursor, segment_end,
					 split);
		else if (vma_is_anonymous(vma))
			parp_record_anon(sample, context, mm, vma, cursor,
					 segment_end, split);
		else
			parp_evidence_account_unresolved(segment_end - cursor,
						 PARP_ALIGN_UNRESOLVED);
		cursor = segment_end;
	}
	if (cursor < end)
		parp_evidence_account_unresolved(end - cursor, PARP_ALIGN_PARTIAL);
	mmap_read_unlock(mm);
}

static void parp_damon_workfn(struct work_struct *work)
{
	struct parp_damon_work *item =
		container_of(work, struct parp_damon_work, work);
	struct parp_app_context context;
	struct task_struct *task;
	struct mm_struct *mm;

	task = get_pid_task(item->pid, PIDTYPE_PID);
	if (!task)
		goto out;
	item->sample.pid = task_pid_nr(task);
	item->sample.tgid = task_tgid_nr(task);
	mm = get_task_mm(task);
	if (!mm) {
		put_task_struct(task);
		goto out;
	}
	atomic64_inc(&parp_evidence_stats.target_to_mm_ok);
	item->sample.mm_cookie = parp_session_cookie(task, mm);
	item->sample.target_cookie = parp_vma_signature(0,
			task->group_leader->start_boottime, item->sample.pid,
			item->sample.tgid, 0);
	if (!parp_task_context(task, item->sample.timestamp_ns, &context)) {
		parp_evidence_account_unresolved(item->sample.region_end -
			item->sample.region_start, PARP_ALIGN_UNRESOLVED);
		goto put_mm;
	}
	if (parp_shared_mm_ambiguous(mm, &context,
				      item->sample.timestamp_ns)) {
		parp_evidence_account_unresolved(item->sample.region_end -
			item->sample.region_start, PARP_ALIGN_AMBIGUOUS);
		goto put_mm;
	}
	parp_resolve_region(&item->sample, mm, &context);
put_mm:
	mmput(mm);
	put_task_struct(task);
out:
	put_pid(item->pid);
	atomic64_dec(&parp_evidence_stats.queue_depth);
	kfree(item);
}

void parp_damon_aggregate(struct damon_ctx *ctx, struct damon_target *target,
			  struct damon_region *region)
{
	struct parp_damon_work *item;
	u64 sequence;

	if (!target->pid || region->ar.start >= region->ar.end)
		return;
	if (!parp_damon_queue_reserve())
		return;
	item = kzalloc(sizeof(*item), GFP_ATOMIC);
	if (!item) {
		atomic64_dec(&parp_evidence_stats.queue_depth);
		atomic64_inc(&parp_evidence_stats.samples_dropped);
		return;
	}
	sequence = atomic64_inc_return(&parp_sample_sequence);
	item->pid = get_pid(target->pid);
	item->sample = (struct parp_damon_sample) {
		.timestamp_ns = ktime_get_mono_fast_ns(),
		.sample_id = sequence,
		.region_start = region->ar.start,
		.region_end = region->ar.end,
		.nr_accesses = region->nr_accesses,
		.age = region->age,
		.sample_interval_us = min_t(unsigned long,
			ctx->attrs.sample_interval, U32_MAX),
		.aggregation_interval_us = min_t(unsigned long,
			ctx->attrs.aggr_interval, U32_MAX),
	};
	INIT_WORK(&item->work, parp_damon_workfn);
	atomic64_inc(&parp_evidence_stats.samples_queued);
	queue_work(system_unbound_wq, &item->work);
}
