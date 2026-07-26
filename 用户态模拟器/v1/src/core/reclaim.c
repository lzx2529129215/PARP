#include "internal.h"

#include <stdint.h>

static void result_init(struct reclaim_result *result, uint64_t target_pages)
{
    *result = (struct reclaim_result){
        .error = RECLAIM_OK,
        .stop_reason = RECLAIM_STOP_PRIORITY_EXHAUSTED,
        .target_pages = target_pages,
    };
}

static uint64_t page_base_pages(const struct reclaim_page *page)
{
    uint64_t pages = 0U;
    (void)reclaim_folio_base_pages(page->order, &pages);
    return pages;
}

static void putback_page(struct reclaim_engine *engine,
                         struct reclaim_page *page,
                         enum reclaim_lru_kind source_lru)
{
    struct reclaim_domain *domain = reclaim_find_domain(engine, page->charge_cgroup_id);
    enum reclaim_lru_kind inactive = reclaim_lru_is_anon(source_lru) ?
        RECLAIM_LRU_INACTIVE_ANON : RECLAIM_LRU_INACTIVE_FILE;
    if (domain != NULL) {
        (void)reclaim_link_page(engine, page, domain, inactive, RECLAIM_PAGE_ON_LRU);
    }
}

static void activate_page(struct reclaim_engine *engine,
                          struct reclaim_page *page,
                          enum reclaim_lru_kind source_lru)
{
    struct reclaim_domain *domain = reclaim_find_domain(engine, page->charge_cgroup_id);
    enum reclaim_lru_kind active = reclaim_lru_is_anon(source_lru) ?
        RECLAIM_LRU_ACTIVE_ANON : RECLAIM_LRU_ACTIVE_FILE;
    if (domain != NULL) {
        (void)reclaim_link_page(engine, page, domain, active, RECLAIM_PAGE_ON_LRU);
    }
}

static void mark_unevictable(struct reclaim_engine *engine, struct reclaim_page *page)
{
    struct reclaim_domain *domain = reclaim_find_domain(engine, page->charge_cgroup_id);
    uint64_t pages = page_base_pages(page);
    if (domain != NULL) {
        domain->stats.nr_unevictable_folios++;
        domain->stats.nr_unevictable_pages += pages;
    }
    engine->stats.nr_unevictable_folios++;
    engine->stats.nr_unevictable_pages += pages;
    page->state = RECLAIM_PAGE_UNEVICTABLE;
    page->lru_kind = RECLAIM_LRU_NONE;
}

static void free_reclaimed_page(struct reclaim_engine *engine, struct reclaim_page *page)
{
    reclaim_page_hash_remove(engine, page);
    engine->stats.nr_reclaimed_folios++;
    engine->stats.nr_reclaimed_pages += page_base_pages(page);
    reclaim_free(engine, page);
}

static int add_candidate(struct reclaim_engine *engine,
                         struct reclaim_candidate_batch *batch,
                         struct reclaim_page *page,
                         enum reclaim_lru_kind source_lru,
                         struct reclaim_result *result)
{
    struct reclaim_domain *domain = reclaim_find_domain(engine, page->charge_cgroup_id);
    uint64_t pages = page_base_pages(page);

    if (batch->count == batch->capacity || domain == NULL) return RECLAIM_ERR_NO_MEMORY;
    batch->items[batch->count++] = (struct reclaim_candidate){
        .page = page,
        .source_lru = source_lru,
        .outcome = RECLAIM_SIM_SUCCESS,
    };
    reclaim_unlink_page(engine, page, domain);
    page->state = RECLAIM_PAGE_ISOLATED;
    result->nr_folios_scanned++;
    result->nr_pages_scanned += pages;
    result->nr_folios_isolated++;
    result->nr_pages_isolated += pages;
    engine->stats.nr_scanned_folios++;
    engine->stats.nr_scanned_pages += pages;
    engine->stats.nr_isolated_folios++;
    engine->stats.nr_isolated_pages += pages;
    return RECLAIM_OK;
}

static int select_list(struct reclaim_engine *engine,
                       struct reclaim_list *list,
                       enum reclaim_lru_kind kind,
                       uint64_t budget,
                       struct reclaim_candidate_batch *batch,
                       struct reclaim_result *result)
{
    struct reclaim_list_node *node = list->head.next;
    uint64_t selected_pages = 0U;
    while (node != &list->head && selected_pages < budget) {
        struct reclaim_list_node *next = node->next;
        struct reclaim_page *page = node->owner;
        uint64_t pages = page_base_pages(page);
        int error = add_candidate(engine, batch, page, kind, result);
        if (error != RECLAIM_OK) return error;
        selected_pages += pages;
        node = next;
    }
    return RECLAIM_OK;
}

