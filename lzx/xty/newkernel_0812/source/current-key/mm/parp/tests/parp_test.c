// SPDX-License-Identifier: GPL-2.0
#include <kunit/test.h>
#include <linux/mm.h>
#include <linux/mm_inline.h>
#include <trace/events/parp.h>
#include "../internal.h"
#include "../adapter/adapter.h"

static void parp_q15_test(struct kunit *test)
{
	KUNIT_EXPECT_EQ(test, parp_q15_mul(PARP_Q15_ONE, PARP_Q15_ONE),
			(u16)32766);
	KUNIT_EXPECT_EQ(test, parp_q15_sat_add(30000, 10000), (s16)S16_MAX);
}

#ifdef CONFIG_PARP_TIER2_WATERMARK
/* xty-test */
/* Tier2 watermark helper tests are built only with the feature. */
static void parp_tier2_ewma_test(struct kunit *test)
{
	KUNIT_EXPECT_EQ(test, parp_tier2_scaled_wmark(1000000, 100, 4096),
			10000ULL);
	KUNIT_EXPECT_EQ(test, parp_tier2_scaled_wmark(100000, 100, 4096),
			4096ULL);
	KUNIT_EXPECT_EQ(test, parp_tier2_ewma_next(1600, 0), 1500ULL);
	KUNIT_EXPECT_EQ(test, parp_tier2_ewma_next(1600, 1600), 1600ULL);
	KUNIT_EXPECT_EQ(test,
			parp_tier2_predict_ms(1000, 900, 800, 300, 10), 50LL);
	KUNIT_EXPECT_EQ(test,
			parp_tier2_predict_ms(1000, 900, 300, 300, 10), 0LL);
	KUNIT_EXPECT_EQ(test,
			parp_tier2_predict_ms(900, 1000, 800, 300, 10), -1LL);
	KUNIT_EXPECT_EQ(test,
			parp_tier2_predict_ms(1000, 900, 800, 300, 0), -1LL);
}
#endif

static void parp_state_test(struct kunit *test)
{
	const s16 features[] = { 10, 12 };
	const s16 centers[] = { 9, 11, 100, 100 };
	u16 table[2 * 2 * 3 * 4] = { 0 };

	table[4] = 25000;
	KUNIT_EXPECT_EQ(test, parp_assign_state(features, centers, 2, 2, 16), 0);
	KUNIT_EXPECT_EQ(test, parp_assign_state(features, centers, 2, 2, 1), -1);
	KUNIT_EXPECT_EQ(test, parp_predict_next_state(table, 2, 0, 0, 1, 0),
			(u16)25000);
}

static void parp_predictor_test(struct kunit *test)
{
	struct parp_page_sample sample = {
		.app_prior_q15 = PARP_Q15_ONE,
		.next_state_q15 = PARP_Q15_ONE,
		.support_q15 = PARP_Q15_ONE,
		.stability_q15 = PARP_Q15_ONE,
		.freshness_q15 = PARP_Q15_ONE,
		.evidence_valid = true,
	};

	KUNIT_EXPECT_GT(test, parp_file_future_score(&sample), (u16)32000);
	sample.app_prior_q15 = 0;
	KUNIT_EXPECT_EQ(test, parp_anon_cold_score(&sample), (u16)32766);
	sample.evidence_valid = false;
	KUNIT_EXPECT_EQ(test, parp_anon_cold_score(&sample), (u16)0);
}

static void parp_metadata_test(struct kunit *test)
{
	struct parp_file_region_key file = {
		.dev_major = 8,
		.dev_minor = 1,
		.inode = 42,
		.file_version = 7,
		.start_index = 128,
		.nr_pages = 16,
	};
	struct parp_file_region_key changed = file;
	struct parp_anon_region_key anon = {
		.domain_id = 9,
		.foreground_epoch_id = 11,
		.mm_cookie = 13,
	};

	KUNIT_EXPECT_EQ(test, parp_app_prior_bin(0), 0U);
	KUNIT_EXPECT_EQ(test, parp_app_prior_bin(PARP_Q15_ONE), 3U);
	KUNIT_EXPECT_TRUE(test, parp_not_expired(101, 100));
	KUNIT_EXPECT_FALSE(test, parp_not_expired(100, 100));
	KUNIT_EXPECT_TRUE(test, parp_file_key_equal(&file, &changed));
	changed.file_version++;
	KUNIT_EXPECT_FALSE(test, parp_file_key_equal(&file, &changed));
	KUNIT_EXPECT_TRUE(test, parp_anon_key_valid(&anon, 11));
	KUNIT_EXPECT_FALSE(test, parp_anon_key_valid(&anon, 12));
	anon.mm_cookie = 0;
	KUNIT_EXPECT_FALSE(test, parp_anon_key_valid(&anon, 11));
	KUNIT_EXPECT_TRUE(test, parp_budget_allow(3, 4));
	KUNIT_EXPECT_FALSE(test, parp_budget_allow(4, 4));
}

static void parp_region_alignment_test(struct kunit *test)
{
	u64 start, end, file_start;
	u32 relative, pages;
	u64 signature;
	const u64 starts[] = { 0x1000, 0x3000, 0x4000 };
	const u64 ends[] = { 0x3000, 0x4000, 0x8000 };

	KUNIT_EXPECT_TRUE(test, parp_align_interval(4097, 12287, 4096,
						   &start, &end));
	KUNIT_EXPECT_EQ(test, start, 4096ULL);
	KUNIT_EXPECT_EQ(test, end, 12288ULL);
	KUNIT_EXPECT_FALSE(test, parp_align_interval(8192, 8192, 4096,
						    &start, &end));
	KUNIT_EXPECT_TRUE(test, parp_file_range_from_vma(0x10000, 7,
			0x12000, 0x15000, 4096, &file_start, &pages));
	KUNIT_EXPECT_EQ(test, file_start, 9ULL);
	KUNIT_EXPECT_EQ(test, pages, 3U);
	KUNIT_EXPECT_TRUE(test, parp_anon_range_from_vma(0x20000,
			0x23000, 0x25000, 4096, &relative, &pages));
	KUNIT_EXPECT_EQ(test, relative, 3U);
	KUNIT_EXPECT_EQ(test, pages, 2U);
	signature = parp_vma_signature(PARP_ANON_PRIVATE, 3, 16, 1, 9);
	KUNIT_EXPECT_EQ(test, signature,
		parp_vma_signature(PARP_ANON_PRIVATE, 3, 16, 1, 9));
	KUNIT_EXPECT_NE(test, signature,
		parp_vma_signature(PARP_ANON_PRIVATE, 7, 16, 1, 9));
	KUNIT_EXPECT_EQ(test,
		parp_backing_classify(true, true, false, false),
		(u32)PARP_BACKING_SHMEM);
	KUNIT_EXPECT_EQ(test,
		parp_backing_classify(true, false, false, true),
		(u32)PARP_BACKING_EXECUTABLE);
	KUNIT_EXPECT_TRUE(test, parp_segments_conserve(starts, ends, 3,
						      0x1000, 0x8000));
	KUNIT_EXPECT_FALSE(test, parp_segments_conserve(starts, ends, 2,
						       0x1000, 0x8000));
}

