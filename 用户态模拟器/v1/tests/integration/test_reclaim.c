#include "myself_kswapd/engine.h"
#include "myself_kswapd/executor.h"
#include "../test_support/test.h"

static struct reclaim_engine_config reclaim_test_config(void)
{
    return (struct reclaim_engine_config){
        .default_swappiness = 60U,
        .default_swap_enabled = true,
        .pressure = {.default_priority = 2U,
                     .minimum_priority = 0U,
                     .scan_batch_pages = 8U,
                     .max_reclaim_rounds = 3U},
        .page_hash_buckets = 8U,
        .domain_hash_buckets = 8U,
    };
}

static bool new_reclaim_engine(struct reclaim_userspace_platform *platform,
                               struct reclaim_simulator_executor *executor,
                               struct reclaim_engine **engine)
{
    struct reclaim_engine_config config = reclaim_test_config();
    reclaim_platform_userspace_init(platform);
    reclaim_simulator_executor_init(executor);
    TEST_ASSERT(reclaim_engine_create(&platform->platform, &config, reclaim_g1_aging_ops(),
                                      reclaim_simulator_executor_ops(), executor, engine) ==
                RECLAIM_OK);
    TEST_ASSERT(reclaim_engine_create_domain(*engine, 1U) == RECLAIM_OK);
    TEST_ASSERT(reclaim_engine_create_domain(*engine, 2U) == RECLAIM_OK);
    return true;
}

static bool test_directed_reclaim_and_overshoot(void)
{
    struct reclaim_userspace_platform platform;
    struct reclaim_simulator_executor executor;
    struct reclaim_engine *engine = NULL;
    struct reclaim_result result;

    TEST_ASSERT(new_reclaim_engine(&platform, &executor, &engine));
    TEST_ASSERT(reclaim_engine_add_page(engine, 10U, 1U, RECLAIM_PAGE_FILE, 2U) == RECLAIM_OK);
    TEST_ASSERT(reclaim_engine_add_page(engine, 20U, 2U, RECLAIM_PAGE_FILE, 0U) == RECLAIM_OK);
    TEST_ASSERT(reclaim_engine_reclaim_group(engine, 1U, 1U, &result) == RECLAIM_OK);
    TEST_ASSERT(result.stop_reason == RECLAIM_STOP_TARGET_REACHED);
    TEST_ASSERT_EQ_U64(1U, result.nr_folios_scanned);
    TEST_ASSERT_EQ_U64(4U, result.nr_pages_reclaimed);
    TEST_ASSERT_EQ_U64(3U, result.nr_overshoot_pages);
    TEST_ASSERT(reclaim_engine_get_page(engine, 10U) == NULL);
    TEST_ASSERT(reclaim_engine_get_page(engine, 20U) != NULL);
    reclaim_engine_destroy(engine);
    return true;
}

static bool test_all_busy_stops_without_isolated_pages(void)
{
    struct reclaim_userspace_platform platform;
    struct reclaim_simulator_executor executor;
    struct reclaim_engine *engine = NULL;
    struct reclaim_result result;
    const struct reclaim_page *page;

    TEST_ASSERT(new_reclaim_engine(&platform, &executor, &engine));
    TEST_ASSERT(reclaim_engine_add_page(engine, 1U, 1U, RECLAIM_PAGE_ANON, 0U) == RECLAIM_OK);
    TEST_ASSERT(reclaim_engine_set_page_outcome(engine, 1U, RECLAIM_SIM_BUSY) == RECLAIM_OK);
    TEST_ASSERT(reclaim_engine_reclaim_group(engine, 1U, 1U, &result) == RECLAIM_OK);
    TEST_ASSERT(result.stop_reason == RECLAIM_STOP_NO_PROGRESS);
    page = reclaim_engine_get_page(engine, 1U);
    TEST_ASSERT(page != NULL);
    TEST_ASSERT(page->state == RECLAIM_PAGE_ON_LRU);
    TEST_ASSERT(page->lru_kind == RECLAIM_LRU_INACTIVE_ANON);
    reclaim_engine_destroy(engine);
    return true;
}

void register_test_directed_reclaim_and_overshoot(void)
{
    reclaim_test_register("directed reclaim and overshoot", test_directed_reclaim_and_overshoot);
}

void register_test_all_busy_stops_without_isolated_pages(void)
{
    reclaim_test_register("all busy stops without isolated pages",
                          test_all_busy_stops_without_isolated_pages);
}
