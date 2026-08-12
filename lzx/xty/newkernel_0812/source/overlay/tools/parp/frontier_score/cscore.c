// SPDX-License-Identifier: GPL-2.0
/* Standalone fixed-cost C oracle; the kernel implementation uses this contract. */
#include <inttypes.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>

#define FEATURES 8
#define BINS 6

struct model {
	int32_t threshold;
	int64_t edges[FEATURES][BINS - 1];
	int16_t weights[FEATURES][BINS];
};

#define COMMON_EDGES { \
	{ 10, 100, 500, 2000, 10000 }, \
	{ 10, 100, 500, 2000, 10000 }, \
	{ 8, 32, 96, 160, 224 }, \
	{ 10, 100, 500, 2000, 10000 }, \
	{ 0, 1, 2, 4, 8 }, \
	{ 0, 1, 2, 3, 5 }, \
	{ 10, 100, 500, 2000, 10000 }, \
	{ 4096, 8192, 16384, 24576, 30000 } \
}

static const struct model models[] = {
	{
		.threshold = 96,
		.edges = COMMON_EDGES,
		.weights = {
			{ 38, 29, 18, 7, -9, -21 },
			{ 24, 18, 11, 3, -8, -17 },
			{ -18, -7, 3, 15, 26, 37 },
			{ 22, 16, 9, 1, -9, -19 },
			{ 22, 13, 5, -5, -15, -25 },
			{ 19, 11, 3, -5, -14, -23 },
			{ 17, 11, 5, -2, -10, -19 },
			{ -22, -13, -4, 8, 21, 34 },
		},
	},
	{
		.threshold = 94,
		.edges = COMMON_EDGES,
		.weights = {
			{ 42, 32, 20, 8, -8, -22 },
			{ 26, 20, 12, 4, -8, -18 },
			{ -20, -8, 4, 16, 28, 40 },
			{ 24, 18, 10, 2, -8, -18 },
			{ 24, 14, 6, -4, -14, -26 },
			{ 20, 12, 4, -4, -12, -20 },
			{ 18, 12, 6, 0, -8, -16 },
			{ -24, -14, -4, 8, 20, 32 },
		},
	},
	{
		.threshold = 100,
		.edges = COMMON_EDGES,
		.weights = {
			{ 36, 28, 18, 8, -6, -18 },
			{ 20, 16, 10, 4, -6, -14 },
			{ -16, -6, 4, 14, 24, 34 },
			{ 18, 14, 8, 2, -8, -16 },
			{ 20, 12, 4, -4, -12, -22 },
			{ 18, 10, 2, -6, -14, -22 },
			{ 16, 10, 4, -2, -10, -18 },
			{ -30, -18, -6, 10, 26, 42 },
		},
	},
	{
		.threshold = 88,
		.edges = COMMON_EDGES,
		.weights = {
			{ 46, 34, 20, 6, -12, -28 },
			{ 30, 22, 12, 2, -10, -22 },
			{ -22, -10, 2, 16, 30, 44 },
			{ 28, 20, 10, 0, -12, -24 },
			{ 26, 16, 6, -6, -18, -30 },
			{ 22, 12, 2, -8, -18, -28 },
			{ 20, 12, 4, -4, -14, -24 },
			{ -18, -10, -2, 6, 16, 26 },
		},
	},
};

static int32_t score_n(const struct model *model,
		       const int64_t values[FEATURES], int nr_features)
{
	int32_t total = 0;
	int feature;

	for (feature = 0; feature < nr_features; feature++) {
		int bin = 0;

		while (bin < BINS - 1 && values[feature] > model->edges[feature][bin])
			bin++;
		total += model->weights[feature][bin];
	}
	return total;
}

static int32_t score(const struct model *model, const int64_t values[FEATURES])
{
	return score_n(model, values, FEATURES);
}

#ifndef PARP_CSCORE_NO_MAIN
int main(void)
{
	int app_id;
	int64_t values[FEATURES];

	while (scanf("%d", &app_id) == 1) {
		int feature;
		int32_t value;

		for (feature = 0; feature < FEATURES; feature++) {
			if (scanf("%" SCNd64, &values[feature]) != 1)
				return EXIT_FAILURE;
		}
		if (app_id < 0 || app_id > 3)
			app_id = 0;
		value = score(&models[app_id], values);
		printf("%" PRId32 " %d\n", value,
		       value >= models[app_id].threshold);
	}
	return ferror(stdin) ? EXIT_FAILURE : EXIT_SUCCESS;
}
#endif