static void parp_evidence_window_test(struct kunit *test)
{
	u64 now = max_t(u64, ktime_get_mono_fast_ns(), 60 * NSEC_PER_SEC); /* #lzx */
	struct parp_app_prior prior = {
		.app_id = 2,
		.use_score_q15 = 20000,
		.expires_ns = now + 120 * NSEC_PER_SEC,
		.model_version = 4,
		.valid = true,
	};
	struct parp_binding binding = {
		.domain_id = 0xface,
		.app_id = 2,
		.bind_generation = 3,
		.expires_ns = now + 120 * NSEC_PER_SEC,
		.model_version = 4,
		.active = true,
	};
	struct parp_file_observation observation = {
		.key = {
			.dev_major = 8,
			.dev_minor = 2,
			.inode = 0xfeed,
			.file_version = 4,
			.start_index = 100,
			.nr_pages = 8,
		},
		.owner = {
			.domain_id = 0xface,
			.app_id = 2,
			.bind_generation = 3,
			.bind_expiry_ns = U64_MAX,
			.model_version = 4,
		},
		.sample = {
			.sample_interval_us = 5000,
			.aggregation_interval_us = 1000000,
			.region_start = 0x1000,
			.region_end = 0x9000,
		},
		.alignment_confidence_q15 = PARP_Q15_ONE,
	};
	struct parp_file_evidence evidence;
	struct parp_file_region_key query = observation.key;

	KUNIT_ASSERT_EQ(test, parp_snapshot_update_prior(&prior), 0);
	KUNIT_ASSERT_EQ(test, parp_snapshot_update_binding(&binding), 0);
	observation.sample.timestamp_ns = now - 50 * NSEC_PER_SEC;
	observation.sample.sample_id = 0x10001;
	observation.sample.nr_accesses = 1;
	KUNIT_ASSERT_EQ(test, parp_evidence_update_file(&observation), 0);
	observation.sample.timestamp_ns = now - 20 * NSEC_PER_SEC;
	observation.sample.sample_id++;
	observation.sample.nr_accesses = 2;
	KUNIT_ASSERT_EQ(test, parp_evidence_update_file(&observation), 0);
	observation.sample.timestamp_ns = now;
	observation.sample.sample_id++;
	observation.sample.nr_accesses = 3;
	KUNIT_ASSERT_EQ(test, parp_evidence_update_file(&observation), 0);
	KUNIT_ASSERT_EQ(test, parp_evidence_publish(now), 0);
	KUNIT_ASSERT_TRUE(test, parp_evidence_lookup_file(0xface, &query,
							  102, 2, &evidence));
	KUNIT_EXPECT_EQ(test, evidence.windows.access_evidence_10s, 3ULL);
	KUNIT_EXPECT_EQ(test, evidence.windows.access_evidence_30s, 5ULL);
	KUNIT_EXPECT_EQ(test, evidence.windows.access_evidence_60s, 6ULL);
	query.file_version++;
	KUNIT_EXPECT_FALSE(test, parp_evidence_lookup_file(0xface, &query,
							   102, 2, &evidence));
	query.file_version--;
	KUNIT_EXPECT_FALSE(test, parp_evidence_lookup_file(0xbeef, &query,
							   102, 2, &evidence));
	KUNIT_EXPECT_EQ(test, parp_evidence_update_file(&observation), -EALREADY);
	observation.sample.sample_id++;
	observation.sample.timestamp_ns = now - 10 * NSEC_PER_SEC;
	KUNIT_EXPECT_EQ(test, parp_evidence_update_file(&observation), -ESTALE);
	binding.bind_generation++;
	KUNIT_ASSERT_EQ(test, parp_snapshot_update_binding(&binding), 0);
	KUNIT_ASSERT_EQ(test, parp_evidence_publish(now), 0);
	KUNIT_EXPECT_FALSE(test, parp_evidence_lookup_file(0xface, &query,
							   102, 2, &evidence));
}

static void parp_anon_domain_test(struct kunit *test)
{
	u64 now = ktime_get_mono_fast_ns();
	struct parp_app_prior prior = {
		.app_id = 8,
		.use_score_q15 = 16000,
		.expires_ns = now + 120 * NSEC_PER_SEC,
		.model_version = 10,
		.valid = true,
	};
	struct parp_binding binding = {
		.domain_id = 0xa110,
		.app_id = 8,
		.bind_generation = 9,
		.expires_ns = now + 120 * NSEC_PER_SEC,
		.epoch_id = 4,
		.model_version = 10,
		.active = true,
	};
	struct parp_anon_observation observation = {
		.key = {
			.domain_id = 0xa110,
			.foreground_epoch_id = 4,
			.mm_cookie = 5,
			.vma_signature = 6,
			.relative_start_pages = 7,
			.nr_pages = 16,
		},
		.owner = {
			.domain_id = 0xa110,
			.app_id = 8,
			.bind_generation = 9,
			.bind_expiry_ns = U64_MAX,
			.model_version = 10,
		},
		.sample = {
			.timestamp_ns = 1,
			.sample_id = 0x20001,
			.region_start = 0x1000,
			.region_end = 0x11000,
			.nr_accesses = 0,
			.sample_interval_us = 5000,
			.aggregation_interval_us = 1000000,
		},
		.identity_confidence_q15 = PARP_Q15_ONE,
	};
	struct parp_domain_anon_evidence evidence;

	KUNIT_ASSERT_EQ(test, parp_snapshot_update_prior(&prior), 0);
	KUNIT_ASSERT_EQ(test, parp_snapshot_update_binding(&binding), 0);
	observation.sample.timestamp_ns = now;
	KUNIT_ASSERT_EQ(test, parp_evidence_update_anon(&observation), 0);
	KUNIT_ASSERT_EQ(test, parp_evidence_publish(now), 0);
	KUNIT_ASSERT_TRUE(test,
		parp_evidence_lookup_anon_domain(0xa110, &evidence));
	KUNIT_EXPECT_EQ(test, evidence.observed_pages, 16ULL);
	KUNIT_EXPECT_EQ(test, evidence.active_pages_30s, 0ULL);
	KUNIT_EXPECT_EQ(test, evidence.cooling_pages, 16ULL);
	binding.epoch_id++;
	KUNIT_ASSERT_EQ(test, parp_snapshot_update_binding(&binding), 0);
	KUNIT_ASSERT_EQ(test, parp_evidence_publish(now), 0);
	KUNIT_EXPECT_FALSE(test,
		parp_evidence_lookup_anon_domain(0xa110, &evidence));
}

static void parp_side_table_limit_test(struct kunit *test)
{
	u64 now = ktime_get_mono_fast_ns();
	struct parp_app_prior prior = {
		.app_id = 0x44,
		.expires_ns = now + 120 * NSEC_PER_SEC,
		.model_version = 2,
		.valid = true,
	};
	struct parp_binding binding = {
		.domain_id = 0x5151,
		.app_id = 0x44,
		.bind_generation = 1,
		.expires_ns = now + 120 * NSEC_PER_SEC,
		.model_version = 2,
		.active = true,
	};
	struct parp_file_observation observation = {
		.key = {
			.dev_major = 8,
			.dev_minor = 3,
			.inode = 0x101,
			.file_version = 1,
			.nr_pages = 2,
		},
		.owner = {
			.domain_id = 0x5151,
			.app_id = 0x44,
			.bind_generation = 1,
			.bind_expiry_ns = U64_MAX,
			.model_version = 2,
		},
		.sample = {
			.timestamp_ns = 1,
			.sample_id = 0x30001,
			.region_start = 0x1000,
			.region_end = 0x3000,
			.nr_accesses = 1,
			.sample_interval_us = 5000,
			.aggregation_interval_us = 1000000,
		},
		.alignment_confidence_q15 = 4096,
	};
	struct parp_file_region_key first_key;
	struct parp_file_evidence evidence;

	observation.sample.timestamp_ns = now;
	KUNIT_ASSERT_EQ(test, parp_snapshot_update_prior(&prior), 0);
	KUNIT_ASSERT_EQ(test, parp_snapshot_update_binding(&binding), 0);
	parp_evidence_set_limits_for_test(1, 2048, 65536);
	KUNIT_ASSERT_EQ(test, parp_evidence_update_file(&observation), 0);
	first_key = observation.key;
	observation.key.inode++;
	observation.sample.sample_id++;
	observation.sample.timestamp_ns++;
	observation.sample.nr_accesses = 20;
	observation.alignment_confidence_q15 = PARP_Q15_ONE;
	KUNIT_ASSERT_EQ(test, parp_evidence_update_file(&observation), 0);
	KUNIT_ASSERT_EQ(test, parp_evidence_publish(now + 1), 0);
	KUNIT_EXPECT_FALSE(test, parp_evidence_lookup_file(0x5151, &first_key,
							   0, 1, &evidence));
	KUNIT_EXPECT_TRUE(test, parp_evidence_lookup_file(0x5151,
			&observation.key, 0, 1, &evidence));
	parp_evidence_domain_offline(0x5151);
	KUNIT_EXPECT_FALSE(test, parp_evidence_lookup_file(0x5151,
			&observation.key, 0, 1, &evidence));
	parp_evidence_set_limits_for_test(4096, 2048, 65536);
}

static void parp_fallback_test(struct kunit *test)
{
	struct parp_snapshot *snapshot;
	struct parp_page_sample sample = {
		.type = PARP_PAGE_FILE,
		.domain_id = 17,
		.evidence_valid = true,
	};
	struct parp_decision decision;
	u64 now = ktime_get_mono_fast_ns();

	snapshot = kunit_kzalloc(test, sizeof(*snapshot), GFP_KERNEL);
	KUNIT_ASSERT_NOT_NULL(test, snapshot);
	decision = parp_engine_score(NULL, &sample);
	KUNIT_EXPECT_EQ(test, decision.fallback, PARP_FALLBACK_NO_DOMAIN);

	snapshot->expires_ns = now + NSEC_PER_SEC;
	decision = parp_engine_score(snapshot, &sample);
	KUNIT_EXPECT_EQ(test, decision.fallback, PARP_FALLBACK_NO_BINDING);

	snapshot->nr_bindings = 1;
	snapshot->bindings[0] = (struct parp_binding) {
		.domain_id = sample.domain_id,
		.app_id = 23,
		.expires_ns = now + NSEC_PER_SEC,
		.model_version = 3,
		.active = true,
	};
	snapshot->nr_priors = 1;
	snapshot->priors[0] = (struct parp_app_prior) {
		.app_id = 23,
		.expires_ns = now + NSEC_PER_SEC,
		.model_version = 4,
		.valid = true,
	};
	decision = parp_engine_score(snapshot, &sample);
	KUNIT_EXPECT_EQ(test, decision.fallback,
			PARP_FALLBACK_MODEL_VERSION);
	snapshot->priors[0].model_version = 3;
	sample.evidence_valid = false;
	decision = parp_engine_score(snapshot, &sample);
	KUNIT_EXPECT_EQ(test, decision.fallback, PARP_FALLBACK_NO_EVIDENCE);
	sample.evidence_valid = true;
	sample.dirty = true;
	decision = parp_engine_score(snapshot, &sample);
	KUNIT_EXPECT_EQ(test, decision.fallback, PARP_FALLBACK_UNSAFE_FOLIO);
}

