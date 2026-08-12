// SPDX-License-Identifier: GPL-2.0
#include <linux/fs.h>
#include <linux/iversion.h>
#include <linux/kdev_t.h>
#include <linux/mm.h>
#include <linux/shmem_fs.h>
#include "adapter.h"

static u64 parp_adapter_file_version(struct inode *inode)
{
	if (IS_I_VERSION(inode))
		return inode_peek_iversion(inode);
	if (inode->i_generation)
		return inode->i_generation;
	return parp_vma_signature(inode->i_mode, inode->i_ino,
				  i_size_read(inode), 0, 0);
}

bool parp_file_sample_from_folio(struct parp_reclaim_ctx *ctx,
				 struct folio *folio,
				 struct parp_page_sample *sample)
{
	struct address_space *mapping;
	struct parp_file_evidence evidence;
	struct parp_app_context context;
	struct parp_file_region_key key = {};
	struct inode *inode;
	u64 now = ktime_get_mono_fast_ns();
	u64 freshness_ns;

	if (folio_test_swapcache(folio)) {
		atomic64_inc(&parp_evidence_stats.file_folio_no_mapping);
		return false;
	}
	mapping = folio_mapping(folio);
	if (!mapping || !mapping->host || shmem_mapping(mapping)) {
		atomic64_inc(&parp_evidence_stats.file_folio_no_mapping);
		return false;
	}
	inode = mapping->host;
	key.dev_major = MAJOR(inode->i_sb->s_dev);
	key.dev_minor = MINOR(inode->i_sb->s_dev);
	key.inode = inode->i_ino;
	key.file_version = parp_adapter_file_version(inode);
	if (!parp_evidence_lookup_file(ctx->domain_id, &key, folio->index,
				       folio_nr_pages(folio), &evidence))
		return false;
	if (!parp_context_lookup(ctx->domain_id, now, &context) ||
	    context.bind_generation != evidence.bind_generation ||
	    context.model_version != evidence.model_version) {
		atomic64_inc(&parp_evidence_stats.file_folio_domain_mismatch);
		return false;
	}
	if (now >= evidence.expires_ns) {
		atomic64_inc(&parp_evidence_stats.file_folio_expired);
		return false;
	}
	freshness_ns = now - evidence.windows.last_seen_ns;
	sample->accesses_10s = min_t(u64,
		evidence.windows.access_evidence_10s, U32_MAX);
	sample->accesses_30s = min_t(u64,
		evidence.windows.access_evidence_30s, U32_MAX);
	sample->accesses_60s = min_t(u64,
		evidence.windows.access_evidence_60s, U32_MAX);
	sample->support_q15 = min_t(u64,
		evidence.windows.access_evidence_30s * 1024, PARP_Q15_ONE);
	sample->stability_q15 = evidence.windows.active_intervals_60s ?
		24576 : 8192;
	sample->freshness_q15 = freshness_ns >= PARP_EVIDENCE_TTL_NS ? 0 :
		PARP_Q15_ONE - div64_u64(freshness_ns * PARP_Q15_ONE,
					PARP_EVIDENCE_TTL_NS);
	sample->next_state_q15 = sample->support_q15;
	sample->file_version = key.file_version;
	sample->index = folio->index;
	sample->evidence_valid = true;
	return true;
}
