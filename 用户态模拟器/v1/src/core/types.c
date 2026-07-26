#include "myself_kswapd/types.h"

const char *reclaim_page_type_name(enum reclaim_page_type type)
{
    switch (type) {
    case RECLAIM_PAGE_ANON: return "ANON";
    case RECLAIM_PAGE_FILE: return "FILE";
    default: return "UNKNOWN";
    }
}

const char *reclaim_page_state_name(enum reclaim_page_state state)
{
    switch (state) {
    case RECLAIM_PAGE_NEW: return "NEW";
    case RECLAIM_PAGE_ON_LRU: return "ON_LRU";
    case RECLAIM_PAGE_ISOLATED: return "ISOLATED";
    case RECLAIM_PAGE_UNEVICTABLE: return "UNEVICTABLE";
    default: return "UNKNOWN";
    }
}

const char *reclaim_lru_kind_name(enum reclaim_lru_kind kind)
{
    switch (kind) {
    case RECLAIM_LRU_NONE: return "NONE";
    case RECLAIM_LRU_INACTIVE_ANON: return "INACTIVE_ANON";
    case RECLAIM_LRU_ACTIVE_ANON: return "ACTIVE_ANON";
    case RECLAIM_LRU_INACTIVE_FILE: return "INACTIVE_FILE";
    case RECLAIM_LRU_ACTIVE_FILE: return "ACTIVE_FILE";
    default: return "UNKNOWN";
    }
}

const char *reclaim_sim_outcome_name(enum reclaim_sim_outcome outcome)
{
    switch (outcome) {
    case RECLAIM_SIM_SUCCESS: return "SUCCESS";
    case RECLAIM_SIM_PUTBACK: return "PUTBACK";
    case RECLAIM_SIM_ACTIVATE: return "ACTIVATE";
    case RECLAIM_SIM_BUSY: return "BUSY";
    case RECLAIM_SIM_DIRTY: return "DIRTY";
    case RECLAIM_SIM_WRITEBACK: return "WRITEBACK";
    case RECLAIM_SIM_UNEVICTABLE: return "UNEVICTABLE";
    default: return "UNKNOWN";
    }
}

int reclaim_folio_base_pages(uint32_t order, uint64_t *pages)
{
    if (pages == NULL || order >= 64U) {
        return -1;
    }
    *pages = 1ULL << order;
    return 0;
}

bool reclaim_lru_is_active(enum reclaim_lru_kind kind)
{
    return kind == RECLAIM_LRU_ACTIVE_ANON || kind == RECLAIM_LRU_ACTIVE_FILE;
}

bool reclaim_lru_is_anon(enum reclaim_lru_kind kind)
{
    return kind == RECLAIM_LRU_INACTIVE_ANON || kind == RECLAIM_LRU_ACTIVE_ANON;
}

bool reclaim_lru_is_file(enum reclaim_lru_kind kind)
{
    return kind == RECLAIM_LRU_INACTIVE_FILE || kind == RECLAIM_LRU_ACTIVE_FILE;
}