static void parp_snapshot_test(struct kunit *test)
{
	const struct parp_snapshot *snapshot;
	struct parp_binding binding = {
		.domain_id = 31,
		.app_id = 37,
		.expires_ns = U64_MAX,
		.model_version = 5,
		.active = true,
	};
	unsigned int i;
	bool found = false;

	KUNIT_ASSERT_EQ(test, parp_snapshot_update_binding(&binding), 0);
	snapshot = parp_snapshot_acquire();
	KUNIT_ASSERT_NOT_NULL(test, snapshot);
	KUNIT_EXPECT_GE(test, snapshot->version, 1ULL);
	for (i = 0; i < snapshot->nr_bindings; i++)
		if (snapshot->bindings[i].domain_id == 31) {
			found = true;
			break;
		}
	KUNIT_EXPECT_TRUE(test, found);
	parp_snapshot_release();
}

static void parp_observe_test(struct kunit *test)
{
	struct parp_decision decision = {
		.original_action = PARP_ACTION_NATIVE,
		.proposed_action = PARP_ACTION_PROTECT,
		.applied_action = PARP_ACTION_NATIVE,
	};

	KUNIT_EXPECT_EQ(test, decision.applied_action, decision.original_action);
	KUNIT_EXPECT_NE(test, decision.proposed_action, decision.applied_action);
	KUNIT_EXPECT_EQ(test,
			parp_policy_applied(PARP_MODE_OBSERVE,
					    PARP_ACTION_NATIVE,
					    PARP_ACTION_PROTECT),
			PARP_ACTION_NATIVE);
}

static void parp_memcg_domain_identity_test(struct kunit *test)
{
	KUNIT_EXPECT_EQ(test, parp_memcg_domain_id(NULL), 0ULL);
}

static struct parp_scan_budget_input parp_budget_input(void)
{
	return (struct parp_scan_budget_input) {
		.domain_id = 0x25,
		.app_id = 1,
		.reclaim_scope = PARP_RECLAIM_SCOPE_TARGET_MEMCG,
		.pressure = PARP_PRESSURE_NORMAL,
		.native_nr_to_scan = 1000,
		.app_use_score_q15 = 30000,
		.app_rank = 1,
		.bind_generation = 2,
		.bind_expiry_ns = U64_MAX,
		.prediction_timestamp_ns = 1,
		.prediction_expiry_ns = U64_MAX,
		.prediction_generation = 3,
		.model_version = 4,
		.now_ns = 2,
		.flags = PARP_SCAN_INPUT_BIND_PRESENT |
			 PARP_SCAN_INPUT_BIND_VALID |
			 PARP_SCAN_INPUT_PRIOR_PRESENT |
			 PARP_SCAN_INPUT_PRIOR_VALID |
			 PARP_SCAN_INPUT_GENERATION_VALID |
			 PARP_SCAN_INPUT_MODEL_COMPATIBLE |
			 PARP_SCAN_INPUT_CIRCUIT_OK,
	};
}

static void parp_scan_budget_gate_matrix_test(struct kunit *test)
{
	struct parp_scan_budget_input input = parp_budget_input();
	struct parp_scan_budget_decision decision;
	struct {
		u32 flag;
		enum parp_scan_budget_reason reason;
	} gates[] = {
		{ PARP_SCAN_INPUT_BIND_PRESENT, PARP_SCAN_REASON_NO_BIND },
		{ PARP_SCAN_INPUT_BIND_VALID, PARP_SCAN_REASON_STALE_BIND },
		{ PARP_SCAN_INPUT_PRIOR_PRESENT, PARP_SCAN_REASON_NO_PRIOR },
		{ PARP_SCAN_INPUT_PRIOR_VALID, PARP_SCAN_REASON_EXPIRED_PRIOR },
		{ PARP_SCAN_INPUT_GENERATION_VALID,
		  PARP_SCAN_REASON_STALE_GENERATION },
		{ PARP_SCAN_INPUT_MODEL_COMPATIBLE,
		  PARP_SCAN_REASON_MODEL_VERSION },
		{ PARP_SCAN_INPUT_CIRCUIT_OK, PARP_SCAN_REASON_CIRCUIT_BREAKER },
	};
	unsigned int i;

	parp_set_scan_budget_apply_domain(input.domain_id);
	parp_set_scan_budget_mode(PARP_SCAN_BUDGET_APPLY);
	for (i = 0; i < ARRAY_SIZE(gates); i++) {
		input = parp_budget_input();
		input.flags &= ~gates[i].flag;
		KUNIT_ASSERT_EQ(test,
				parp_compute_scan_budget(&input, &decision), 0);
		KUNIT_EXPECT_EQ(test, decision.reason, gates[i].reason);
		KUNIT_EXPECT_EQ(test, decision.proposed_nr_to_scan, 1000ULL);
		KUNIT_EXPECT_EQ(test, decision.applied_nr_to_scan, 1000ULL);
	}
	input = parp_budget_input();
	input.bind_expiry_ns = input.now_ns;
	KUNIT_ASSERT_EQ(test, parp_compute_scan_budget(&input, &decision), 0);
	KUNIT_EXPECT_EQ(test, decision.reason,
			(enum parp_scan_budget_reason)PARP_SCAN_REASON_STALE_BIND);
	parp_set_scan_budget_mode(PARP_SCAN_BUDGET_OBSERVE);
	parp_set_scan_budget_apply_domain(0);
}

static void parp_scan_budget_monotonic_repeat_test(struct kunit *test)
{
	struct parp_scan_budget_input input = parp_budget_input();
	struct parp_scan_budget_decision first, second;
	u64 previous = U64_MAX;
	u16 scores[] = { 0, 12287, 12288, 24575, 24576, 32767 };
	unsigned int i;

	for (i = 0; i < ARRAY_SIZE(scores); i++) {
		input.app_use_score_q15 = scores[i];
		KUNIT_ASSERT_EQ(test,
				parp_compute_scan_budget(&input, &first), 0);
		KUNIT_EXPECT_LE(test, first.proposed_nr_to_scan, previous);
		previous = first.proposed_nr_to_scan;
		KUNIT_ASSERT_EQ(test,
				parp_compute_scan_budget(&input, &second), 0);
		KUNIT_EXPECT_EQ(test, first.proposed_nr_to_scan,
				second.proposed_nr_to_scan);
	}
	input.foreground = true;
	KUNIT_ASSERT_EQ(test, parp_compute_scan_budget(&input, &first), 0);
	input.foreground = false;
	KUNIT_ASSERT_EQ(test, parp_compute_scan_budget(&input, &second), 0);
	KUNIT_EXPECT_LE(test, first.proposed_nr_to_scan,
			second.proposed_nr_to_scan);
}

static void parp_scan_budget_basic_test(struct kunit *test)
{
	struct parp_scan_budget_input input = parp_budget_input();
	struct parp_scan_budget_decision decision;

	input.foreground = true;
	KUNIT_ASSERT_EQ(test, parp_compute_scan_budget(&input, &decision), 0);
	KUNIT_EXPECT_EQ(test, decision.proposed_nr_to_scan, 500ULL);
	KUNIT_EXPECT_EQ(test, decision.applied_nr_to_scan, 1000ULL);
	KUNIT_EXPECT_EQ(test, decision.reason,
			(enum parp_scan_budget_reason)PARP_SCAN_REASON_FOREGROUND);
	input.foreground = false;
	KUNIT_ASSERT_EQ(test, parp_compute_scan_budget(&input, &decision), 0);
	KUNIT_EXPECT_EQ(test, decision.proposed_nr_to_scan, 600ULL);
	input.app_use_score_q15 = 18000;
	KUNIT_ASSERT_EQ(test, parp_compute_scan_budget(&input, &decision), 0);
	KUNIT_EXPECT_EQ(test, decision.proposed_nr_to_scan, 800ULL);
	input.app_use_score_q15 = 4000;
	KUNIT_ASSERT_EQ(test, parp_compute_scan_budget(&input, &decision), 0);
	KUNIT_EXPECT_EQ(test, decision.proposed_nr_to_scan, 1200ULL);
}

