#ifndef MYSELF_KSWAPD_EVENT_H
#define MYSELF_KSWAPD_EVENT_H

#include "myself_kswapd/engine.h"
#include "myself_kswapd/validator.h"

#include <stdbool.h>
#include <stddef.h>
#include <stdio.h>

enum reclaim_event_type {
    RECLAIM_EVENT_GROUP_CREATE,
    RECLAIM_EVENT_GROUP_DESTROY,
    RECLAIM_EVENT_GROUP_SET_SWAPPINESS,
    RECLAIM_EVENT_GROUP_SET_SWAP_ENABLED,
    RECLAIM_EVENT_PAGE_ADD,
    RECLAIM_EVENT_PAGE_ACCESS,
    RECLAIM_EVENT_PAGE_REMOVE,
    RECLAIM_EVENT_PAGE_RECHARGE,
    RECLAIM_EVENT_PAGE_MIGRATE,
    RECLAIM_EVENT_PAGE_EXEC_OUTCOME,
    RECLAIM_EVENT_AGE_GROUP,
    RECLAIM_EVENT_AGE_ALL,
    RECLAIM_EVENT_RECLAIM_GROUP,
    RECLAIM_EVENT_RECLAIM_ALL,
    RECLAIM_EVENT_VALIDATE,
    RECLAIM_EVENT_DUMP,
    RECLAIM_EVENT_ASSERT_PAGE_MISSING,
    RECLAIM_EVENT_ASSERT_PAGE_STATE,
    RECLAIM_EVENT_ASSERT_PAGE_LRU,
    RECLAIM_EVENT_ASSERT_DOMAIN_PAGES,
    RECLAIM_EVENT_ASSERT_LAST_STOP_REASON
};

struct reclaim_event {
    enum reclaim_event_type type;
    size_t line_number;
    char raw[256];
    union {
        struct { uint64_t cgroup_id; } group;
        struct { uint64_t cgroup_id; int inherited; uint32_t value; } swappiness;
        struct { uint64_t cgroup_id; int inherited; bool enabled; } swap;
        struct { uint64_t page_id; uint64_t cgroup_id; enum reclaim_page_type type; uint32_t order; } add;
        struct { uint64_t page_id; uint64_t cgroup_id; } page_cgroup;
        struct { uint64_t old_page_id; uint64_t new_page_id; } migrate;
        struct { uint64_t page_id; enum reclaim_sim_outcome outcome; } outcome;
        struct { uint64_t target_pages; } reclaim_all;
        struct { uint64_t cgroup_id; uint64_t target_pages; } reclaim_group;
        struct { uint64_t page_id; enum reclaim_page_state state; } assert_state;
        struct { uint64_t page_id; enum reclaim_lru_kind kind; } assert_lru;
        struct { uint64_t cgroup_id; uint64_t base_pages; } assert_domain;
        struct { enum reclaim_stop_reason reason; } assert_stop;
    } args;
};

struct reclaim_trace_state {
    struct reclaim_result last_result;
    bool has_last_result;
};

typedef void (*reclaim_output_fn)(void *context, const char *line);

int reclaim_event_parse(const char *filename,
                        size_t line_number,
                        const char *text,
                        struct reclaim_event *event,
                        char *error_message,
                        size_t error_message_size);
int reclaim_event_apply(struct reclaim_engine *engine,
                        const struct reclaim_event *event,
                        struct reclaim_trace_state *state);
int reclaim_trace_run(struct reclaim_engine *engine,
                      const char *filename,
                      FILE *input,
                      bool validate_each_event,
                      bool validate_at_end,
                      reclaim_output_fn output,
                      void *output_context,
                      size_t *failed_line);

#endif
