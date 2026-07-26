#include "myself_kswapd/types.h"
#include "../test_support/test.h"

static bool test_folio_order_pages(void)
{
    uint64_t pages = 0U;
    TEST_ASSERT(reclaim_folio_base_pages(0U, &pages) == 0);
    TEST_ASSERT_EQ_U64(1U, pages);
    TEST_ASSERT(reclaim_folio_base_pages(4U, &pages) == 0);
    TEST_ASSERT_EQ_U64(16U, pages);
    TEST_ASSERT(reclaim_folio_base_pages(64U, &pages) != 0);
    return true;
}

void register_test_folio_order_pages(void)
{
    reclaim_test_register("folio order accounting", test_folio_order_pages);
}