static void parp_scan_budget_gate_pressure_test(struct kunit *test)
{
	struct parp_scan_budget_input input = parp_budget_input();
	struct parp_scan_budget_decision decision;

	input.foreground = true;
	input.pressure = PARP_PRESSURE_ELEVATED;
	KUNIT_ASSERT_EQ(test, parp_compute_scan_budget(&input, &decision), 0);
	KUNIT_EXPECT_GE(test, decision.proposed_nr_to_scan, 500ULL);
	input.pressure = PARP_PRESSURE_HIGH;
	KUNIT_ASSERT_EQ(test, parp_compute_scan_budget(&input, &decision), 0);
	KUNIT_EXPECT_GE(test, decision.proposed_nr_to_scan, 750ULL);
	input.pressure = PARP_PRESSURE_EMERGENCY;
	KUNIT_ASSERT_EQ(test, parp_compute_scan_budget(&input, &decision), 0);
	KUNIT_EXPECT_EQ(test, decision.proposed_nr_to_scan, 1000ULL);
	input.pressure = PARP_PRESSURE_NORMAL;
	input.reclaim_scope = PARP_RECLAIM_SCOPE_PROACTIVE_MEMCG;
	KUNIT_ASSERT_EQ(test, parp_compute_scan_budget(&input, &decision), 0);
	KUNIT_EXPECT_TRUE(test, decision.valid);
	KUNIT_EXPECT_EQ(test, decision.proposed_nr_to_scan, 500ULL);
	input.reclaim_scope = PARP_RECLAIM_SCOPE_GLOBAL_KSWAPD;
	KUNIT_ASSERT_EQ(test, parp_compute_scan_budget(&input, &decision), 0);
	KUNIT_EXPECT_EQ(test, decision.proposed_nr_to_scan, 1000ULL);
	input.reclaim_scope = PARP_RECLAIM_SCOPE_GLOBAL_DIRECT;
	KUNIT_ASSERT_EQ(test, parp_compute_scan_budget(&input, &decision), 0);
	KUNIT_EXPECT_EQ(test, decision.proposed_nr_to_scan, 1000ULL);
}

static void parp_damon_shared_mm_budget_test(struct kunit *test)
{
	struct mm_struct *expected = (struct mm_struct *)test;
	struct mm_struct *unrelated = (struct mm_struct *)&expected;
	unsigned int checked = 0;
	unsigned int i;

	for (i = 0; i < 300; i++)
		KUNIT_EXPECT_FALSE(test,
			parp_damon_mm_task_budget_exhausted(unrelated, expected,
				&checked));
	KUNIT_EXPECT_EQ(test, checked, 0U);
	for (i = 0; i < 256; i++)
		KUNIT_EXPECT_FALSE(test,
			parp_damon_mm_task_budget_exhausted(expected, expected,
				&checked));
	KUNIT_EXPECT_TRUE(test,
		parp_damon_mm_task_budget_exhausted(expected, expected, &checked));
}

static void parp_scan_budget_bounds_mode_test(struct kunit *test)
{
	struct parp_scan_budget_input input = parp_budget_input();
	struct parp_scan_budget_decision decision;
	u64 first;

	input.native_nr_to_scan = 0;
	KUNIT_ASSERT_EQ(test, parp_compute_scan_budget(&input, &decision), 0);
	KUNIT_EXPECT_EQ(test, decision.proposed_nr_to_scan, 0ULL);
	input.native_nr_to_scan = 1;
	input.foreground = true;
	KUNIT_ASSERT_EQ(test, parp_compute_scan_budget(&input, &decision), 0);
	KUNIT_EXPECT_EQ(test, decision.proposed_nr_to_scan, 1ULL);
	input.native_nr_to_scan = 3;
	KUNIT_ASSERT_EQ(test, parp_compute_scan_budget(&input, &decision), 0);
	KUNIT_EXPECT_EQ(test, decision.proposed_nr_to_scan, 2ULL);
	input.native_nr_to_scan = 100000;
	input.foreground = false;
	input.app_use_score_q15 = 0;
	KUNIT_ASSERT_EQ(test, parp_compute_scan_budget(&input, &decision), 0);
	KUNIT_EXPECT_EQ(test, decision.proposed_nr_to_scan, 104096ULL);
	KUNIT_EXPECT_TRUE(test, decision.reason_flags &
			  BIT(PARP_SCAN_REASON_CLAMP_MAX));
	input.native_nr_to_scan = U64_MAX;
	KUNIT_ASSERT_EQ(test, parp_compute_scan_budget(&input, &decision), 0);
	KUNIT_EXPECT_EQ(test, decision.proposed_nr_to_scan, U64_MAX); /* #lzx */
	input.native_nr_to_scan = 1000;
	input.app_use_score_q15 = 30000;
	parp_set_scan_budget_apply_domain(input.domain_id);
	parp_set_scan_budget_mode(PARP_SCAN_BUDGET_APPLY);
	KUNIT_ASSERT_EQ(test, parp_compute_scan_budget(&input, &decision), 0);
	KUNIT_EXPECT_EQ(test, decision.applied_nr_to_scan,
			decision.proposed_nr_to_scan);
	first = parp_adapter_apply_scan_budget(&decision);
	KUNIT_EXPECT_EQ(test, parp_adapter_apply_scan_budget(&decision), first);
	parp_set_scan_budget_mode(PARP_SCAN_BUDGET_OBSERVE);
	parp_set_scan_budget_apply_domain(0);
}

static void parp_scan_budget_apply_domain_test(struct kunit *test)
{
	struct parp_scan_budget_input input = parp_budget_input();
	struct parp_scan_budget_decision decision;

	parp_set_scan_budget_mode(PARP_SCAN_BUDGET_OBSERVE);
	parp_set_scan_budget_apply_domain(0);
	KUNIT_EXPECT_EQ(test,
			parp_set_scan_budget_mode(PARP_SCAN_BUDGET_APPLY), -EPERM);
	KUNIT_ASSERT_EQ(test,
			parp_set_scan_budget_apply_domain(input.domain_id), 0);
	KUNIT_ASSERT_EQ(test,
			parp_set_scan_budget_mode(PARP_SCAN_BUDGET_APPLY), 0);
	input.domain_id++;
	KUNIT_ASSERT_EQ(test, parp_compute_scan_budget(&input, &decision), 0);
	KUNIT_EXPECT_EQ(test, decision.applied_nr_to_scan,
			decision.native_nr_to_scan);
	KUNIT_EXPECT_TRUE(test, decision.reason_flags &
			  BIT(PARP_SCAN_REASON_APPLY_DOMAIN));
	parp_set_scan_budget_mode(PARP_SCAN_BUDGET_OBSERVE);
	parp_set_scan_budget_apply_domain(0);
}

static void parp_prior_batch_test(struct kunit *test)
{
	u64 now = ktime_get_mono_fast_ns();
	struct parp_app_prior_batch batch = {
		.schema_version = 1,
		.model_version = 9,
		.prediction_generation = 100,
		.timestamp_ns = now,
		.horizon_ns = 30 * NSEC_PER_SEC,
		.expiry_ns = now + 60 * NSEC_PER_SEC,
		.nr_entries = 2,
		.entries = {
			{ .app_id = 1, .use_score_q15 = 30000, .rank = 1,
			  .foreground = true, .valid = true },
			{ .app_id = 2, .use_score_q15 = 10000, .rank = 2,
			  .valid = true },
		},
	};
	const struct parp_snapshot *snapshot;

	KUNIT_ASSERT_EQ(test, parp_snapshot_replace_prior_batch(&batch), 0);
	snapshot = parp_snapshot_acquire();
	KUNIT_ASSERT_NOT_NULL(test, snapshot);
	KUNIT_EXPECT_EQ(test, snapshot->prediction_generation, 100U);
	KUNIT_EXPECT_EQ(test, snapshot->nr_priors, 2U);
	parp_snapshot_release();
	KUNIT_EXPECT_EQ(test, parp_snapshot_replace_prior_batch(&batch), -EALREADY);
	batch.prediction_generation--;
	KUNIT_EXPECT_EQ(test, parp_snapshot_replace_prior_batch(&batch), -ESTALE);
	batch.prediction_generation += 2;
	batch.entries[1].rank = 1;
	KUNIT_EXPECT_EQ(test, parp_snapshot_replace_prior_batch(&batch), -EINVAL);
	snapshot = parp_snapshot_acquire();
	KUNIT_ASSERT_NOT_NULL(test, snapshot);
	KUNIT_EXPECT_EQ(test, snapshot->prediction_generation, 100U);
	KUNIT_EXPECT_EQ(test, snapshot->nr_priors, 2U);
	parp_snapshot_release();
}

