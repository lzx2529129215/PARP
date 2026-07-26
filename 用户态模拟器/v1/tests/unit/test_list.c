#include "myself_kswapd/types.h"
#include "../test_support/test.h"

static bool test_intrusive_list_order(void)
{
    struct reclaim_list list;
    struct reclaim_list_node first = {0};
    struct reclaim_list_node second = {0};

    reclaim_list_init(&list);
    reclaim_list_push_back(&list, &first);
    reclaim_list_push_back(&list, &second);
    TEST_ASSERT(reclaim_list_front(&list) == &first);
    TEST_ASSERT(reclaim_list_back(&list) == &second);
    TEST_ASSERT_EQ_U64(2U, list.nr_folios);
    reclaim_list_move_back(&list, &first);
    TEST_ASSERT(reclaim_list_front(&list) == &second);
    reclaim_list_remove(&list, &second);
    reclaim_list_remove(&list, &first);
    TEST_ASSERT(reclaim_list_empty(&list));
    return true;
}

void register_test_intrusive_list_order(void)
{
    reclaim_test_register("intrusive list order", test_intrusive_list_order);
}
