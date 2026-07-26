#ifndef RECLAIM_TEST_SUPPORT_H
#define RECLAIM_TEST_SUPPORT_H

#include <stdbool.h>
#include <stdint.h>

typedef bool (*reclaim_test_fn)(void);

void reclaim_test_register(const char *name, reclaim_test_fn fn);
int reclaim_test_run_all(void);
void reclaim_test_fail(const char *file, int line, const char *expr);

#define TEST_ASSERT(expr) \
    do { \
        if (!(expr)) { \
            reclaim_test_fail(__FILE__, __LINE__, #expr); \
            return false; \
        } \
    } while (0)

#define TEST_ASSERT_EQ_U64(expected, actual) \
    do { \
        uint64_t test_expected_value = (expected); \
        uint64_t test_actual_value = (actual); \
        if (test_expected_value != test_actual_value) { \
            reclaim_test_fail(__FILE__, __LINE__, #expected " == " #actual); \
            return false; \
        } \
    } while (0)

#endif