static void parp_prior_batch_validation_test(struct kunit *test)
{
	u64 now = ktime_get_mono_fast_ns();
	struct parp_app_prior_batch batch = {
		.schema_version = PARP_APP_PRIOR_BATCH_SCHEMA,
		.model_version = 9,
		.prediction_generation = 200,
		.timestamp_ns = now,
		.horizon_ns = NSEC_PER_SEC,
		.expiry_ns = now + NSEC_PER_SEC,
		.nr_entries = 2,
		.entries = {
			{ .app_id = 11, .use_score_q15 = 20000, .rank = 1,
			  .foreground = true, .valid = true },
			{ .app_id = 12, .use_score_q15 = 10000, .rank = 2,
			  .valid = true },
		},
	};

	batch.entries[1].app_id = batch.entries[0].app_id;
	KUNIT_EXPECT_EQ(test, parp_snapshot_replace_prior_batch(&batch), -EINVAL);
	batch.entries[1].app_id = 12;
	batch.entries[1].foreground = true;
	KUNIT_EXPECT_EQ(test, parp_snapshot_replace_prior_batch(&batch), -EINVAL);
	batch.entries[1].foreground = false;
	batch.entries[1].use_score_q15 = PARP_Q15_ONE + 1;
	KUNIT_EXPECT_EQ(test, parp_snapshot_replace_prior_batch(&batch), -EINVAL);
	batch.entries[1].use_score_q15 = 10000;
	batch.horizon_ns = PARP_APP_PRIOR_MAX_HORIZON_NS + 1;
	KUNIT_EXPECT_EQ(test, parp_snapshot_replace_prior_batch(&batch), -ERANGE);
	batch.horizon_ns = NSEC_PER_SEC;
	batch.expiry_ns = batch.timestamp_ns + PARP_APP_PRIOR_MAX_TTL_NS + 1;
	KUNIT_EXPECT_EQ(test, parp_snapshot_replace_prior_batch(&batch), -ERANGE);
}

static void parp_scan_budget_circuit_test(struct kunit *test)
{
	parp_scan_budget_guard_reset_all_for_test();
	KUNIT_EXPECT_TRUE(test, parp_scan_budget_guard(7, 1, false));
	KUNIT_EXPECT_TRUE(test, parp_scan_budget_guard(7, 1, false));
	KUNIT_EXPECT_FALSE(test, parp_scan_budget_guard(7, 1, false));
	KUNIT_EXPECT_TRUE(test, parp_scan_budget_guard(8, 1, true));
	KUNIT_EXPECT_TRUE(test, parp_scan_budget_guard(7, 2, true));
}

static void parp_phase27_region_trace_schema_test(struct kunit *test)
{
	struct parp_region_trace event = {
		.sample_id = 1,
		.sample_timestamp_ns = 2,
		.bind_generation = 3,
		.foreground_epoch_id = 4,
		.model_version = 5,
		.region_start = 4096,
		.region_end = 8192,
		.dev_major = 8,
		.dev_minor = 5,
		.inode = 9,
		.file_version = 10,
		.file_size_bytes = 4097,
		.file_page_count = 2,
		.vma_signature = 11,
		.sample_interval_us = 5000,
		.aggregation_interval_us = 1000000,
	};

	KUNIT_EXPECT_EQ(test, event.file_page_count, 2ULL);
	KUNIT_EXPECT_EQ(test, event.region_end - event.region_start, 4096ULL);
	KUNIT_EXPECT_EQ(test, event.bind_generation, 3U);
	KUNIT_EXPECT_EQ(test, event.aggregation_interval_us /
				event.sample_interval_us, 200U);
}

#ifdef CONFIG_PARP_EFFECTIVE_TIER
static const struct parp_tier_policy parp_effective_test_policy = {
	.cold_threshold = -48,
	.hot_threshold_1 = 48,
	.hot_threshold_2 = 96,
	.hot_threshold_3 = 144,
	.max_upgrade_tiers = 2,
	.max_downgrade_tiers = 1,
	.require_two_cold = true,
};

static void parp_effective_score_bins_test(struct kunit *test)
{
	const s64 lower_edges[PARP_TIER_FEATURES] = {
		10, 10, 10, 0, 10, 8,
	};
	const s64 above_edges[PARP_TIER_FEATURES] = {
		11, 11, 11, 1, 11, 9,
	};
	s32 score;

	/* Equality belongs to the lower bin in both Python and C. */
	KUNIT_ASSERT_TRUE(test,
		parp_effective_tier_score_values(lower_edges, &score));
	KUNIT_EXPECT_EQ(test, score, 144);
	KUNIT_ASSERT_TRUE(test,
		parp_effective_tier_score_values(above_edges, &score));
	KUNIT_EXPECT_EQ(test, score, 108);
}

static void parp_effective_rank_runtime_contract_test(struct kunit *test)
{
	const s64 values[PARP_TIER_FEATURES] = { 10, 20, 30, 1, 40, 50 };
	struct parp_tier_decision decision;
	s32 first;
	s32 second;

	KUNIT_EXPECT_FALSE(test, PARP_TIER_RUNTIME_PAIRWISE_COMPARISON);
	KUNIT_EXPECT_FALSE(test, PARP_TIER_RUNTIME_CANDIDATE_SORTING);
	KUNIT_EXPECT_FALSE(test, PARP_TIER_RUNTIME_SIGMOID);
	KUNIT_ASSERT_TRUE(test,
		parp_effective_tier_score_values(values, &first));
	KUNIT_ASSERT_TRUE(test,
		parp_effective_tier_score_values(values, &second));
	KUNIT_EXPECT_EQ(test, first, second);

	parp_effective_tier_classify(-48, true, 1, 0, false,
		&parp_effective_test_policy, &decision);
	KUNIT_EXPECT_EQ(test, decision.rank_score_bin,
		(u8)PARP_RANK_SCORE_COLD);
	parp_effective_tier_classify(-47, true, 1, 0, false,
		&parp_effective_test_policy, &decision);
	KUNIT_EXPECT_EQ(test, decision.rank_score_bin,
		(u8)PARP_RANK_SCORE_NEUTRAL);
	parp_effective_tier_classify(48, true, 1, 0, false,
		&parp_effective_test_policy, &decision);
	KUNIT_EXPECT_EQ(test, decision.rank_score_bin,
		(u8)PARP_RANK_SCORE_HOT_1);
	parp_effective_tier_classify(96, true, 1, 0, false,
		&parp_effective_test_policy, &decision);
	KUNIT_EXPECT_EQ(test, decision.rank_score_bin,
		(u8)PARP_RANK_SCORE_HOT_2);
	parp_effective_tier_classify(144, true, 1, 0, false,
		&parp_effective_test_policy, &decision);
	KUNIT_EXPECT_EQ(test, decision.rank_score_bin,
		(u8)PARP_RANK_SCORE_HOT_3);
	parp_effective_tier_classify(96, false, 1, 0, false,
		&parp_effective_test_policy, &decision);
	KUNIT_EXPECT_EQ(test, decision.rank_score_bin,
		(u8)PARP_RANK_SCORE_INVALID);
}

static void parp_effective_delta_thresholds_test(struct kunit *test)
{
	struct parp_tier_policy invalid = parp_effective_test_policy;
	struct parp_tier_policy plus_three = parp_effective_test_policy;
	struct parp_tier_decision invalid_decision;

	KUNIT_EXPECT_EQ(test, parp_score_to_delta_q8(-48,
		&parp_effective_test_policy), -PARP_TIER_SCALE);
	KUNIT_EXPECT_EQ(test, parp_score_to_delta_q8(-47,
		&parp_effective_test_policy), 0);
	KUNIT_EXPECT_EQ(test, parp_score_to_delta_q8(48,
		&parp_effective_test_policy), PARP_TIER_SCALE);
	KUNIT_EXPECT_EQ(test, parp_score_to_delta_q8(96,
		&parp_effective_test_policy), 2 * PARP_TIER_SCALE);
	plus_three.max_upgrade_tiers = 3;
	KUNIT_EXPECT_EQ(test, parp_score_to_delta_q8(96, &plus_three),
		2 * PARP_TIER_SCALE);
	KUNIT_EXPECT_EQ(test, parp_score_to_delta_q8(144, &plus_three),
		3 * PARP_TIER_SCALE);
	invalid.max_downgrade_tiers = 2;
	KUNIT_EXPECT_EQ(test, parp_score_to_delta_q8(-100, &invalid), 0);
	parp_effective_tier_classify(96, true, 1, 0, false, NULL,
		&invalid_decision);
	KUNIT_EXPECT_FALSE(test, invalid_decision.model_valid);
	KUNIT_EXPECT_EQ(test, invalid_decision.delta_tier_q8, 0);
}

