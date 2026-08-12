// SPDX-License-Identifier: GPL-2.0
/* Direct userspace microbenchmark of the fixed-cost C scoring contract. */
#define _GNU_SOURCE
#define PARP_CSCORE_NO_MAIN
#include "cscore.c"

#include <stdbool.h>
#include <string.h>
#include <time.h>

#define BATCH_SIZE 128

static bool shadow_enabled;

static void consume_score(int32_t value)
{
	asm volatile("" : : "r"(value) : "memory");
}

static uint64_t now_ns(void)
{
	struct timespec value;

	if (clock_gettime(CLOCK_MONOTONIC_RAW, &value))
		exit(EXIT_FAILURE);
	return (uint64_t)value.tv_sec * 1000000000ULL + value.tv_nsec;
}

static int compare_u64(const void *left, const void *right)
{
	const uint64_t a = *(const uint64_t *)left;
	const uint64_t b = *(const uint64_t *)right;

	return (a > b) - (a < b);
}

static uint64_t percentile(const uint64_t *samples, size_t count,
			   unsigned int percent)
{
	size_t index = (count - 1) * percent / 100;

	return samples[index];
}

static void measure(const char *name, const struct model *model,
		    const int64_t values[FEATURES], int nr_features,
		    size_t iterations, bool off_branch, bool trailing_comma)
{
	uint64_t *samples = calloc(iterations, sizeof(*samples));
	int64_t working[FEATURES];
	uint64_t total = 0;
	size_t i;

	if (!samples)
		exit(EXIT_FAILURE);
	memcpy(working, values, sizeof(working));
	for (i = 0; i < iterations; i++) {
		uint64_t start = now_ns();

		working[0] = values[0] + (i & 1);
		if (off_branch) {
			if (shadow_enabled)
				consume_score(score_n(model, working, nr_features));
		} else {
			consume_score(score_n(model, working, nr_features));
		}
		samples[i] = now_ns() - start;
		total += samples[i];
	}
	qsort(samples, iterations, sizeof(*samples), compare_u64);
	printf("    \"%s\": {\"features\": %d, \"p50_ns\": %" PRIu64
	       ", \"p95_ns\": %" PRIu64 ", \"p99_ns\": %" PRIu64
	       ", \"max_ns\": %" PRIu64 ", \"mean_ns\": %.3f}%s\n",
	       name, nr_features, percentile(samples, iterations, 50),
	       percentile(samples, iterations, 95),
	       percentile(samples, iterations, 99), samples[iterations - 1],
	       (double)total / iterations, trailing_comma ? "," : "");
	free(samples);
}

static void measure_batch(const struct model *model,
			  const int64_t values[FEATURES], size_t iterations)
{
	uint64_t *samples = calloc(iterations, sizeof(*samples));
	int64_t working[FEATURES];
	uint64_t total = 0;
	size_t i;

	if (!samples)
		exit(EXIT_FAILURE);
	memcpy(working, values, sizeof(working));
	for (i = 0; i < iterations; i++) {
		uint64_t start = now_ns();
		int candidate;

		for (candidate = 0; candidate < BATCH_SIZE; candidate++) {
			working[0] = values[0] + (candidate & 1);
			consume_score(score(model, working));
		}
		samples[i] = now_ns() - start;
		total += samples[i];
	}
	qsort(samples, iterations, sizeof(*samples), compare_u64);
	printf("  },\n  \"batch_128\": {\"p50_ns\": %" PRIu64
	       ", \"p95_ns\": %" PRIu64 ", \"p99_ns\": %" PRIu64
	       ", \"max_ns\": %" PRIu64 ", \"mean_ns\": %.3f}\n",
	       percentile(samples, iterations, 50),
	       percentile(samples, iterations, 95),
	       percentile(samples, iterations, 99), samples[iterations - 1],
	       (double)total / iterations);
	free(samples);
}

int main(int argc, char **argv)
{
	const int64_t values[FEATURES] = {
		50, 200, 128, 300, 1, 2, 700, 20000,
	};
	size_t iterations = 20000;
	size_t batch_iterations;

	if (argc == 2) {
		char *end;
		unsigned long parsed = strtoul(argv[1], &end, 10);

		if (*end || !parsed)
			return EXIT_FAILURE;
		iterations = parsed;
	}
	shadow_enabled = getenv("PARP_BENCH_SHADOW_ENABLE") != NULL;
	printf("{\n  \"iterations\": %zu,\n", iterations);
	printf("  \"clock\": \"CLOCK_MONOTONIC_RAW\",\n");
	printf("  \"measurements\": {\n");
	measure("OFF_BRANCH", &models[0], values, FEATURES, iterations, true,
		true);
	measure("GENERIC_4", &models[0], values, 4, iterations, false, true);
	measure("GENERIC_6", &models[0], values, 6, iterations, false, true);
	measure("GENERIC_8", &models[0], values, 8, iterations, false, true);
	measure("WPS_8", &models[1], values, 8, iterations, false, true);
	measure("QQ_8", &models[2], values, 8, iterations, false, true);
	measure("FILES_8", &models[3], values, 8, iterations, false, false);
	batch_iterations = iterations / 32;
	measure_batch(&models[0], values, batch_iterations ? batch_iterations : 1);
	printf("}\n");
	return EXIT_SUCCESS;
}