static int select_domain(struct reclaim_engine *engine,
                         struct reclaim_domain *domain,
                         uint64_t target_remaining,
                         uint32_t priority,
                         struct reclaim_candidate_batch *batch,
                         struct reclaim_result *result)
{
    uint64_t anon_available = domain->inactive_anon.nr_base_pages;
    uint64_t file_available = domain->inactive_file.nr_base_pages;
    uint64_t effective = anon_available + file_available;
    uint64_t scan_budget = reclaim_scan_pages(effective, priority);
    uint64_t anon_budget;
    uint64_t file_budget;
    uint64_t total_available;

    if (effective == 0U || target_remaining == 0U) return RECLAIM_OK;
    if (scan_budget > target_remaining) scan_budget = target_remaining;
    reclaim_split_scan_budget(scan_budget,
                              domain->config.swappiness,
                              domain->config.swap_enabled,
                              anon_available,
                              file_available,
                              &anon_budget,
                              &file_budget);
    total_available = anon_budget + file_budget;
    if (total_available == 0U) return RECLAIM_OK;
    if (select_list(engine, &domain->inactive_anon, RECLAIM_LRU_INACTIVE_ANON,
                    anon_budget, batch, result) != RECLAIM_OK) return RECLAIM_ERR_NO_MEMORY;
    if (select_list(engine, &domain->inactive_file, RECLAIM_LRU_INACTIVE_FILE,
                    file_budget, batch, result) != RECLAIM_OK) return RECLAIM_ERR_NO_MEMORY;
    return RECLAIM_OK;
}

static void rollback_batch(struct reclaim_engine *engine,
                           struct reclaim_candidate_batch *batch,
                           struct reclaim_result *result)
{
    size_t i;
    for (i = 0U; i < batch->count; i++) {
        struct reclaim_page *page = batch->items[i].page;
        if (page->state == RECLAIM_PAGE_ISOLATED) {
            uint64_t pages = page_base_pages(page);
            putback_page(engine, page, batch->items[i].source_lru);
            result->nr_pages_putback += pages;
        }
    }
}

static int execute_batch(struct reclaim_engine *engine,
                         struct reclaim_candidate_batch *batch,
                         struct reclaim_result *result)
{
    struct reclaim_exec_result execution;
    size_t i;

    if (batch->count == 0U) return RECLAIM_OK;
    if (engine->executor_ops->execute_batch(engine->executor_context, batch, &execution) != 0 ||
        execution.error != 0) {
        rollback_batch(engine, batch, result);
        return RECLAIM_ERR_EXECUTOR;
    }
    for (i = 0U; i < batch->count; i++) {
        struct reclaim_candidate *candidate = &batch->items[i];
        struct reclaim_page *page = candidate->page;
        uint64_t pages = page_base_pages(page);
        switch (candidate->outcome) {
        case RECLAIM_SIM_SUCCESS:
            free_reclaimed_page(engine, page);
            result->nr_folios_reclaimed++;
            result->nr_pages_reclaimed += pages;
            break;
        case RECLAIM_SIM_ACTIVATE:
            activate_page(engine, page, candidate->source_lru);
            result->nr_pages_activated += pages;
            break;
        case RECLAIM_SIM_UNEVICTABLE:
            mark_unevictable(engine, page);
            break;
        case RECLAIM_SIM_PUTBACK:
        case RECLAIM_SIM_BUSY:
        case RECLAIM_SIM_DIRTY:
        case RECLAIM_SIM_WRITEBACK:
            putback_page(engine, page, candidate->source_lru);
            result->nr_pages_putback += pages;
            if (candidate->outcome == RECLAIM_SIM_BUSY) engine->stats.nr_busy++;
            if (candidate->outcome == RECLAIM_SIM_DIRTY) engine->stats.nr_dirty++;
            if (candidate->outcome == RECLAIM_SIM_WRITEBACK) engine->stats.nr_writeback++;
            break;
        default:
            return RECLAIM_ERR_INTERNAL;
        }
    }
    return RECLAIM_OK;
}

