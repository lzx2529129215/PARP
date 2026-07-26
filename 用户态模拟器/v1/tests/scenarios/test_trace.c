#include "myself_kswapd/event.h"
#include "myself_kswapd/executor.h"
#include "../test_support/test.h"

static bool test_event_parser_and_apply(void)
{
    struct reclaim_event event;
    struct reclaim_trace_state state = {0};
    char error[256];
    struct reclaim_userspace_platform platform;
    struct reclaim_simulator_executor executor;
    struct reclaim_engine_config config = {
        .default_swappiness = 60U, .default_swap_enabled = true,
        .pressure = {.default_priority = 0U, .minimum_priority = 0U,
                     .scan_batch_pages = 8U, .max_reclaim_rounds = 1U},
        .page_hash_buckets = 8U, .domain_hash_buckets = 8U,
    };
    struct reclaim_engine *engine = NULL;

    TEST_ASSERT(reclaim_event_parse("trace", 1U, "GROUP_CREATE 7", &event, error,
                                   sizeof(error)) == RECLAIM_OK);
    reclaim_platform_userspace_init(&platform);
    reclaim_simulator_executor_init(&executor);
    TEST_ASSERT(reclaim_engine_create(&platform.platform, &config, reclaim_g1_aging_ops(),
                                      reclaim_simulator_executor_ops(), &executor, &engine) ==
                RECLAIM_OK);
    TEST_ASSERT(reclaim_event_apply(engine, &event, &state) == RECLAIM_OK);
    TEST_ASSERT(reclaim_event_parse("trace", 2U, "PAGE_ADD 9 7 FILE 1", &event, error,
                                   sizeof(error)) == RECLAIM_OK);
    TEST_ASSERT(reclaim_event_apply(engine, &event, &state) == RECLAIM_OK);
    TEST_ASSERT(reclaim_engine_get_page(engine, 9U) != NULL);
    TEST_ASSERT(reclaim_event_parse("trace", 3U, "GROUP_SET_SWAP 7 OFF", &event, error,
                                   sizeof(error)) == RECLAIM_OK);
    TEST_ASSERT(reclaim_event_apply(engine, &event, &state) == RECLAIM_OK);
    TEST_ASSERT(reclaim_event_parse("trace", 4U, "PAGE_ADD bad", &event, error,
                                   sizeof(error)) == RECLAIM_ERR_PARSE);
    TEST_ASSERT(error[0] != '\0');
    reclaim_engine_destroy(engine);
    return true;
}

void register_test_event_parser_and_apply(void)
{
    reclaim_test_register("event parser and apply", test_event_parser_and_apply);
}
