#include "test.h"

#include <stdio.h>
#include <stdint.h>

struct reclaim_test_case {
    const char *name;
    reclaim_test_fn fn;
};

static struct reclaim_test_case cases[128];
static size_t case_count;
static unsigned failures;

void register_test_intrusive_list_order(void);
void register_test_folio_order_pages(void);
void register_test_page_domain_lifecycle(void);
void register_test_duplicate_ids_and_missing_domain(void);
void register_test_allocation_failure_preserves_state(void);

void reclaim_test_register(const char *name, reclaim_test_fn fn)
{
    if (case_count < sizeof(cases) / sizeof(cases[0])) {
        cases[case_count++] = (struct reclaim_test_case){name, fn};
    }
}

void reclaim_test_fail(const char *file, int line, const char *expr)
{
    (void)fprintf(stderr, "%s:%d assertion failed: %s\n", file, line, expr);
    failures++;
}

int reclaim_test_run_all(void)
{
    size_t i;
    unsigned passed = 0U;
    register_test_intrusive_list_order();
    register_test_folio_order_pages();
    register_test_page_domain_lifecycle();
    register_test_duplicate_ids_and_missing_domain();
    register_test_allocation_failure_preserves_state();
    for (i = 0U; i < case_count; i++) {
        if (cases[i].fn()) {
            passed++;
        }
    }
    (void)fprintf(stderr, "%u/%zu tests passed\n", passed, case_count);
    return failures == 0U && passed == case_count ? 0 : 1;
}

int main(void)
{
    return reclaim_test_run_all();
}