static int reclaim_round(struct reclaim_engine *engine,
                         struct reclaim_domain *only_domain,
                         uint64_t target_remaining,
                         uint32_t priority,
                         struct reclaim_result *result,
                         bool *had_candidates,
                         bool *had_reclaim)
{
    struct reclaim_domain *domain;
    struct reclaim_candidate_batch batch;
    int error;

    batch.capacity = 64U;
    batch.count = 0U;
    batch.items = reclaim_calloc(engine, batch.capacity, sizeof(*batch.items));
    if (batch.items == NULL) return RECLAIM_ERR_NO_MEMORY;
    if (only_domain != NULL) {
        error = select_domain(engine, only_domain, target_remaining, priority, &batch, result);
        if (error != RECLAIM_OK) {
            rollback_batch(engine, &batch, result);
            reclaim_free(engine, batch.items);
            return error;
        }
    } else {
        for (domain = engine->domains.sorted_head; domain != NULL;
             domain = domain->sorted_next) {
            uint64_t reclaimed = result->nr_pages_reclaimed;
            if (reclaimed >= result->target_pages) break;
            error = select_domain(engine, domain, result->target_pages - reclaimed,
                                  priority, &batch, result);
            if (error != RECLAIM_OK) {
                rollback_batch(engine, &batch, result);
                reclaim_free(engine, batch.items);
                return error;
            }
        }
    }
    if (batch.count > 0U) {
        *had_candidates = true;
        error = execute_batch(engine, &batch, result);
        if (error != RECLAIM_OK) {
            reclaim_free(engine, batch.items);
            return error;
        }
        if (result->nr_pages_reclaimed > 0U) *had_reclaim = true;
    }
    reclaim_free(engine, batch.items);
    return RECLAIM_OK;
}

static int reclaim_run(struct reclaim_engine *engine,
                       struct reclaim_domain *only_domain,
                       uint64_t target_pages,
                       struct reclaim_result *result)
{
    uint32_t priority;
    uint32_t rounds = 0U;
    bool had_candidates = false;
    bool had_reclaim = false;
    int error;

    result_init(result, target_pages);
    if (target_pages == 0U) {
        result->stop_reason = RECLAIM_STOP_TARGET_REACHED;
        return RECLAIM_OK;
    }
    engine->event_seq++;
    for (priority = engine->config.pressure.default_priority;; priority--) {
        bool round_candidates = false;
        bool round_reclaim = false;
        error = reclaim_round(engine, only_domain, target_pages - result->nr_pages_reclaimed,
                              priority, result, &round_candidates, &round_reclaim);
        result->final_priority = priority;
        if (error == RECLAIM_ERR_NO_MEMORY) {
            result->error = error;
            return error;
        }
        if (error != RECLAIM_OK) {
            result->error = error;
            result->stop_reason = RECLAIM_STOP_EXECUTOR_ERROR;
            return error;
        }
        had_candidates = had_candidates || round_candidates;
        had_reclaim = had_reclaim || round_reclaim;
        rounds++;
        if (result->nr_pages_reclaimed >= target_pages) {
            result->stop_reason = RECLAIM_STOP_TARGET_REACHED;
            break;
        }
        if (!round_candidates) {
            result->stop_reason = had_candidates ? RECLAIM_STOP_NO_SCANNABLE_PAGES :
                                                   RECLAIM_STOP_NO_SCANNABLE_PAGES;
            break;
        }
        if (round_candidates && !round_reclaim) {
            result->stop_reason = RECLAIM_STOP_NO_PROGRESS;
            break;
        }
        if (rounds >= engine->config.pressure.max_reclaim_rounds) {
            result->stop_reason = RECLAIM_STOP_ROUND_LIMIT;
            break;
        }
        if (priority == engine->config.pressure.minimum_priority) {
            result->stop_reason = had_reclaim ? RECLAIM_STOP_PRIORITY_EXHAUSTED :
                                                RECLAIM_STOP_NO_PROGRESS;
            break;
        }
    }
    result->nr_overshoot_pages = result->nr_pages_reclaimed > target_pages ?
        result->nr_pages_reclaimed - target_pages : 0U;
    engine->stats.nr_overshoot_pages += result->nr_overshoot_pages;
    return RECLAIM_OK;
}

int reclaim_engine_reclaim_group(struct reclaim_engine *engine,
                                 uint64_t cgroup_id,
                                 uint64_t target_pages,
                                 struct reclaim_result *result)
{
    struct reclaim_domain *domain;
    if (engine == NULL || result == NULL) return RECLAIM_ERR_INVALID_ARGUMENT;
    domain = reclaim_find_domain(engine, cgroup_id);
    if (domain == NULL) return RECLAIM_ERR_DOMAIN_NOT_FOUND;
    return reclaim_run(engine, domain, target_pages, result);
}

int reclaim_engine_reclaim_all(struct reclaim_engine *engine,
                               uint64_t target_pages,
                               struct reclaim_result *result)
{
    if (engine == NULL || result == NULL) return RECLAIM_ERR_INVALID_ARGUMENT;
    return reclaim_run(engine, NULL, target_pages, result);
}
