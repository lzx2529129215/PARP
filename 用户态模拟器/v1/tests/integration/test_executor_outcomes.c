#include "myself_kswapd/engine.h"
#include "myself_kswapd/executor.h"
#include "../test_support/test.h"

static bool test_executor_outcomes_restore_state(void)
{
    struct reclaim_userspace_platform platform;
    struct reclaim_simulator_executor executor;
    struct reclaim_engine_config config = {
        .default_swappiness = 60U,
        .default_swap_enabled = true,
        .pressure = {.default_priority = 0U, .minimum_priority = 0U,
                     .scan_batch_pages = 8U, .max_reclaim_rounds = 1U},
        .page_hash_buckets = 8U, .domain_hash_buckets = 8U,
    };
    struct reclaim_engine *engine = NULL;
    struct reclaim_result result;
    struct reclaim_domain_stats stats;
    struct reclaim_engine_stats engine_stats;
    const struct reclaim_page *page;

    reclaim_platform_userspace_init(&platform);
    reclaim_simulator_executor_init(&executor);
    TEST_ASSERT(reclaim_engine_create(&platform.platform, &config, reclaim_g1_aging_ops(),
                                      reclaim_simulator_executor_ops(), &executor, &engine) ==
                RECLAIM_OK);
    TEST_ASSERT(reclaim_engine_create_domain(engine, 1U) == RECLAIM_OK);
    TEST_ASSERT(reclaim_engine_add_page(engine, 1U, 1U, RECLAIM_PAGE_FILE, 0U) == RECLAIM_OK);
    TEST_ASSERT(reclaim_engine_set_page_outcome(engine, 1U, RECLAIM_SIM_ACTIVATE) == RECLAIM_OK);
    TEST_ASSERT(reclaim_engine_reclaim_group(engine, 1U, 1U, &result) == RECLAIM_OK);
    page = reclaim_engine_get_page(engine, 1U);
    TEST_ASSERT(page->state == RECLAIM_PAGE_ON_LRU);
    TEST_ASSERT(page->lru_kind == RECLAIM_LRU_ACTIVE_FILE);
    TEST_ASSERT_EQ_U64(1U, result.nr_pages_activated);

    TEST_ASSERT(reclaim_engine_set_page_outcome(engine, 1U, RECLAIM_SIM_UNEVICTABLE) ==
                RECLAIM_OK);
    TEST_ASSERT(reclaim_engine_age_group(engine, 1U) == RECLAIM_OK);
    TEST_ASSERT(reclaim_engine_reclaim_group(engine, 1U, 1U, &result) == RECLAIM_OK);
    page = reclaim_engine_get_page(engine, 1U);
    TEST_ASSERT(page->state == RECLAIM_PAGE_UNEVICTABLE);
    TEST_ASSERT(page->lru_kind == RECLAIM_LRU_NONE);
    TEST_ASSERT(reclaim_engine_get_domain_stats(engine, 1U, &stats) == RECLAIM_OK);
    TEST_ASSERT_EQ_U64(1U, stats.nr_unevictable_folios);
    TEST_ASSERT(reclaim_engine_add_page(engine, 2U, 1U, RECLAIM_PAGE_FILE, 0U) == RECLAIM_OK);
    TEST_ASSERT(reclaim_engine_add_page(engine, 3U, 1U, RECLAIM_PAGE_FILE, 0U) == RECLAIM_OK);
    TEST_ASSERT(reclaim_engine_add_page(engine, 4U, 1U, RECLAIM_PAGE_FILE, 0U) == RECLAIM_OK);
    TEST_ASSERT(reclaim_engine_set_page_outcome(engine, 2U, RECLAIM_SIM_PUTBACK) == RECLAIM_OK);
    TEST_ASSERT(reclaim_engine_set_page_outcome(engine, 3U, RECLAIM_SIM_DIRTY) == RECLAIM_OK);
    TEST_ASSERT(reclaim_engine_set_page_outcome(engine, 4U, RECLAIM_SIM_WRITEBACK) == RECLAIM_OK);
    TEST_ASSERT(reclaim_engine_reclaim_group(engine, 1U, 3U, &result) == RECLAIM_OK);
    TEST_ASSERT(reclaim_engine_get_page(engine, 2U)->lru_kind == RECLAIM_LRU_INACTIVE_FILE);
    TEST_ASSERT(reclaim_engine_get_page(engine, 3U)->lru_kind == RECLAIM_LRU_INACTIVE_FILE);
    TEST_ASSERT(reclaim_engine_get_page(engine, 4U)->lru_kind == RECLAIM_LRU_INACTIVE_FILE);
    reclaim_engine_get_stats(engine, &engine_stats);
    TEST_ASSERT_EQ_U64(0U, engine_stats.nr_busy);
    TEST_ASSERT_EQ_U64(1U, engine_stats.nr_dirty);
    TEST_ASSERT_EQ_U64(1U, engine_stats.nr_writeback);
    reclaim_engine_destroy(engine);
    return true;
}

void register_test_executor_outcomes_restore_state(void)
{
    reclaim_test_register("executor outcomes restore state", test_executor_outcomes_restore_state);
}
