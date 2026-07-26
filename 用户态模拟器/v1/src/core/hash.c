#include "internal.h"

size_t reclaim_page_bucket(const struct reclaim_engine *engine, uint64_t page_id)
{
    return (size_t)((page_id * UINT64_C(11400714819323198485)) % engine->pages.bucket_count);
}

size_t reclaim_domain_bucket(const struct reclaim_engine *engine, uint64_t cgroup_id)
{
    return (size_t)((cgroup_id * UINT64_C(14029467366897019727)) %
                    engine->domains.bucket_count);
}

struct reclaim_page *reclaim_find_page(struct reclaim_engine *engine, uint64_t page_id)
{
    struct reclaim_page *page;
    size_t bucket;

    bucket = reclaim_page_bucket(engine, page_id);
    for (page = engine->pages.buckets[bucket]; page != NULL; page = page->hash_next) {
        if (page->page_id == page_id) {
            return page;
        }
    }
    return NULL;
}

const struct reclaim_page *reclaim_find_page_const(const struct reclaim_engine *engine,
                                                   uint64_t page_id)
{
    struct reclaim_page *page;
    size_t bucket;

    bucket = reclaim_page_bucket(engine, page_id);
    for (page = engine->pages.buckets[bucket]; page != NULL; page = page->hash_next) {
        if (page->page_id == page_id) {
            return page;
        }
    }
    return NULL;
}

struct reclaim_domain *reclaim_find_domain(struct reclaim_engine *engine, uint64_t cgroup_id)
{
    struct reclaim_domain *domain;
    size_t bucket;

    bucket = reclaim_domain_bucket(engine, cgroup_id);
    for (domain = engine->domains.buckets[bucket]; domain != NULL; domain = domain->hash_next) {
        if (domain->cgroup_id == cgroup_id) {
            return domain;
        }
    }
    return NULL;
}

const struct reclaim_domain *reclaim_find_domain_const(const struct reclaim_engine *engine,
                                                       uint64_t cgroup_id)
{
    struct reclaim_domain *domain;
    size_t bucket;

    bucket = reclaim_domain_bucket(engine, cgroup_id);
    for (domain = engine->domains.buckets[bucket]; domain != NULL; domain = domain->hash_next) {
        if (domain->cgroup_id == cgroup_id) {
            return domain;
        }
    }
    return NULL;
}

void reclaim_page_hash_insert(struct reclaim_engine *engine, struct reclaim_page *page)
{
    size_t bucket = reclaim_page_bucket(engine, page->page_id);
    page->hash_next = engine->pages.buckets[bucket];
    engine->pages.buckets[bucket] = page;
}

void reclaim_page_hash_remove(struct reclaim_engine *engine, struct reclaim_page *page)
{
    struct reclaim_page **cursor;
    size_t bucket = reclaim_page_bucket(engine, page->page_id);

    for (cursor = &engine->pages.buckets[bucket]; *cursor != NULL; cursor = &(*cursor)->hash_next) {
        if (*cursor == page) {
            *cursor = page->hash_next;
            page->hash_next = NULL;
            return;
        }
    }
}

void reclaim_domain_hash_insert(struct reclaim_engine *engine, struct reclaim_domain *domain)
{
    size_t bucket = reclaim_domain_bucket(engine, domain->cgroup_id);
    domain->hash_next = engine->domains.buckets[bucket];
    engine->domains.buckets[bucket] = domain;
}

void reclaim_domain_hash_remove(struct reclaim_engine *engine, struct reclaim_domain *domain)
{
    struct reclaim_domain **cursor;
    size_t bucket = reclaim_domain_bucket(engine, domain->cgroup_id);

    for (cursor = &engine->domains.buckets[bucket]; *cursor != NULL;
         cursor = &(*cursor)->hash_next) {
        if (*cursor == domain) {
            *cursor = domain->hash_next;
            domain->hash_next = NULL;
            return;
        }
    }
}

void reclaim_domain_sorted_insert(struct reclaim_engine *engine, struct reclaim_domain *domain)
{
    struct reclaim_domain *cursor = engine->domains.sorted_head;
    struct reclaim_domain *previous = NULL;

    while (cursor != NULL && cursor->cgroup_id < domain->cgroup_id) {
        previous = cursor;
        cursor = cursor->sorted_next;
    }
    domain->sorted_prev = previous;
    domain->sorted_next = cursor;
    if (previous == NULL) {
        engine->domains.sorted_head = domain;
    } else {
        previous->sorted_next = domain;
    }
    if (cursor != NULL) {
        cursor->sorted_prev = domain;
    }
}

void reclaim_domain_sorted_remove(struct reclaim_engine *engine, struct reclaim_domain *domain)
{
    if (domain->sorted_prev == NULL) {
        engine->domains.sorted_head = domain->sorted_next;
    } else {
        domain->sorted_prev->sorted_next = domain->sorted_next;
    }
    if (domain->sorted_next != NULL) {
        domain->sorted_next->sorted_prev = domain->sorted_prev;
    }
    domain->sorted_prev = NULL;
    domain->sorted_next = NULL;
}
