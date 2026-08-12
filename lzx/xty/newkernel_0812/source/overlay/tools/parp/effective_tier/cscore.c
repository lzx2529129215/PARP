// SPDX-License-Identifier: GPL-2.0
/* Standalone C oracle for the PARP global score and Q8 effective-tier gate. */
#include <inttypes.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>

#define FEATURES 6
#define BINS 6
#define TIER_SCALE 256
#define MAX_TIER 3
#define COLD_THRESHOLD (-48)
#define HOT_THRESHOLD_1 48
#define HOT_THRESHOLD_2 96
#define HOT_THRESHOLD_3 144
#define DEFAULT_MAX_UPGRADE_TIERS 2

struct global_model {
	int32_t bias;
	int64_t edges[FEATURES][BINS - 1];
	int16_t weights[FEATURES][BINS];
};

static const struct global_model model = {
	.bias = 0,
	.edges = {
		{ 10, 100, 500, 2000, 10000 },
		{ 10, 100, 500, 2000, 10000 },
		{ 10, 100, 500, 2000, 10000 },
		{ 0, 1, 2, 4, 8 },
		{ 10, 100, 500, 2000, 10000 },
		{ 8, 32, 96, 160, 224 },
	},
	.weights = {
		{ 64, 48, 24, 0, -32, -64 },
		{ 32, 24, 12, 0, -12, -24 },
		{ 32, 24, 12, 0, -12, -24 },
		{ 24, 12, 0, -12, -24, -36 },
		{ 16, 12, 8, 0, -8, -16 },
		{ -24, -12, 0, 12, 24, 36 },
	},
};

static bool score_global(const int64_t values[FEATURES], int32_t *result)
{
	int64_t total = model.bias;
	int feature;

	for (feature = 0; feature < FEATURES; feature++) {
		int bin = 0;

		if (values[feature] == INT64_MIN)
			return false;
		while (bin < BINS - 1 &&
		       values[feature] > model.edges[feature][bin])
			bin++;
		total += model.weights[feature][bin];
	}
	if (total < INT32_MIN || total > INT32_MAX)
		return false;
	*result = (int32_t)total;
	return true;
}

static bool score_to_delta_q8(int64_t score, int max_upgrade_tiers,
			      int32_t *result)
{
	if (score < INT32_MIN || score > INT32_MAX ||
	    max_upgrade_tiers < 1 || max_upgrade_tiers > 3)
		return false;
	if (score <= COLD_THRESHOLD)
		*result = -TIER_SCALE;
	else if (score >= HOT_THRESHOLD_3 && max_upgrade_tiers == 3)
		*result = 3 * TIER_SCALE;
	else if (score >= HOT_THRESHOLD_2)
		*result = (max_upgrade_tiers < 2 ? max_upgrade_tiers : 2) *
			TIER_SCALE;
	else if (score >= HOT_THRESHOLD_1)
		*result = TIER_SCALE;
	else
		*result = 0;
	return true;
}

static bool tier_gate(int native_tier, int tier_idx, int64_t delta_q8,
		      int32_t *effective_q8, bool *native_protect,
		      bool *effective_protect)
{
	int64_t value;

	if (native_tier < 0 || native_tier > MAX_TIER ||
	    tier_idx < 0 || tier_idx > MAX_TIER)
		return false;
	value = (int64_t)native_tier * TIER_SCALE + delta_q8;
	if (value < 0)
		value = 0;
	else if (value > MAX_TIER * TIER_SCALE)
		value = MAX_TIER * TIER_SCALE;
	*effective_q8 = (int32_t)value;
	*native_protect = native_tier > tier_idx;
	*effective_protect = value > (int64_t)tier_idx * TIER_SCALE;
	return true;
}

static bool add_base_pages(uint64_t total, uint64_t pages, uint64_t *result)
{
	if (!pages || total > UINT64_MAX - pages)
		return false;
	*result = total + pages;
	return true;
}

static uint32_t elapsed_u32(int64_t now, int64_t then)
{
	return (uint32_t)now - (uint32_t)then;
}

static int run_score(void)
{
	int64_t values[FEATURES];
	int32_t score = 0;
	int feature;
	bool valid;

	for (feature = 0; feature < FEATURES; feature++) {
		if (scanf("%" SCNd64, &values[feature]) != 1)
			return EXIT_FAILURE;
	}
	valid = score_global(values, &score);
	printf("S %d %" PRId32 "\n", valid, valid ? score : 0);
	return EXIT_SUCCESS;
}

static int run_delta(void)
{
	int64_t score;
	int max_upgrade_tiers;
	int32_t delta = 0;
	bool valid;

	if (scanf("%" SCNd64 " %d", &score, &max_upgrade_tiers) != 2)
		return EXIT_FAILURE;
	valid = score_to_delta_q8(score, max_upgrade_tiers, &delta);
	printf("D %d %" PRId32 "\n", valid, valid ? delta : 0);
	return EXIT_SUCCESS;
}

static int run_tier(void)
{
	int native_tier;
	int tier_idx;
	int64_t delta;
	int32_t effective = 0;
	bool native_protect = false;
	bool effective_protect = false;
	bool valid;

	if (scanf("%d %d %" SCNd64, &native_tier, &tier_idx, &delta) != 3)
		return EXIT_FAILURE;
	valid = tier_gate(native_tier, tier_idx, delta, &effective,
			  &native_protect, &effective_protect);
	printf("T %d %" PRId32 " %d %d\n", valid,
	       valid ? effective : 0, valid ? native_protect : 0,
	       valid ? effective_protect : 0);
	return EXIT_SUCCESS;
}

static int run_time(void)
{
	int64_t now;
	int64_t then;

	if (scanf("%" SCNd64 " %" SCNd64, &now, &then) != 2)
		return EXIT_FAILURE;
	printf("U %" PRIu32 "\n", elapsed_u32(now, then));
	return EXIT_SUCCESS;
}

static int run_base_pages(void)
{
	uint64_t total;
	uint64_t pages;
	uint64_t result = 0;
	bool valid;

	if (scanf("%" SCNu64 " %" SCNu64, &total, &pages) != 2)
		return EXIT_FAILURE;
	valid = add_base_pages(total, pages, &result);
	printf("B %d %" PRIu64 "\n", valid, valid ? result : 0);
	return EXIT_SUCCESS;
}

int main(void)
{
	char operation;

	while (scanf(" %c", &operation) == 1) {
		int status;

		switch (operation) {
		case 'S':
			status = run_score();
			break;
		case 'D':
			status = run_delta();
			break;
		case 'T':
			status = run_tier();
			break;
		case 'U':
			status = run_time();
			break;
		case 'B':
			status = run_base_pages();
			break;
		default:
			return EXIT_FAILURE;
		}
		if (status != EXIT_SUCCESS)
			return status;
	}
	return ferror(stdin) ? EXIT_FAILURE : EXIT_SUCCESS;
}
