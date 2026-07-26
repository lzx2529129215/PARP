#include "internal.h"

#include "myself_kswapd/validator.h"

#include <stddef.h>

static uint64_t validator_page_base_pages(const struct reclaim_page *page)
{
    uint64_t pages = 0U;
    (void)reclaim_folio_base_pages(page->order, &pages);
    return pages;
}

static int validation_fail(struct reclaim_validation_report *report,
                           const struct reclaim_engine *engine,
                           uint64_t page_id,
                           uint64_t cgroup_id,
                           const char *invariant,
                           uint64_t expected,
                           uint64_t observed)
{
    if (report != NULL) {
        *report = (struct reclaim_validation_report){
            .event_seq = engine->event_seq,
            .page_id = page_id,
            .cgroup_id = cgroup_id,
            .invariant = invariant,
            .expected = expected,
            .observed = observed,
        };
    }
    return RECLAIM_ERR_VALIDATION;
}

static int validate_list(const struct reclaim_engine *engine,
                         const struct reclaim_domain *domain,
                         const struct reclaim_list *list,
                         enum reclaim_lru_kind kind,
                         struct reclaim_validation_report *report,
                         uint64_t *folios,
                         uint64_t *pages)
{
    const struct reclaim_list_node *node;
    uint64_t count = 0U;
    uint64_t base_pages = 0U;

    for (node = list->head.next; node != &list->head; node = node->next) {
        const struct reclaim_page *page = node->owner;
        if (node->list != list || node->prev == NULL || node->next == NULL || page == NULL) {
            return validation_fail(report, engine, page == NULL ? 0U : page->page_id,
                                   domain->cgroup_id, "list node linkage", 1U, 0U);
        }
        if (reclaim_find_page_const(engine, page->page_id) != page) {
            return validation_fail(report, engine, page->page_id, domain->cgroup_id,
                                   "lru page is indexed", 1U, 0U);
        }
        if (page->state != RECLAIM_PAGE_ON_LRU || page->lru_kind != kind ||
            page->charge_cgroup_id != domain->cgroup_id ||
            (reclaim_lru_is_anon(kind) && page->type != RECLAIM_PAGE_ANON) ||
            (reclaim_lru_is_file(kind) && page->type != RECLAIM_PAGE_FILE)) {
            return validation_fail(report, engine, page->page_id, domain->cgroup_id,
                                   "page state owner type and lru", 1U, 0U);
        }
        count++;
        base_pages += validator_page_base_pages(page);
    }
    if (list->nr_folios != count || list->nr_base_pages != base_pages) {
        return validation_fail(report, engine, 0U, domain->cgroup_id,
                               "lru counters", base_pages, list->nr_base_pages);
    }
    *folios += count;
    *pages += base_pages;
    return RECLAIM_OK;
}

static int page_link_count(const struct reclaim_engine *engine,
                           const struct reclaim_page *needle)
{
    const struct reclaim_domain *domain;
    enum reclaim_lru_kind kind;
    int count = 0;
    for (domain = engine->domains.sorted_head; domain != NULL; domain = domain->sorted_next) {
        for (kind = RECLAIM_LRU_INACTIVE_ANON; kind <= RECLAIM_LRU_ACTIVE_FILE; kind++) {
            const struct reclaim_list *list = reclaim_domain_lru_const(domain, kind);
            const struct reclaim_list_node *node;
            for (node = list->head.next; node != &list->head; node = node->next) {
                if (node->owner == needle) count++;
            }
        }
    }
    return count;
}

int reclaim_engine_validate(const struct reclaim_engine *engine,
                            struct reclaim_validation_report *report)
{
    const struct reclaim_domain *domain;
    const struct reclaim_page *page;
    uint64_t ordinary_folios = 0U;
    uint64_t ordinary_pages = 0U;
    uint64_t active_folios = 0U;
    uint64_t active_pages = 0U;
    uint64_t inactive_folios = 0U;
    uint64_t inactive_pages = 0U;
    uint64_t unevictable_folios = 0U;
    uint64_t unevictable_pages = 0U;
    size_t i;

