#ifndef MYSELF_KSWAPD_VALIDATOR_H
#define MYSELF_KSWAPD_VALIDATOR_H

#include <stdint.h>

struct reclaim_engine;

struct reclaim_validation_report {
    uint64_t event_seq;
    uint64_t page_id;
    uint64_t cgroup_id;
    const char *invariant;
    uint64_t expected;
    uint64_t observed;
};

int reclaim_engine_validate(const struct reclaim_engine *engine,
                            struct reclaim_validation_report *report);

#endif
