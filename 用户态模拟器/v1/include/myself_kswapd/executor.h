#ifndef MYSELF_KSWAPD_EXECUTOR_H
#define MYSELF_KSWAPD_EXECUTOR_H

#include "myself_kswapd/types.h"

#include <stddef.h>
#include <stdint.h>

struct reclaim_candidate {
    struct reclaim_page *page;
    enum reclaim_lru_kind source_lru;
    enum reclaim_sim_outcome outcome;
};

struct reclaim_candidate_batch {
    struct reclaim_candidate *items;
    size_t count;
    size_t capacity;
};

struct reclaim_exec_result {
    int error;
    uint64_t nr_requested;
    uint64_t nr_isolated;
    uint64_t nr_reclaimed;
    uint64_t nr_putback;
    uint64_t nr_activated;
    uint64_t nr_dirty;
    uint64_t nr_writeback;
    uint64_t nr_unevictable;
    uint64_t nr_busy;
};

struct reclaim_executor_ops {
    int (*execute_batch)(void *context,
                         struct reclaim_candidate_batch *batch,
                         struct reclaim_exec_result *result);
};

struct reclaim_simulator_executor {
    struct reclaim_executor_ops ops;
    enum reclaim_sim_outcome injected_outcome;
    uint64_t injected_page_id;
    int injected_error;
};

void reclaim_simulator_executor_init(struct reclaim_simulator_executor *executor);
void reclaim_simulator_executor_inject(struct reclaim_simulator_executor *executor,
                                       uint64_t page_id,
                                       enum reclaim_sim_outcome outcome);
void reclaim_simulator_executor_fail_next(struct reclaim_simulator_executor *executor,
                                          int error);
const struct reclaim_executor_ops *reclaim_simulator_executor_ops(void);

#endif
