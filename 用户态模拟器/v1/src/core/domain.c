#include "internal.h"

struct reclaim_list *reclaim_domain_lru(struct reclaim_domain *domain,
                                        enum reclaim_lru_kind kind)
{
    if (domain == NULL) {
        return NULL;
    }
    switch (kind) {
    case RECLAIM_LRU_INACTIVE_ANON: return &domain->inactive_anon;
    case RECLAIM_LRU_ACTIVE_ANON: return &domain->active_anon;
    case RECLAIM_LRU_INACTIVE_FILE: return &domain->inactive_file;
    case RECLAIM_LRU_ACTIVE_FILE: return &domain->active_file;
    default: return NULL;
    }
}

const struct reclaim_list *reclaim_domain_lru_const(const struct reclaim_domain *domain,
                                                    enum reclaim_lru_kind kind)
{
    return reclaim_domain_lru((struct reclaim_domain *)domain, kind);
}

enum reclaim_lru_kind reclaim_initial_lru(enum reclaim_page_type type)
{
    return type == RECLAIM_PAGE_ANON ? RECLAIM_LRU_INACTIVE_ANON :
                                       RECLAIM_LRU_INACTIVE_FILE;
}

void reclaim_account_add(struct reclaim_engine *engine,
                         struct reclaim_domain *domain,
                         struct reclaim_page *page,
                         enum reclaim_lru_kind kind)
{
    uint64_t pages = 0U;
    struct reclaim_list *list = reclaim_domain_lru(domain, kind);
    (void)reclaim_folio_base_pages(page->order, &pages);
    list->nr_base_pages += pages;
    domain->stats.nr_folios++;
    domain->stats.nr_base_pages += pages;
    engine->stats.nr_folios++;
    engine->stats.nr_base_pages += pages;
    if (reclaim_lru_is_active(kind)) {
        domain->stats.nr_active_folios++;
        domain->stats.nr_active_pages += pages;
        engine->stats.nr_active_folios++;
        engine->stats.nr_active_pages += pages;
    } else {
        domain->stats.nr_inactive_folios++;
        domain->stats.nr_inactive_pages += pages;
        engine->stats.nr_inactive_folios++;
        engine->stats.nr_inactive_pages += pages;
    }
}

void reclaim_account_remove(struct reclaim_engine *engine,
                            struct reclaim_domain *domain,
                            struct reclaim_page *page,
                            enum reclaim_lru_kind kind)
{
    uint64_t pages = 0U;
    struct reclaim_list *list = reclaim_domain_lru(domain, kind);
    (void)reclaim_folio_base_pages(page->order, &pages);
    if (list->nr_base_pages >= pages) list->nr_base_pages -= pages;
    if (domain->stats.nr_folios > 0U) domain->stats.nr_folios--;
    if (domain->stats.nr_base_pages >= pages) domain->stats.nr_base_pages -= pages;
    if (engine->stats.nr_folios > 0U) engine->stats.nr_folios--;
    if (engine->stats.nr_base_pages >= pages) engine->stats.nr_base_pages -= pages;
    if (reclaim_lru_is_active(kind)) {
        if (domain->stats.nr_active_folios > 0U) domain->stats.nr_active_folios--;
        if (domain->stats.nr_active_pages >= pages) domain->stats.nr_active_pages -= pages;
        if (engine->stats.nr_active_folios > 0U) engine->stats.nr_active_folios--;
        if (engine->stats.nr_active_pages >= pages) engine->stats.nr_active_pages -= pages;
    } else {
        if (domain->stats.nr_inactive_folios > 0U) domain->stats.nr_inactive_folios--;
        if (domain->stats.nr_inactive_pages >= pages) domain->stats.nr_inactive_pages -= pages;
        if (engine->stats.nr_inactive_folios > 0U) engine->stats.nr_inactive_folios--;
        if (engine->stats.nr_inactive_pages >= pages) engine->stats.nr_inactive_pages -= pages;
    }
}

int reclaim_link_page(struct reclaim_engine *engine,
                      struct reclaim_page *page,
                      struct reclaim_domain *domain,
                      enum reclaim_lru_kind kind,
                      enum reclaim_page_state state)
{
    struct reclaim_list *list = reclaim_domain_lru(domain, kind);
    if (list == NULL || page == NULL || domain == NULL) {
        return RECLAIM_ERR_INVALID_ARGUMENT;
    }
    page->lru_node.owner = page;
    reclaim_list_push_back(list, &page->lru_node);
    page->lru_kind = kind;
    page->state = state;
    reclaim_account_add(engine, domain, page, kind);
    return RECLAIM_OK;
}

void reclaim_unlink_page(struct reclaim_engine *engine,
                         struct reclaim_page *page,
                         struct reclaim_domain *domain)
{
    struct reclaim_list *list = reclaim_domain_lru(domain, page->lru_kind);
    if (list == NULL) {
        return;
    }
    reclaim_account_remove(engine, domain, page, page->lru_kind);
    reclaim_list_remove(list, &page->lru_node);
    page->lru_kind = RECLAIM_LRU_NONE;
}