static void parp_effective_q8_clamp_test(struct kunit *test)
{
	KUNIT_EXPECT_EQ(test, parp_effective_tier_q8(0, -PARP_TIER_SCALE), 0);
	KUNIT_EXPECT_EQ(test, parp_effective_tier_q8(0, PARP_TIER_SCALE),
		PARP_TIER_SCALE);
	KUNIT_EXPECT_EQ(test, parp_effective_tier_q8(3,
		3 * PARP_TIER_SCALE), 3 * PARP_TIER_SCALE);
	KUNIT_EXPECT_EQ(test, parp_effective_tier_q8(2,
		-PARP_TIER_SCALE), PARP_TIER_SCALE);
}

static void parp_effective_native_modes_test(struct kunit *test)
{
	struct parp_tier_decision upgrade;
	struct parp_tier_decision downgrade;
	struct parp_tier_policy plus_three = parp_effective_test_policy;
	struct parp_tier_decision saturated_upgrade;

	parp_effective_tier_classify(96, true, 1, 1, false,
		&parp_effective_test_policy, &upgrade);
	KUNIT_EXPECT_EQ(test, upgrade.action,
		(u8)PARP_TIER_PREDICTIVE_UPGRADE);
	KUNIT_EXPECT_FALSE(test, parp_effective_tier_actual_protect(
		PARP_EFFECTIVE_TIER_OFF, &upgrade));
	KUNIT_EXPECT_FALSE(test, parp_effective_tier_actual_protect(
		PARP_EFFECTIVE_TIER_SHADOW, &upgrade));
	KUNIT_EXPECT_TRUE(test, parp_effective_tier_actual_protect(
		PARP_EFFECTIVE_TIER_PROTECT_ONLY, &upgrade));
	plus_three.max_upgrade_tiers = 3;
	parp_effective_tier_classify(144, true, 0, PARP_MAX_TIER, false,
		&plus_three, &saturated_upgrade);
	KUNIT_EXPECT_EQ(test, saturated_upgrade.action,
		(u8)PARP_TIER_PREDICTIVE_UPGRADE);
	KUNIT_EXPECT_EQ(test, saturated_upgrade.effective_tier_q8,
		PARP_MAX_TIER * PARP_TIER_SCALE);
	KUNIT_EXPECT_TRUE(test, saturated_upgrade.effective_protect);
	KUNIT_EXPECT_TRUE(test, parp_effective_tier_actual_protect(
		PARP_EFFECTIVE_TIER_PROTECT_ONLY, &saturated_upgrade));
	KUNIT_EXPECT_FALSE(test, parp_effective_tier_actual_protect(
		PARP_EFFECTIVE_TIER_SHADOW, &saturated_upgrade));

	parp_effective_tier_classify(-48, true, 2, 1, false,
		&parp_effective_test_policy, &downgrade);
	KUNIT_EXPECT_EQ(test, downgrade.action,
		(u8)PARP_TIER_PREDICTIVE_DOWNGRADE);
	KUNIT_EXPECT_TRUE(test, parp_effective_tier_actual_protect(
		PARP_EFFECTIVE_TIER_SHADOW, &downgrade));
	KUNIT_EXPECT_TRUE(test, parp_effective_tier_actual_protect(
		PARP_EFFECTIVE_TIER_PROTECT_ONLY, &downgrade));
	KUNIT_EXPECT_FALSE(test, parp_effective_tier_actual_protect(
		PARP_EFFECTIVE_TIER_BIDIRECTIONAL, &downgrade));
}

static void parp_effective_native_fallback_test(struct kunit *test)
{
	struct parp_tier_decision invalid;
	struct parp_tier_decision special;

	parp_effective_tier_classify(1000, false, 2, 1, false,
		&parp_effective_test_policy, &invalid);
	KUNIT_EXPECT_EQ(test, invalid.delta_tier_q8, 0);
	KUNIT_EXPECT_EQ(test, invalid.effective_tier_q8,
		2 * PARP_TIER_SCALE);
	KUNIT_EXPECT_EQ(test, invalid.native_protect,
		invalid.effective_protect);
	KUNIT_EXPECT_EQ(test, invalid.bypass,
		(u8)PARP_TIER_BYPASS_MODEL_INVALID);

	parp_effective_tier_classify(-1000, true, 1, 0, true,
		&parp_effective_test_policy, &special);
	KUNIT_EXPECT_EQ(test, special.action,
		(u8)PARP_TIER_SPECIAL_NATIVE_PROTECT);
	KUNIT_EXPECT_TRUE(test, parp_effective_tier_actual_protect(
		PARP_EFFECTIVE_TIER_BIDIRECTIONAL, &special));
}

static void parp_effective_downgrade_boundary_test(struct kunit *test)
{
	struct parp_tier_decision boundary;
	struct parp_tier_decision strong;

	parp_effective_tier_classify(-48, true, 2, 1, false,
		&parp_effective_test_policy, &boundary);
	KUNIT_EXPECT_EQ(test, boundary.raw_delta_tier_q8, -PARP_TIER_SCALE);
	KUNIT_EXPECT_EQ(test, boundary.action,
		(u8)PARP_TIER_PREDICTIVE_DOWNGRADE);
	parp_effective_tier_classify(-48, true, 3, 1, false,
		&parp_effective_test_policy, &strong);
	KUNIT_EXPECT_EQ(test, strong.action, (u8)PARP_TIER_KEEP_PROTECT);
	KUNIT_EXPECT_TRUE(test, strong.effective_protect);
}

static void parp_effective_policy_flags_test(struct kunit *test)
{
	unsigned long old_flags;
	unsigned long new_flags;
	int next;

	old_flags = (2UL << LRU_GEN_PGOFF) | LRU_REFS_FLAGS |
		BIT(PG_workingset) | BIT(PG_dirty);
	next = parp_effective_tier_next_generation(1);
	KUNIT_ASSERT_EQ(test, next, 2);
	new_flags = parp_effective_tier_policy_flags(old_flags, next);
	KUNIT_EXPECT_EQ(test, new_flags & ~LRU_GEN_MASK,
		old_flags & ~LRU_GEN_MASK);
	KUNIT_EXPECT_EQ(test,
		(new_flags & LRU_GEN_MASK) >> LRU_GEN_PGOFF, 3UL);
	KUNIT_EXPECT_EQ(test, parp_effective_tier_next_generation(
		MAX_NR_GENS - 1), 0);
}

static void parp_effective_budget_test(struct kunit *test)
{
	KUNIT_EXPECT_TRUE(test, parp_effective_tier_budget_allows(
		0, 100, 1, 8, 100));
	KUNIT_EXPECT_FALSE(test, parp_effective_tier_budget_allows(
		1, 100, 1, 8, 100));
	KUNIT_EXPECT_FALSE(test, parp_effective_tier_budget_allows(
		7, 10000, 2, 8, 100));
	KUNIT_EXPECT_FALSE(test, parp_effective_tier_budget_allows(
		0, 10000, 512, 1024, 100));
	KUNIT_EXPECT_FALSE(test, parp_effective_tier_budget_allows(
		ULONG_MAX, ULONG_MAX, 1, ULONG_MAX, 10000));
}

static void parp_effective_upgrade_gate_test(struct kunit *test)
{
	enum parp_tier_bypass_reason bypass;

	KUNIT_EXPECT_FALSE(test, parp_effective_tier_upgrade_gate(true, false,
		&bypass));
	KUNIT_EXPECT_EQ(test, bypass, PARP_TIER_BYPASS_PRESSURE);
	KUNIT_EXPECT_FALSE(test, parp_effective_tier_upgrade_gate(false, true,
		&bypass));
	KUNIT_EXPECT_EQ(test, bypass, PARP_TIER_BYPASS_NO_PROGRESS);
	KUNIT_EXPECT_TRUE(test, parp_effective_tier_upgrade_gate(false, false,
		&bypass));
	KUNIT_EXPECT_EQ(test, bypass, PARP_TIER_BYPASS_NONE);
}

