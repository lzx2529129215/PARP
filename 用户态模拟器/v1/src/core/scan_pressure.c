#include "myself_kswapd/policy.h"

#include <stddef.h>

uint64_t reclaim_scan_pages(uint64_t effective_lru_pages, uint32_t priority)
{
    uint64_t pages;
    if (effective_lru_pages == 0U) return 0U;
    pages = priority >= 64U ? 0U : effective_lru_pages >> priority;
    return pages == 0U ? 1U : pages;
}

void reclaim_split_scan_budget(uint64_t total_pages,
                               uint32_t swappiness,
                               int swap_enabled,
                               uint64_t anon_available,
                               uint64_t file_available,
                               uint64_t *anon_budget,
                               uint64_t *file_budget)
{
    uint64_t anon_wanted;
    uint64_t file_wanted;
    uint64_t anon_used;
    uint64_t file_used;
    uint64_t remaining;

    if (anon_budget == NULL || file_budget == NULL) return;
    if (!swap_enabled) {
        *anon_budget = 0U;
        *file_budget = total_pages < file_available ? total_pages : file_available;
        return;
    }
    if (swappiness > 200U) swappiness = 200U;
    anon_wanted = (total_pages * swappiness) / 200U;
    file_wanted = total_pages - anon_wanted;
    anon_used = anon_wanted < anon_available ? anon_wanted : anon_available;
    file_used = file_wanted < file_available ? file_wanted : file_available;
    remaining = total_pages - anon_used - file_used;
    if (remaining > 0U && anon_available > anon_used) {
        uint64_t extra = anon_available - anon_used;
        if (extra > remaining) extra = remaining;
        anon_used += extra;
        remaining -= extra;
    }
    if (remaining > 0U && file_available > file_used) {
        uint64_t extra = file_available - file_used;
        if (extra > remaining) extra = remaining;
        file_used += extra;
    }
    *anon_budget = anon_used;
    *file_budget = file_used;
}
