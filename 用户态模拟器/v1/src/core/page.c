#include "internal.h"

static int valid_page_type(enum reclaim_page_type type)
{
    return type == RECLAIM_PAGE_ANON || type == RECLAIM_PAGE_FILE;
}

int reclaim_engine_add_page(struct reclaim_engine *engine,
                            uint64_t page_id,
                            uint64_t cgroup_id,
                            enum reclaim_page_type type,
                            uint32_t order)
{
    struct reclaim_domain *domain;
    struct reclaim_page *page;

    if (engine == NULL || !valid_page_type(type)) return RECLAIM_ERR_INVALID_ARGUMENT;
    domain = reclaim_find_domain(engine, cgroup_id);
    if (domain == NULL) return RECLAIM_ERR_DOMAIN_NOT_FOUND;
    if (reclaim_find_page(engine, page_id) != NULL) return RECLAIM_ERR_PAGE_ALREADY_EXISTS;
    if (order >= 64U) return RECLAIM_ERR_INVALID_ARGUMENT;
    page = reclaim_calloc(engine, 1U, sizeof(*page));
    if (page == NULL) return RECLAIM_ERR_NO_MEMORY;
    page->page_id = page_id;
    page->charge_cgroup_id = cgroup_id;
    page->last_access_cgroup_id = cgroup_id;
    page->type = type;
    page->state = RECLAIM_PAGE_NEW;
    page->next_sim_outcome = RECLAIM_SIM_SUCCESS;
    page->order = order;
    reclaim_page_hash_insert(engine, page);
    if (reclaim_link_page(engine, page, domain, reclaim_initial_lru(type),
                          RECLAIM_PAGE_ON_LRU) != RECLAIM_OK) {
        reclaim_page_hash_remove(engine, page);
        reclaim_free(engine, page);
        return RECLAIM_ERR_INTERNAL;
    }
    engine->event_seq++;
    return RECLAIM_OK;
}

int reclaim_engine_remove_page(struct reclaim_engine *engine, uint64_t page_id)
{
    struct reclaim_page *page;
    struct reclaim_domain *domain;

    if (engine == NULL) return RECLAIM_ERR_INVALID_ARGUMENT;
    page = reclaim_find_page(engine, page_id);
    if (page == NULL) return RECLAIM_ERR_PAGE_NOT_FOUND;
    if (page->state == RECLAIM_PAGE_ISOLATED) return RECLAIM_ERR_PAGE_STATE;
    domain = reclaim_find_domain(engine, page->charge_cgroup_id);
    if (domain == NULL) return RECLAIM_ERR_INTERNAL;
    if (page->state == RECLAIM_PAGE_ON_LRU) {
        reclaim_unlink_page(engine, page, domain);
    } else if (page->state == RECLAIM_PAGE_UNEVICTABLE) {
        uint64_t pages = 0U;
        (void)reclaim_folio_base_pages(page->order, &pages);
        if (domain->stats.nr_unevictable_folios > 0U) domain->stats.nr_unevictable_folios--;
        if (domain->stats.nr_unevictable_pages >= pages) domain->stats.nr_unevictable_pages -= pages;
        if (engine->stats.nr_unevictable_folios > 0U) engine->stats.nr_unevictable_folios--;
        if (engine->stats.nr_unevictable_pages >= pages) engine->stats.nr_unevictable_pages -= pages;
    }
    reclaim_page_hash_remove(engine, page);
    reclaim_free(engine, page);
    engine->event_seq++;
    return RECLAIM_OK;
}

int reclaim_engine_access_page(struct reclaim_engine *engine,
                               uint64_t page_id,
                               uint64_t access_cgroup_id)
{
    struct reclaim_page *page;
    if (engine == NULL) return RECLAIM_ERR_INVALID_ARGUMENT;
    page = reclaim_find_page(engine, page_id);
    if (page == NULL) return RECLAIM_ERR_PAGE_NOT_FOUND;
    if (reclaim_find_domain(engine, access_cgroup_id) == NULL) {
        return RECLAIM_ERR_DOMAIN_NOT_FOUND;
    }
    page->referenced = true;
    page->last_access_seq = ++engine->event_seq;
    page->last_access_cgroup_id = access_cgroup_id;
    page->access_count++;
    if (access_cgroup_id != page->charge_cgroup_id) page->shared = true;
    return RECLAIM_OK;
}

int reclaim_engine_recharge_page(struct reclaim_engine *engine,
                                 uint64_t page_id,
                                 uint64_t new_cgroup_id)
{
    struct reclaim_page *page;
    struct reclaim_domain *old_domain;
    struct reclaim_domain *new_domain;
    enum reclaim_lru_kind kind;

    if (engine == NULL) return RECLAIM_ERR_INVALID_ARGUMENT;
    page = reclaim_find_page(engine, page_id);
    if (page == NULL) return RECLAIM_ERR_PAGE_NOT_FOUND;
    if (page->state != RECLAIM_PAGE_ON_LRU) return RECLAIM_ERR_PAGE_STATE;
    new_domain = reclaim_find_domain(engine, new_cgroup_id);
    if (new_domain == NULL) return RECLAIM_ERR_DOMAIN_NOT_FOUND;
    old_domain = reclaim_find_domain(engine, page->charge_cgroup_id);
    if (old_domain == NULL) return RECLAIM_ERR_INTERNAL;
    kind = page->lru_kind;
    reclaim_unlink_page(engine, page, old_domain);
    page->charge_cgroup_id = new_cgroup_id;
    reclaim_link_page(engine, page, new_domain, kind, RECLAIM_PAGE_ON_LRU);
    engine->event_seq++;
    return RECLAIM_OK;
}

int reclaim_engine_migrate_page(struct reclaim_engine *engine,
                                uint64_t old_page_id,
                                uint64_t new_page_id)
{
    struct reclaim_page *page;
    if (engine == NULL) return RECLAIM_ERR_INVALID_ARGUMENT;
    page = reclaim_find_page(engine, old_page_id);
    if (page == NULL) return RECLAIM_ERR_PAGE_NOT_FOUND;
    if (reclaim_find_page(engine, new_page_id) != NULL) return RECLAIM_ERR_PAGE_ALREADY_EXISTS;
    reclaim_page_hash_remove(engine, page);
    page->page_id = new_page_id;
    reclaim_page_hash_insert(engine, page);
    engine->event_seq++;
    return RECLAIM_OK;
}

int reclaim_engine_set_page_outcome(struct reclaim_engine *engine,
                                    uint64_t page_id,
                                    enum reclaim_sim_outcome outcome)
{
    struct reclaim_page *page;
    if (engine == NULL || outcome > RECLAIM_SIM_UNEVICTABLE) return RECLAIM_ERR_INVALID_ARGUMENT;
    page = reclaim_find_page(engine, page_id);
    if (page == NULL) return RECLAIM_ERR_PAGE_NOT_FOUND;
    page->next_sim_outcome = outcome;
    engine->event_seq++;
    return RECLAIM_OK;
}
