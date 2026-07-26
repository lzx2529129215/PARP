#ifndef MYSELF_KSWAPD_POLICY_H
#define MYSELF_KSWAPD_POLICY_H

#include <stdint.h>

struct reclaim_engine;

struct reclaim_pressure_config {
    uint32_t default_priority;
    uint32_t minimum_priority;
    uint32_t scan_batch_pages;
    uint32_t max_reclaim_rounds;
};

struct reclaim_aging_ops {
    int (*age_group)(void *context, struct reclaim_engine *engine, uint64_t cgroup_id);
    int (*age_all)(void *context, struct reclaim_engine *engine);
    void *context;
};

uint64_t reclaim_scan_pages(uint64_t effective_lru_pages, uint32_t priority);
void reclaim_split_scan_budget(uint64_t total_pages,
                               uint32_t swappiness,
                               int swap_enabled,
                               uint64_t anon_available,
                               uint64_t file_available,
                               uint64_t *anon_budget,
                               uint64_t *file_budget);

const struct reclaim_aging_ops *reclaim_g1_aging_ops(void);

#endif
