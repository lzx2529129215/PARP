#ifndef MYSELF_KSWAPD_ENGINE_H
#define MYSELF_KSWAPD_ENGINE_H

#include "myself_kswapd/error.h"
#include "myself_kswapd/platform.h"
#include "myself_kswapd/policy.h"
#include "myself_kswapd/stats.h"
#include "myself_kswapd/types.h"

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

struct reclaim_executor_ops;
struct reclaim_engine;
struct reclaim_domain;

struct reclaim_domain_config {
    bool override_swappiness;
    uint32_t swappiness;
    bool override_swap_enabled;
    bool swap_enabled;
};

struct reclaim_engine_config {
    uint32_t default_swappiness;
    bool default_swap_enabled;
    struct reclaim_pressure_config pressure;
    size_t page_hash_buckets;
    size_t domain_hash_buckets;
};

enum reclaim_stop_reason {
    RECLAIM_STOP_TARGET_REACHED = 0,
    RECLAIM_STOP_NO_SCANNABLE_PAGES,
    RECLAIM_STOP_NO_PROGRESS,
    RECLAIM_STOP_PRIORITY_EXHAUSTED,
    RECLAIM_STOP_EXECUTOR_ERROR,
    RECLAIM_STOP_ROUND_LIMIT
};

struct reclaim_result {
    enum reclaim_error error;
    enum reclaim_stop_reason stop_reason;
    uint64_t target_pages;
    uint64_t nr_folios_scanned;
    uint64_t nr_pages_scanned;
    uint64_t nr_folios_isolated;
    uint64_t nr_pages_isolated;
    uint64_t nr_folios_reclaimed;
    uint64_t nr_pages_reclaimed;
    uint64_t nr_pages_putback;
    uint64_t nr_pages_activated;
    uint64_t nr_overshoot_pages;
    uint32_t final_priority;
};

int reclaim_engine_create(const struct reclaim_platform *platform,
                          const struct reclaim_engine_config *config,
                          const struct reclaim_aging_ops *aging_ops,
                          const struct reclaim_executor_ops *executor_ops,
                          void *executor_context,
                          struct reclaim_engine **out_engine);
void reclaim_engine_destroy(struct reclaim_engine *engine);
int reclaim_engine_create_domain(struct reclaim_engine *engine, uint64_t cgroup_id);
int reclaim_engine_destroy_domain(struct reclaim_engine *engine, uint64_t cgroup_id);
int reclaim_engine_set_swappiness(struct reclaim_engine *engine,
                                  uint64_t cgroup_id,
                                  int inherited,
                                  uint32_t swappiness);
int reclaim_engine_set_swap_enabled(struct reclaim_engine *engine,
                                    uint64_t cgroup_id,
                                    int inherited,
                                    bool enabled);
int reclaim_engine_add_page(struct reclaim_engine *engine,
                            uint64_t page_id,
                            uint64_t cgroup_id,
                            enum reclaim_page_type type,
                            uint32_t order);
int reclaim_engine_remove_page(struct reclaim_engine *engine, uint64_t page_id);
int reclaim_engine_access_page(struct reclaim_engine *engine,
                              uint64_t page_id,
                              uint64_t access_cgroup_id);
int reclaim_engine_recharge_page(struct reclaim_engine *engine,
                                 uint64_t page_id,
                                 uint64_t new_cgroup_id);
int reclaim_engine_migrate_page(struct reclaim_engine *engine,
                                uint64_t old_page_id,
                                uint64_t new_page_id);
int reclaim_engine_set_page_outcome(struct reclaim_engine *engine,
                                    uint64_t page_id,
                                    enum reclaim_sim_outcome outcome);
int reclaim_engine_age_group(struct reclaim_engine *engine, uint64_t cgroup_id);
int reclaim_engine_age_all(struct reclaim_engine *engine);
int reclaim_engine_reclaim_group(struct reclaim_engine *engine,
                                 uint64_t cgroup_id,
                                 uint64_t target_pages,
                                 struct reclaim_result *result);
int reclaim_engine_reclaim_all(struct reclaim_engine *engine,
                               uint64_t target_pages,
                               struct reclaim_result *result);
const struct reclaim_page *reclaim_engine_get_page(const struct reclaim_engine *engine,
                                                   uint64_t page_id);
int reclaim_engine_get_domain_stats(const struct reclaim_engine *engine,
                                   uint64_t cgroup_id,
                                   struct reclaim_domain_stats *stats);
void reclaim_engine_get_stats(const struct reclaim_engine *engine,
                              struct reclaim_engine_stats *stats);
uint64_t reclaim_engine_event_seq(const struct reclaim_engine *engine);
typedef void (*reclaim_dump_line_fn)(void *context, const char *line);
int reclaim_engine_dump(const struct reclaim_engine *engine,
                        reclaim_dump_line_fn output,
                        void *output_context);

#endif