static void parp_effective_tier_pressure_policy_test(struct kunit *test)
{
	KUNIT_EXPECT_EQ(test, PARP_PRESSURE_NORMAL, PARP_PRESSURE_LOW);
	KUNIT_EXPECT_EQ(test, PARP_PRESSURE_ELEVATED, PARP_PRESSURE_MEDIUM);
	KUNIT_EXPECT_EQ(test, PARP_PRESSURE_EMERGENCY,
		PARP_PRESSURE_CRITICAL);
	KUNIT_EXPECT_EQ(test, parp_effective_tier_pressure_level(12, false,
		0, 0), (u8)PARP_PRESSURE_LOW);
	KUNIT_EXPECT_EQ(test, parp_effective_tier_pressure_level(8, false,
		0, 0), (u8)PARP_PRESSURE_MEDIUM);
	KUNIT_EXPECT_EQ(test, parp_effective_tier_pressure_level(4, false,
		0, 0), (u8)PARP_PRESSURE_HIGH);
	KUNIT_EXPECT_EQ(test, parp_effective_tier_pressure_level(2, false,
		0, 0), (u8)PARP_PRESSURE_CRITICAL);
	KUNIT_EXPECT_EQ(test, parp_effective_tier_pressure_level(12, true,
		0, 0), (u8)PARP_PRESSURE_CRITICAL);

	KUNIT_EXPECT_EQ(test, parp_effective_tier_pressure_delta_q8(
		PARP_TIER_SCALE, PARP_PRESSURE_LOW), PARP_TIER_SCALE);
	KUNIT_EXPECT_EQ(test, parp_effective_tier_pressure_delta_q8(
		PARP_TIER_SCALE, PARP_PRESSURE_MEDIUM), 192);
	KUNIT_EXPECT_EQ(test, parp_effective_tier_pressure_delta_q8(
		PARP_TIER_SCALE, PARP_PRESSURE_HIGH), 64);
	KUNIT_EXPECT_EQ(test, parp_effective_tier_pressure_delta_q8(
		PARP_TIER_SCALE, PARP_PRESSURE_CRITICAL), 0);
	KUNIT_EXPECT_EQ(test, parp_effective_tier_pressure_delta_q8(
		-PARP_TIER_SCALE, PARP_PRESSURE_LOW), -128);
	KUNIT_EXPECT_EQ(test, parp_effective_tier_pressure_delta_q8(
		-PARP_TIER_SCALE, PARP_PRESSURE_MEDIUM), -PARP_TIER_SCALE);
	KUNIT_EXPECT_EQ(test, parp_effective_tier_pressure_delta_q8(
		-PARP_TIER_SCALE, PARP_PRESSURE_HIGH), -PARP_TIER_SCALE);
	KUNIT_EXPECT_EQ(test, parp_effective_tier_pressure_delta_q8(
		-PARP_TIER_SCALE, PARP_PRESSURE_CRITICAL), 0);
} /* #lzx */

static void parp_effective_random_matched_test(struct kunit *test)
{
	unsigned long selected_upgrade = 0;
	unsigned long selected_downgrade = 0;
	unsigned long seen;

	for (seen = 0; seen < 100; seen++) {
		if (parp_effective_tier_random_claim(seen * 7919ULL,
			selected_upgrade, 5, seen, 100))
			selected_upgrade++;
		if (parp_effective_tier_random_claim(seen * 104729ULL,
			selected_downgrade, 2, seen, 100))
			selected_downgrade++;
	}
	KUNIT_EXPECT_EQ(test, selected_upgrade, 5UL);
	KUNIT_EXPECT_EQ(test, selected_downgrade, 2UL);
}

static void parp_effective_recency_time_test(struct kunit *test)
{
	bool valid;

	KUNIT_EXPECT_EQ(test, parp_effective_tier_recency_score(100,
		&parp_effective_test_policy), 96);
	KUNIT_EXPECT_EQ(test, parp_effective_tier_recency_score(500,
		&parp_effective_test_policy), 48);
	KUNIT_EXPECT_EQ(test, parp_effective_tier_recency_score(10000,
		&parp_effective_test_policy), -48);
	KUNIT_EXPECT_EQ(test, parp_effective_tier_elapsed_ms(5, U32_MAX - 4,
		&valid), 10U);
	KUNIT_EXPECT_TRUE(test, valid);
	KUNIT_EXPECT_EQ(test, parp_effective_tier_metadata_size(), 24UL);
}

static void parp_effective_candidate_access_trace_gate_test(struct kunit *test)
{
	KUNIT_EXPECT_FALSE(test,
		parp_effective_tier_candidate_access_trace_eligible(false, 1)); /* #lzx */
	KUNIT_EXPECT_FALSE(test,
		parp_effective_tier_candidate_access_trace_eligible(true, 0)); /* #lzx */
	KUNIT_EXPECT_TRUE(test,
		parp_effective_tier_candidate_access_trace_eligible(true, 1)); /* #lzx */
	KUNIT_EXPECT_TRUE(test,
		parp_effective_tier_candidate_access_trace_eligible(true, 63)); /* #lzx */
} /* #lzx */

static void parp_effective_access_lifetime_test(struct kunit *test)
{
	struct parp_tier_state_snapshot before = { };
	struct parp_tier_state_snapshot after = { };
	struct folio *folio;
	struct page *page;
	u64 first_cookie;
	u64 second_cookie;
	u32 last_access;
	int error;

	if (!parp_effective_tier_metadata_ready()) {
		kunit_skip(test, "effective-tier metadata was not boot-reserved");
		return;
	} /* #lzx */

	page = alloc_page(GFP_KERNEL);
	KUNIT_ASSERT_NOT_NULL(test, page);
	folio = page_folio(page);
	parp_effective_tier_page_alloc(page, 0);
	error = parp_effective_tier_set_mode(PARP_EFFECTIVE_TIER_SHADOW);
	if (error) {
		KUNIT_FAIL(test, "cannot enable SHADOW: %d", error);
		__free_page(page);
		return;
	}
	parp_effective_tier_note_access(folio, PARP_ACCESS_PTE_YOUNG);
	KUNIT_EXPECT_TRUE(test,
		parp_effective_tier_state_snapshot(folio, &before));
	last_access = before.last_access_ms;
	first_cookie = parp_effective_tier_cookie(folio);
	KUNIT_EXPECT_NE(test, first_cookie, 0ULL);
	parp_effective_tier_note_move(folio, PARP_POLICY_PROMOTION, 1);
	KUNIT_EXPECT_TRUE(test,
		parp_effective_tier_state_snapshot(folio, &after));
	KUNIT_EXPECT_EQ(test, after.last_access_ms, last_access);
	parp_effective_tier_note_move(folio, PARP_NATIVE_GENERATION_MOVE, 2);
	KUNIT_EXPECT_TRUE(test,
		parp_effective_tier_state_snapshot(folio, &after));
	KUNIT_EXPECT_EQ(test, after.last_access_ms, last_access);
	parp_effective_tier_page_alloc(page, 0);
	second_cookie = parp_effective_tier_cookie(folio);
	KUNIT_EXPECT_NE(test, second_cookie, first_cookie);
	KUNIT_EXPECT_FALSE(test,
		parp_effective_tier_state_snapshot(folio, &after));
	KUNIT_EXPECT_EQ(test,
		parp_effective_tier_set_mode(PARP_EFFECTIVE_TIER_OFF), 0);
	__free_page(page);
}

static void parp_effective_epoch_limit_test(struct kunit *test)
{
	enum parp_tier_bypass_reason bypass;
	struct folio *folio;
	struct page *page;
	int error;

	if (!parp_effective_tier_metadata_ready()) {
		kunit_skip(test, "effective-tier metadata was not boot-reserved");
		return;
	} /* #lzx */

	page = alloc_page(GFP_KERNEL);
	KUNIT_ASSERT_NOT_NULL(test, page);
	folio = page_folio(page);
	parp_effective_tier_page_alloc(page, 0);
	error = parp_effective_tier_set_mode(PARP_EFFECTIVE_TIER_SHADOW);
	if (error) {
		KUNIT_FAIL(test, "cannot enable SHADOW: %d", error);
		__free_page(page);
		return;
	}
	parp_effective_tier_note_access(folio, PARP_ACCESS_FD_REFERENCE);
	KUNIT_EXPECT_TRUE(test, parp_effective_tier_claim_epoch(folio, true,
		7, &bypass));
	KUNIT_EXPECT_FALSE(test, parp_effective_tier_claim_epoch(folio, true,
		7, &bypass));
	KUNIT_EXPECT_EQ(test, bypass, PARP_TIER_BYPASS_REPEAT_UPGRADE);
	KUNIT_EXPECT_TRUE(test, parp_effective_tier_claim_epoch(folio, false,
		7, &bypass));
	KUNIT_EXPECT_FALSE(test, parp_effective_tier_claim_epoch(folio, false,
		7, &bypass));
	KUNIT_EXPECT_EQ(test, bypass, PARP_TIER_BYPASS_REPEAT_DOWNGRADE);
	KUNIT_EXPECT_EQ(test,
		parp_effective_tier_set_mode(PARP_EFFECTIVE_TIER_OFF), 0);
	__free_page(page);
}

static void parp_effective_trace_quadrants_test(struct kunit *test)
{
	struct parp_tier_decision decision;

	parp_effective_tier_classify(0, true, 0, 0, false,
		&parp_effective_test_policy, &decision);
	KUNIT_EXPECT_EQ(test, decision.action, (u8)PARP_TIER_KEEP_RECLAIM);
	parp_effective_tier_classify(96, true, 0, 0, false,
		&parp_effective_test_policy, &decision);
	KUNIT_EXPECT_EQ(test, decision.action,
		(u8)PARP_TIER_PREDICTIVE_UPGRADE);
	parp_effective_tier_classify(0, true, 2, 1, false,
		&parp_effective_test_policy, &decision);
	KUNIT_EXPECT_EQ(test, decision.action, (u8)PARP_TIER_KEEP_PROTECT);
	parp_effective_tier_classify(-48, true, 2, 1, false,
		&parp_effective_test_policy, &decision);
	KUNIT_EXPECT_EQ(test, decision.action,
		(u8)PARP_TIER_PREDICTIVE_DOWNGRADE);
}

