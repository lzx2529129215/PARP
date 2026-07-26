#include "myself_kswapd/executor.h"

static int simulator_execute_batch(void *context,
                                   struct reclaim_candidate_batch *batch,
                                   struct reclaim_exec_result *result)
{
    struct reclaim_simulator_executor *executor = context;
    size_t i;

    if (executor == NULL || batch == NULL || result == NULL) return -1;
    *result = (struct reclaim_exec_result){.nr_requested = batch->count,
                                           .nr_isolated = batch->count};
    if (executor->injected_error != 0) {
        result->error = executor->injected_error;
        executor->injected_error = 0;
        return result->error;
    }
    for (i = 0U; i < batch->count; i++) {
        struct reclaim_page *page = batch->items[i].page;
        enum reclaim_sim_outcome outcome = page->next_sim_outcome;
        if (executor->injected_page_id == page->page_id) {
            outcome = executor->injected_outcome;
            executor->injected_page_id = UINT64_MAX;
        }
        batch->items[i].outcome = outcome;
        page->next_sim_outcome = RECLAIM_SIM_SUCCESS;
        switch (outcome) {
        case RECLAIM_SIM_SUCCESS: result->nr_reclaimed++; break;
        case RECLAIM_SIM_PUTBACK: result->nr_putback++; break;
        case RECLAIM_SIM_ACTIVATE: result->nr_activated++; break;
        case RECLAIM_SIM_BUSY: result->nr_busy++; break;
        case RECLAIM_SIM_DIRTY: result->nr_dirty++; break;
        case RECLAIM_SIM_WRITEBACK: result->nr_writeback++; break;
        case RECLAIM_SIM_UNEVICTABLE: result->nr_unevictable++; break;
        default: return -1;
        }
    }
    return 0;
}

void reclaim_simulator_executor_init(struct reclaim_simulator_executor *executor)
{
    if (executor != NULL) {
        *executor = (struct reclaim_simulator_executor){
            .ops = {.execute_batch = simulator_execute_batch},
            .injected_page_id = UINT64_MAX,
        };
    }
}

void reclaim_simulator_executor_inject(struct reclaim_simulator_executor *executor,
                                       uint64_t page_id,
                                       enum reclaim_sim_outcome outcome)
{
    if (executor != NULL) {
        executor->injected_page_id = page_id;
        executor->injected_outcome = outcome;
    }
}

void reclaim_simulator_executor_fail_next(struct reclaim_simulator_executor *executor,
                                          int error)
{
    if (executor != NULL) executor->injected_error = error;
}

const struct reclaim_executor_ops *reclaim_simulator_executor_ops(void)
{
    static const struct reclaim_executor_ops ops = {.execute_batch = simulator_execute_batch};
    return &ops;
}
