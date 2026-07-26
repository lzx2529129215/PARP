#include "myself_kswapd/event.h"
#include "myself_kswapd/executor.h"

#include <stdio.h>
#include <string.h>

static void stdout_line(void *context, const char *line)
{
    FILE *stream = context;
    (void)fprintf(stream, "%s\n", line);
}

int main(int argc, char **argv)
{
    const char *filename = "<stdin>";
    FILE *input = stdin;
    bool validate_each = true;
    bool validate_end = true;
    struct reclaim_userspace_platform platform;
    struct reclaim_simulator_executor executor;
    struct reclaim_engine_config config = {
        .default_swappiness = 60U,
        .default_swap_enabled = true,
        .pressure = {.default_priority = 12U, .minimum_priority = 0U,
                     .scan_batch_pages = 32U, .max_reclaim_rounds = 13U},
        .page_hash_buckets = 64U,
        .domain_hash_buckets = 64U,
    };
    struct reclaim_engine *engine = NULL;
    size_t i;
    size_t failed_line = 0U;
    int error;

    for (i = 1U; i < (size_t)argc; i++) {
        if (strcmp(argv[i], "--validate-each-event") == 0) {
            validate_each = true;
        } else if (strcmp(argv[i], "--validate-at-end") == 0) {
            validate_end = true;
        } else if (strcmp(argv[i], "--no-validate") == 0) {
            validate_each = false;
            validate_end = false;
        } else if (argv[i][0] == '-') {
            (void)fprintf(stderr, "unknown option: %s\n", argv[i]);
            return 2;
        } else if (filename[0] != '<') {
            (void)fprintf(stderr, "only one trace file is supported\n");
            return 2;
        } else {
            filename = argv[i];
            input = fopen(filename, "r");
            if (input == NULL) {
                (void)fprintf(stderr, "cannot open trace: %s\n", filename);
                return 2;
            }
        }
    }
    reclaim_platform_userspace_init(&platform);
    reclaim_simulator_executor_init(&executor);
    error = reclaim_engine_create(&platform.platform, &config, reclaim_g1_aging_ops(),
                                  reclaim_simulator_executor_ops(), &executor, &engine);
    if (error != RECLAIM_OK) {
        if (input != stdin) (void)fclose(input);
        (void)fprintf(stderr, "engine create failed: %s\n", reclaim_error_name(error));
        return 1;
    }
    error = reclaim_trace_run(engine, filename, input, validate_each, validate_end,
                              stdout_line, stdout, &failed_line);
    if (input != stdin) (void)fclose(input);
    reclaim_engine_destroy(engine);
    return error == RECLAIM_OK ? 0 : 1;
}