    if (engine == NULL) return RECLAIM_ERR_INVALID_ARGUMENT;
    for (domain = engine->domains.sorted_head; domain != NULL; domain = domain->sorted_next) {
        uint64_t domain_folios = 0U;
        uint64_t domain_pages = 0U;
        uint64_t domain_active_folios = 0U;
        uint64_t domain_active_pages = 0U;
        uint64_t domain_inactive_folios = 0U;
        uint64_t domain_inactive_pages = 0U;
        int error;
        error = validate_list(engine, domain, &domain->inactive_anon,
                              RECLAIM_LRU_INACTIVE_ANON, report,
                              &domain_inactive_folios, &domain_inactive_pages);
        if (error != RECLAIM_OK) return error;
        error = validate_list(engine, domain, &domain->active_anon,
                              RECLAIM_LRU_ACTIVE_ANON, report,
                              &domain_active_folios, &domain_active_pages);
        if (error != RECLAIM_OK) return error;
        error = validate_list(engine, domain, &domain->inactive_file,
                              RECLAIM_LRU_INACTIVE_FILE, report,
                              &domain_inactive_folios, &domain_inactive_pages);
        if (error != RECLAIM_OK) return error;
        error = validate_list(engine, domain, &domain->active_file,
                              RECLAIM_LRU_ACTIVE_FILE, report,
                              &domain_active_folios, &domain_active_pages);
        if (error != RECLAIM_OK) return error;
        domain_folios = domain_active_folios + domain_inactive_folios;
        domain_pages = domain_active_pages + domain_inactive_pages;
        if (domain->stats.nr_folios != domain_folios ||
            domain->stats.nr_base_pages != domain_pages ||
            domain->stats.nr_active_folios != domain_active_folios ||
            domain->stats.nr_active_pages != domain_active_pages ||
            domain->stats.nr_inactive_folios != domain_inactive_folios ||
            domain->stats.nr_inactive_pages != domain_inactive_pages) {
            return validation_fail(report, engine, 0U, domain->cgroup_id,
                                   "domain statistics", domain_folios, domain->stats.nr_folios);
        }
        ordinary_folios += domain_folios;
        ordinary_pages += domain_pages;
        active_folios += domain_active_folios;
        active_pages += domain_active_pages;
        inactive_folios += domain_inactive_folios;
        inactive_pages += domain_inactive_pages;
        unevictable_folios += domain->stats.nr_unevictable_folios;
        unevictable_pages += domain->stats.nr_unevictable_pages;
    }
    for (i = 0U; i < engine->pages.bucket_count; i++) {
        for (page = engine->pages.buckets[i]; page != NULL; page = page->hash_next) {
            int links = page_link_count(engine, page);
            if (page->state == RECLAIM_PAGE_ON_LRU && links != 1) {
                return validation_fail(report, engine, page->page_id,
                                       page->charge_cgroup_id, "on lru has one link", 1U,
                                       (uint64_t)links);
            }
            if ((page->state == RECLAIM_PAGE_ISOLATED ||
                 page->state == RECLAIM_PAGE_UNEVICTABLE) && links != 0) {
                return validation_fail(report, engine, page->page_id,
                                       page->charge_cgroup_id, "isolated or unevictable link", 0U,
                                       (uint64_t)links);
            }
            if (page->state != RECLAIM_PAGE_ON_LRU && page->state != RECLAIM_PAGE_ISOLATED &&
                page->state != RECLAIM_PAGE_UNEVICTABLE) {
                return validation_fail(report, engine, page->page_id,
                                       page->charge_cgroup_id, "indexed page state", 1U, 0U);
            }
        }
    }
    if (engine->stats.nr_folios != ordinary_folios || engine->stats.nr_base_pages != ordinary_pages ||
        engine->stats.nr_active_folios != active_folios ||
        engine->stats.nr_active_pages != active_pages ||
        engine->stats.nr_inactive_folios != inactive_folios ||
        engine->stats.nr_inactive_pages != inactive_pages ||
        engine->stats.nr_unevictable_folios != unevictable_folios ||
        engine->stats.nr_unevictable_pages != unevictable_pages) {
        return validation_fail(report, engine, 0U, 0U, "engine statistics", ordinary_folios,
                               engine->stats.nr_folios);
    }
    return RECLAIM_OK;
}