static void parp_effective_config_safety_test(struct kunit *test)
{
	KUNIT_EXPECT_FALSE(test, IS_ENABLED(CONFIG_PARP_FRONTIER_SCORE));
	KUNIT_EXPECT_EQ(test, parp_effective_tier_get_mode(),
		PARP_EFFECTIVE_TIER_OFF);
	if (!parp_effective_tier_metadata_ready())
		KUNIT_EXPECT_EQ(test,
				parp_effective_tier_set_mode(PARP_EFFECTIVE_TIER_SHADOW),
				-EOPNOTSUPP); /* #lzx */
	if (!IS_ENABLED(CONFIG_PARP_EFFECTIVE_TIER_EXPERIMENTAL_APPLY))
		KUNIT_EXPECT_EQ(test, parp_effective_tier_set_mode(
			PARP_EFFECTIVE_TIER_BIDIRECTIONAL), -EOPNOTSUPP);
	KUNIT_EXPECT_TRUE(test,
		parp_access_event_is_real(PARP_ACCESS_PTE_YOUNG));
	KUNIT_EXPECT_TRUE(test,
		parp_access_event_is_real(PARP_ACCESS_MARK_ACCESSED));
	KUNIT_EXPECT_TRUE(test,
		parp_access_event_is_real(PARP_ACCESS_FD_REFERENCE));
	KUNIT_EXPECT_FALSE(test,
		parp_access_event_is_real(PARP_NATIVE_TIER_PROMOTION));
	KUNIT_EXPECT_FALSE(test,
		parp_access_event_is_real(PARP_NATIVE_GENERATION_MOVE));
	KUNIT_EXPECT_FALSE(test,
		parp_access_event_is_real(PARP_POLICY_PROMOTION));
}
#endif

#ifdef CONFIG_PARP_FRONTIER_SCORE
static void parp_frontier_quantized_score_test(struct kunit *test)
{
	const s64 lower_edges[PARP_FRONTIER_FEATURES] = {
		10, 10, 8, 10, 0, 0, 10, 4096,
	};
	const s64 above_edges[PARP_FRONTIER_FEATURES] = {
		11, 11, 9, 11, 1, 1, 11, 4097,
	};
	s32 threshold = 0;

	/* Equality belongs to the lower bin in both Python and C. */
	KUNIT_EXPECT_EQ(test,
		parp_frontier_score_values(0, lower_edges, &threshold), 102);
	KUNIT_EXPECT_EQ(test, threshold, 96);
	KUNIT_EXPECT_EQ(test,
		parp_frontier_score_values(99, lower_edges, &threshold), 102);
	KUNIT_EXPECT_EQ(test,
		parp_frontier_score_values(0, above_edges, &threshold), 78);
	KUNIT_EXPECT_EQ(test,
		parp_frontier_score_values(1, lower_edges, &threshold), 110);
	KUNIT_EXPECT_EQ(test, threshold, 94);
}

static void parp_frontier_selection_budget_test(struct kunit *test)
{
	const unsigned long capacities[] = { 10, 20, 30 };
	unsigned long headroom = 0;
	unsigned int frontier = 0;

	KUNIT_EXPECT_TRUE(test, parp_frontier_select(capacities,
		ARRAY_SIZE(capacities), 25, 32767, &frontier, &headroom));
	KUNIT_EXPECT_EQ(test, frontier, 1U);
	KUNIT_EXPECT_EQ(test, headroom, 5UL);
	KUNIT_EXPECT_TRUE(test, parp_frontier_select(capacities,
		ARRAY_SIZE(capacities), 12, 16384, &frontier, &headroom));
	KUNIT_EXPECT_EQ(test, frontier, 1U);
	KUNIT_EXPECT_EQ(test, headroom, 3UL);
	KUNIT_EXPECT_FALSE(test, parp_frontier_select(capacities,
		ARRAY_SIZE(capacities), 100, 32767, &frontier, &headroom));
	KUNIT_EXPECT_EQ(test,
		parp_frontier_budget_min(64, 32, 16, 8), 8UL);
	KUNIT_EXPECT_TRUE(test, parp_frontier_context_valid(99, 100, 7, 7));
	KUNIT_EXPECT_FALSE(test, parp_frontier_context_valid(100, 100, 7, 7));
	KUNIT_EXPECT_FALSE(test, parp_frontier_context_valid(99, 100, 7, 8));
	KUNIT_EXPECT_EQ(test, parp_frontier_set_mode(PARP_FRONTIER_APPLY),
		-EOPNOTSUPP);
}

static void parp_frontier_trace_schema_test(struct kunit *test)
{
	struct parp_frontier_trace event = {
		.source_seq = 7,
		.frontier_seq = 9,
		.folio_pages = 4,
		.would_promote = true,
		.applied = false,
	};

	KUNIT_EXPECT_EQ(test, event.frontier_seq - event.source_seq, 2ULL);
	KUNIT_EXPECT_EQ(test, event.folio_pages, 4ULL);
	KUNIT_EXPECT_TRUE(test, event.would_promote);
	KUNIT_EXPECT_FALSE(test, event.applied);
}
#endif

static struct kunit_case parp_cases[] = {
	KUNIT_CASE(parp_q15_test),
#ifdef CONFIG_PARP_TIER2_WATERMARK
	/* xty-test */
	/* Keep KUnit linkage valid when tier2 watermark is disabled. */
	KUNIT_CASE(parp_tier2_ewma_test),
#endif
	KUNIT_CASE(parp_state_test),
	KUNIT_CASE(parp_predictor_test),
	KUNIT_CASE(parp_metadata_test),
	KUNIT_CASE(parp_region_alignment_test),
	KUNIT_CASE(parp_evidence_window_test),
	KUNIT_CASE(parp_anon_domain_test),
	KUNIT_CASE(parp_side_table_limit_test),
	KUNIT_CASE(parp_fallback_test),
	KUNIT_CASE(parp_snapshot_test),
	KUNIT_CASE(parp_observe_test),
	KUNIT_CASE(parp_memcg_domain_identity_test),
	KUNIT_CASE(parp_scan_budget_gate_matrix_test),
	KUNIT_CASE(parp_scan_budget_monotonic_repeat_test),
	KUNIT_CASE(parp_scan_budget_basic_test),
	KUNIT_CASE(parp_scan_budget_gate_pressure_test),
	KUNIT_CASE(parp_damon_shared_mm_budget_test),
	KUNIT_CASE(parp_scan_budget_bounds_mode_test),
	KUNIT_CASE(parp_scan_budget_apply_domain_test),
	KUNIT_CASE(parp_prior_batch_test),
	KUNIT_CASE(parp_prior_batch_validation_test),
	KUNIT_CASE(parp_scan_budget_circuit_test),
	KUNIT_CASE(parp_phase27_region_trace_schema_test),
#ifdef CONFIG_PARP_EFFECTIVE_TIER
	KUNIT_CASE(parp_effective_score_bins_test),
	KUNIT_CASE(parp_effective_rank_runtime_contract_test),
	KUNIT_CASE(parp_effective_delta_thresholds_test),
	KUNIT_CASE(parp_effective_q8_clamp_test),
	KUNIT_CASE(parp_effective_native_modes_test),
	KUNIT_CASE(parp_effective_native_fallback_test),
	KUNIT_CASE(parp_effective_downgrade_boundary_test),
	KUNIT_CASE(parp_effective_policy_flags_test),
	KUNIT_CASE(parp_effective_budget_test),
	KUNIT_CASE(parp_effective_upgrade_gate_test),
	KUNIT_CASE(parp_effective_tier_pressure_policy_test), /* #lzx */
	KUNIT_CASE(parp_effective_random_matched_test),
	KUNIT_CASE(parp_effective_recency_time_test),
	KUNIT_CASE(parp_effective_candidate_access_trace_gate_test), /* #lzx */
	KUNIT_CASE(parp_effective_access_lifetime_test),
	KUNIT_CASE(parp_effective_epoch_limit_test),
	KUNIT_CASE(parp_effective_trace_quadrants_test),
	KUNIT_CASE(parp_effective_config_safety_test),
#endif
#ifdef CONFIG_PARP_FRONTIER_SCORE
	KUNIT_CASE(parp_frontier_quantized_score_test),
	KUNIT_CASE(parp_frontier_selection_budget_test),
	KUNIT_CASE(parp_frontier_trace_schema_test),
#endif
	{}
};

static struct kunit_suite parp_suite = {
	.name = "parp",
	.test_cases = parp_cases,
};
kunit_test_suite(parp_suite);

MODULE_LICENSE("GPL");
