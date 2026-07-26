#include "myself_kswapd/engine.h"
#include "myself_kswapd/executor.h"
#include "myself_kswapd/policy.h"
#include "../test_support/test.h"

static struct reclaim_engine_config policy_config(void)
{
    return (struct reclaim_engine_config){
        .default_swappiness = 60U,
        .default_swap_enabled = true,
        .pressure = {.default_priority = 2U,
                     .minimum_priority = 0U,
                     .scan_batch_pages = 4U,
                     .max_reclaim_rounds = 3U},
        .page_hash_buckets = 8U,
        .domain_hash_buckets = 8U,
    };
}

static bool test_scan_pressure_and_budget(void)
{
    uint64_t anon;
    uint64_t file;

    TEST_ASSERT_EQ_U64(4U, reclaim_scan_pages(32U, 3U));
    TEST_ASSERT_EQ_U64(1U, reclaim_scan_pages(1U, 3U));
    reclaim_split_scan_budget(100U, 0U, 1, 100U, 100U, &anon, &file);
    TEST_ASSERT_EQ_U64(0U, anon);
    TEST_ASSERT_EQ_U64(100U, file);
    reclaim_split_scan_budget(100U, 200U, 1, 100U, 100U, &anon, &file);
    TEST_ASSERT_EQ_U64(100U, anon);
    TEST_ASSERT_EQ_U64(0U, file);
    reclaim_split_scan_budget(100U, 60U, 0, 100U, 100U, &anon, &file);
    TEST_ASSERT_EQ_U64(0U, anon);
    TEST_ASSERT_EQ_U64(100U, file);
    reclaim_split_scan_budget(100U, 60U, 1, 2U, 100U, &anon, &file);
    TEST_ASSERT_EQ_U64(2U, anon);
    TEST_ASSERT_EQ_U64(98U, file);
    return true;
}

static bool test_access_aging_and_scope(void)
{
    struct reclaim_userspace_platform platform;
    struct reclaim_simulator_executor executor;
    struct reclaim_engine_config config = policy_config();
    struct reclaim_engine *engine = NULL;
    const struct reclaim_page *page;

    reclaim_platform_userspace_init(&platform);
    reclaim_simulator_executor_init(&executor);
    TEST_ASSERT(reclaim_engine_create(&platform.platform, &config, reclaim_g1_aging_ops(),
                                      reclaim_simulator_executor_ops(), &executor, &engine) ==
                RECLAIM_OK);
    TEST_ASSERT(reclaim_engine_create_domain(engine, 1U) == RECLAIM_OK);
    TEST_ASSERT(reclaim_engine_create_domain(engine, 2U) == RECLAIM_OK);
    TEST_ASSERT(reclaim_engine_add_page(engine, 10U, 1U, RECLAIM_PAGE_ANON, 0U) == RECLAIM_OK);
    TEST_ASSERT(reclaim_engine_add_page(engine, 20U, 2U, RECLAIM_PAGE_FILE, 0U) == RECLAIM_OK);
    TEST_ASSERT(reclaim_engine_access_page(engine, 10U, 2U) == RECLAIM_OK);
    page = reclaim_engine_get_page(engine, 10U);
    TEST_ASSERT(page->charge_cgroup_id == 1U);
    TEST_ASSERT(page->last_access_cgroup_id == 2U);
    TEST_ASSERT(page->shared);
    TEST_ASSERT(page->lru_kind == RECLAIM_LRU_INACTIVE_ANON);
    TEST_ASSERT(reclaim_engine_age_group(engine, 1U) == RECLAIM_OK);
    page = reclaim_engine_get_page(engine, 10U);
    TEST_ASSERT(page->lru_kind == RECLAIM_LRU_ACTIVE_ANON);
    TEST_ASSERT(!page->referenced);
    TEST_ASSERT(reclaim_engine_age_group(engine, 1U) == RECLAIM_OK);
    page = reclaim_engine_get_page(engine, 10U);
    TEST_ASSERT(page->lru_kind == RECLAIM_LRU_INACTIVE_ANON);
    page = reclaim_engine_get_page(engine, 20U);
    TEST_ASSERT(page->lru_kind == RECLAIM_LRU_INACTIVE_FILE);
    reclaim_engine_destroy(engine);
    return true;
}

void register_test_scan_pressure_and_budget(void)
{
    reclaim_test_register("scan pressure and budget", test_scan_pressure_and_budget);
}

void register_test_access_aging_and_scope(void)
{
    reclaim_test_register("access aging and scope", test_access_aging_and_scope);
}
