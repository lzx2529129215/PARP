#ifndef MYSELF_KSWAPD_STATS_H
#define MYSELF_KSWAPD_STATS_H

#include <stdint.h>

struct reclaim_domain_stats {
    uint64_t nr_folios;
    uint64_t nr_base_pages;
    uint64_t nr_active_folios;
    uint64_t nr_active_pages;
    uint64_t nr_inactive_folios;
    uint64_t nr_inactive_pages;
    uint64_t nr_unevictable_folios;
    uint64_t nr_unevictable_pages;
};

struct reclaim_engine_stats {
    uint64_t nr_folios;
    uint64_t nr_base_pages;
    uint64_t nr_active_folios;
    uint64_t nr_active_pages;
    uint64_t nr_inactive_folios;
    uint64_t nr_inactive_pages;
    uint64_t nr_unevictable_folios;
    uint64_t nr_unevictable_pages;
    uint64_t nr_overshoot_pages;
    uint64_t nr_scanned_folios;
    uint64_t nr_scanned_pages;
    uint64_t nr_isolated_folios;
    uint64_t nr_isolated_pages;
    uint64_t nr_reclaimed_folios;
    uint64_t nr_reclaimed_pages;
    uint64_t nr_putback_pages;
    uint64_t nr_activated_pages;
    uint64_t nr_busy;
    uint64_t nr_dirty;
    uint64_t nr_writeback;
};

#endif
