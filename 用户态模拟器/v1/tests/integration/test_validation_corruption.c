#include "myself_kswapd/engine.h"
#include "myself_kswapd/executor.h"
#include "myself_kswapd/validator.h"
#include "../../src/core/internal.h"
#include "../test_support/test.h"

static bool test_validator_detects_corruption(void)
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
    struct reclaim_validation_report report;
    struct reclaim_page *page;
    struct reclaim_domain *domain;
    uint64_t saved_pages;
    void *saved_list;

    reclaim_platform_userspace_init(&platform);
    reclaim_simulator_executor_init(&executor);
    TEST_ASSERT(reclaim_engine_create(&platform.platform, &config, reclaim_g1_aging_ops(),
                                      reclaim_simulator_executor_ops(), &executor, &engine) ==
                RECLAIM_OK);
    TEST_ASSERT(reclaim_engine_create_domain(engine, 1U) == RECLAIM_OK);
    TEST_ASSERT(reclaim_engine_add_page(engine, 1U, 1U, RECLAIM_PAGE_ANON, 0U) == RECLAIM_OK);
    TEST_ASSERT(reclaim_engine_validate(engine, &report) == RECLAIM_OK);
    page = (struct reclaim_page *)reclaim_engine_get_page(engine, 1U);
    domain = reclaim_find_domain(engine, 1U);
    saved_list = page->lru_node.list;
    page->lru_node.list = &domain->active_anon;
    TEST_ASSERT(reclaim_engine_validate(engine, &report) != RECLAIM_OK);
    page->lru_node.list = saved_list;
    saved_pages = domain->stats.nr_base_pages;
    domain->stats.nr_base_pages++;
    TEST_ASSERT(reclaim_engine_validate(engine, &report) != RECLAIM_OK);
    domain->stats.nr_base_pages = saved_pages;
    page->state = RECLAIM_PAGE_ISOLATED;
    TEST_ASSERT(reclaim_engine_validate(engine, &report) != RECLAIM_OK);
    reclaim_engine_destroy(engine);
    return true;
}

void register_test_validator_detects_corruption(void)
{
    reclaim_test_register("validator detects corruption", test_validator_detects_corruption);
}
