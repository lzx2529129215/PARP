#include "myself_kswapd/engine.h"
#include "myself_kswapd/executor.h"
#include "../test_support/test.h"

static struct reclaim_engine_config test_config(void)
{
    struct reclaim_engine_config config = {
        .default_swappiness = 60U,
        .default_swap_enabled = true,
        .pressure = {
            .default_priority = 3U,
            .minimum_priority = 0U,
            .scan_batch_pages = 4U,
            .max_reclaim_rounds = 4U,
        },
        .page_hash_buckets = 8U,
        .domain_hash_buckets = 8U,
    };
    return config;
}

static bool create_test_engine(struct reclaim_userspace_platform *platform,
                               struct reclaim_simulator_executor *executor,
                               struct reclaim_engine **engine)
{
    struct reclaim_engine_config config = test_config();
    reclaim_platform_userspace_init(platform);
    reclaim_simulator_executor_init(executor);
    TEST_ASSERT(reclaim_engine_create(&platform->platform,
                                      &config,
                                      NULL,
                                      reclaim_simulator_executor_ops(),
                                      executor,
                                      engine) == RECLAIM_OK);
    return true;
}

static bool test_page_domain_lifecycle(void)
{
    struct reclaim_userspace_platform platform;
    struct reclaim_simulator_executor executor;
    struct reclaim_engine *engine = NULL;
    struct reclaim_domain_stats stats;
    const struct reclaim_page *page;

    TEST_ASSERT(create_test_engine(&platform, &executor, &engine));
    TEST_ASSERT(reclaim_engine_create_domain(engine, 42U) == RECLAIM_OK);
    TEST_ASSERT(reclaim_engine_add_page(engine, 7U, 42U, RECLAIM_PAGE_ANON, 2U) == RECLAIM_OK);
    page = reclaim_engine_get_page(engine, 7U);
    TEST_ASSERT(page != NULL);
    TEST_ASSERT(page->state == RECLAIM_PAGE_ON_LRU);
    TEST_ASSERT(page->lru_kind == RECLAIM_LRU_INACTIVE_ANON);
    TEST_ASSERT(page->charge_cgroup_id == 42U);
    TEST_ASSERT(reclaim_engine_get_domain_stats(engine, 42U, &stats) == RECLAIM_OK);
    TEST_ASSERT_EQ_U64(1U, stats.nr_folios);
    TEST_ASSERT_EQ_U64(4U, stats.nr_base_pages);
    TEST_ASSERT(reclaim_engine_destroy_domain(engine, 42U) == RECLAIM_ERR_DOMAIN_NOT_EMPTY);
    TEST_ASSERT(reclaim_engine_remove_page(engine, 7U) == RECLAIM_OK);
    TEST_ASSERT(reclaim_engine_destroy_domain(engine, 42U) == RECLAIM_OK);
    reclaim_engine_destroy(engine);
    TEST_ASSERT_EQ_U64(0U, reclaim_platform_userspace_live_allocations(&platform));
    return true;
}

static bool test_duplicate_ids_and_missing_domain(void)
{
    struct reclaim_userspace_platform platform;
    struct reclaim_simulator_executor executor;
    struct reclaim_engine *engine = NULL;

    TEST_ASSERT(create_test_engine(&platform, &executor, &engine));
    TEST_ASSERT(reclaim_engine_add_page(engine, 1U, 99U, RECLAIM_PAGE_FILE, 0U) ==
                RECLAIM_ERR_DOMAIN_NOT_FOUND);
    TEST_ASSERT(reclaim_engine_create_domain(engine, 99U) == RECLAIM_OK);
    TEST_ASSERT(reclaim_engine_add_page(engine, 1U, 99U, RECLAIM_PAGE_FILE, 0U) == RECLAIM_OK);
    TEST_ASSERT(reclaim_engine_add_page(engine, 1U, 99U, RECLAIM_PAGE_FILE, 0U) ==
                RECLAIM_ERR_PAGE_ALREADY_EXISTS);
    reclaim_engine_destroy(engine);
    return true;
}

static bool test_allocation_failure_preserves_state(void)
{
    struct reclaim_userspace_platform platform;
    struct reclaim_simulator_executor executor;
    struct reclaim_engine *engine = NULL;
    struct reclaim_engine_stats stats;

    TEST_ASSERT(create_test_engine(&platform, &executor, &engine));
    TEST_ASSERT(reclaim_engine_create_domain(engine, 5U) == RECLAIM_OK);
    reclaim_platform_userspace_set_fail_after(&platform, 0L);
    TEST_ASSERT(reclaim_engine_add_page(engine, 5U, 5U, RECLAIM_PAGE_FILE, 0U) ==
                RECLAIM_ERR_NO_MEMORY);
    TEST_ASSERT(reclaim_engine_get_page(engine, 5U) == NULL);
    reclaim_engine_get_stats(engine, &stats);
    TEST_ASSERT_EQ_U64(0U, stats.nr_folios);
    reclaim_engine_destroy(engine);
    TEST_ASSERT_EQ_U64(0U, reclaim_platform_userspace_live_allocations(&platform));
    return true;
}

void register_test_page_domain_lifecycle(void)
{
    reclaim_test_register("page domain lifecycle", test_page_domain_lifecycle);
}

void register_test_duplicate_ids_and_missing_domain(void)
{
    reclaim_test_register("duplicate ids and missing domain", test_duplicate_ids_and_missing_domain);
}

void register_test_allocation_failure_preserves_state(void)
{
    reclaim_test_register("allocation failure preserves state", test_allocation_failure_preserves_state);
}
