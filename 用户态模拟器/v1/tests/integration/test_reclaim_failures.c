#include "myself_kswapd/engine.h"
#include "myself_kswapd/executor.h"
#include "../test_support/test.h"

static bool test_executor_error_puts_back_batch(void)
{
    struct reclaim_userspace_platform platform;
    struct reclaim_simulator_executor executor;
    struct reclaim_engine_config config = {
        .default_swappiness = 60U, .default_swap_enabled = true,
        .pressure = {.default_priority = 0U, .minimum_priority = 0U,
                     .scan_batch_pages = 8U, .max_reclaim_rounds = 1U},
        .page_hash_buckets = 8U, .domain_hash_buckets = 8U,
    };
    struct reclaim_engine *engine = NULL;
    struct reclaim_result result;
    const struct reclaim_page *page;

    reclaim_platform_userspace_init(&platform);
    reclaim_simulator_executor_init(&executor);
    TEST_ASSERT(reclaim_engine_create(&platform.platform, &config, reclaim_g1_aging_ops(),
                                      reclaim_simulator_executor_ops(), &executor, &engine) ==
                RECLAIM_OK);
    TEST_ASSERT(reclaim_engine_create_domain(engine, 1U) == RECLAIM_OK);
    TEST_ASSERT(reclaim_engine_add_page(engine, 1U, 1U, RECLAIM_PAGE_ANON, 0U) == RECLAIM_OK);
    TEST_ASSERT(reclaim_engine_add_page(engine, 2U, 1U, RECLAIM_PAGE_FILE, 0U) == RECLAIM_OK);
    reclaim_simulator_executor_fail_next(&executor, -7);
    TEST_ASSERT(reclaim_engine_reclaim_group(engine, 1U, 2U, &result) == RECLAIM_ERR_EXECUTOR);
    page = reclaim_engine_get_page(engine, 1U);
    TEST_ASSERT(page->state == RECLAIM_PAGE_ON_LRU);
    page = reclaim_engine_get_page(engine, 2U);
    TEST_ASSERT(page->state == RECLAIM_PAGE_ON_LRU);
    reclaim_engine_destroy(engine);
    return true;
}

void register_test_executor_error_puts_back_batch(void)
{
    reclaim_test_register("executor error puts back batch", test_executor_error_puts_back_batch);
}
