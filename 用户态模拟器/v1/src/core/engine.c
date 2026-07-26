#include "internal.h"

#include "myself_kswapd/error.h"

#include <string.h>

static const struct reclaim_engine_config default_config = {
    .default_swappiness = 60U,
    .default_swap_enabled = true,
    .pressure = {
        .default_priority = 12U,
        .minimum_priority = 0U,
        .scan_batch_pages = 32U,
        .max_reclaim_rounds = 13U,
    },
    .page_hash_buckets = 64U,
    .domain_hash_buckets = 64U,
};

void *reclaim_alloc(struct reclaim_engine *engine, size_t size)
{
    if (engine == NULL || engine->platform.allocator == NULL ||
        engine->platform.allocator->alloc == NULL) {
        return NULL;
    }
    return engine->platform.allocator->alloc(engine->platform.allocator_context, size);
}

void *reclaim_calloc(struct reclaim_engine *engine, size_t count, size_t size)
{
    if (engine == NULL || engine->platform.allocator == NULL ||
        engine->platform.allocator->calloc == NULL) {
        return NULL;
    }
    return engine->platform.allocator->calloc(engine->platform.allocator_context, count, size);
}

void reclaim_free(struct reclaim_engine *engine, void *pointer)
{
    if (engine != NULL && engine->platform.allocator != NULL &&
        engine->platform.allocator->dealloc != NULL) {
        engine->platform.allocator->dealloc(engine->platform.allocator_context, pointer);
    }
}

static int platform_valid(const struct reclaim_platform *platform)
{
    return platform != NULL && platform->allocator != NULL &&
           platform->allocator->alloc != NULL && platform->allocator->calloc != NULL &&
           platform->allocator->dealloc != NULL;
}

int reclaim_engine_create(const struct reclaim_platform *platform,
                          const struct reclaim_engine_config *config,
                          const struct reclaim_aging_ops *aging_ops,
                          const struct reclaim_executor_ops *executor_ops,
                          void *executor_context,
                          struct reclaim_engine **out_engine)
{
    struct reclaim_engine_config selected;
    struct reclaim_engine *engine;

    if (!platform_valid(platform) || executor_ops == NULL || out_engine == NULL) {
        return RECLAIM_ERR_INVALID_ARGUMENT;
    }
    selected = config == NULL ? default_config : *config;
    if (selected.default_swappiness > 200U || selected.page_hash_buckets == 0U ||
        selected.domain_hash_buckets == 0U ||
        selected.pressure.default_priority < selected.pressure.minimum_priority) {
        return RECLAIM_ERR_INVALID_ARGUMENT;
    }
    if (selected.pressure.max_reclaim_rounds == 0U) {
        selected.pressure.max_reclaim_rounds =
            selected.pressure.default_priority - selected.pressure.minimum_priority + 1U;
    }
    engine = platform->allocator->calloc(platform->allocator_context, 1U, sizeof(*engine));
    if (engine == NULL) {
        return RECLAIM_ERR_NO_MEMORY;
    }
    engine->platform = *platform;
    engine->config = selected;
    engine->aging_ops = aging_ops;
    engine->executor_ops = executor_ops;
    engine->executor_context = executor_context;
    engine->pages.bucket_count = selected.page_hash_buckets;
    engine->domains.bucket_count = selected.domain_hash_buckets;
    engine->pages.buckets = reclaim_calloc(engine, engine->pages.bucket_count,
                                           sizeof(*engine->pages.buckets));
    engine->domains.buckets = reclaim_calloc(engine, engine->domains.bucket_count,
                                             sizeof(*engine->domains.buckets));
    if (engine->pages.buckets == NULL || engine->domains.buckets == NULL) {
        reclaim_free(engine, engine->pages.buckets);
        reclaim_free(engine, engine->domains.buckets);
        reclaim_free(engine, engine);
        return RECLAIM_ERR_NO_MEMORY;
    }
    *out_engine = engine;
    return RECLAIM_OK;
}

void reclaim_engine_destroy(struct reclaim_engine *engine)
{
    size_t i;
    struct reclaim_page *page;
    struct reclaim_page *next_page;
    struct reclaim_domain *domain;
    struct reclaim_domain *next_domain;

    if (engine == NULL) {
        return;
    }
    for (i = 0U; i < engine->pages.bucket_count; i++) {
        for (page = engine->pages.buckets[i]; page != NULL; page = next_page) {
            next_page = page->hash_next;
            reclaim_free(engine, page);
        }
    }
    for (i = 0U; i < engine->domains.bucket_count; i++) {
        for (domain = engine->domains.buckets[i]; domain != NULL; domain = next_domain) {
            next_domain = domain->hash_next;
            reclaim_free(engine, domain);
        }
    }
    reclaim_free(engine, engine->pages.buckets);
    reclaim_free(engine, engine->domains.buckets);
    reclaim_free(engine, engine);
}

int reclaim_engine_create_domain(struct reclaim_engine *engine, uint64_t cgroup_id)
{
    struct reclaim_domain *domain;

    if (engine == NULL) return RECLAIM_ERR_INVALID_ARGUMENT;
    if (reclaim_find_domain(engine, cgroup_id) != NULL) {
        return RECLAIM_ERR_DOMAIN_ALREADY_EXISTS;
    }
    domain = reclaim_calloc(engine, 1U, sizeof(*domain));
    if (domain == NULL) return RECLAIM_ERR_NO_MEMORY;
    domain->cgroup_id = cgroup_id;
    domain->config.swappiness = engine->config.default_swappiness;
    domain->config.swap_enabled = engine->config.default_swap_enabled;
    reclaim_list_init(&domain->inactive_anon);
    reclaim_list_init(&domain->active_anon);
    reclaim_list_init(&domain->inactive_file);
    reclaim_list_init(&domain->active_file);
    reclaim_domain_hash_insert(engine, domain);
    reclaim_domain_sorted_insert(engine, domain);
    engine->event_seq++;
    return RECLAIM_OK;
}

int reclaim_engine_destroy_domain(struct reclaim_engine *engine, uint64_t cgroup_id)
{
    struct reclaim_domain *domain;

    if (engine == NULL) return RECLAIM_ERR_INVALID_ARGUMENT;
    domain = reclaim_find_domain(engine, cgroup_id);
    if (domain == NULL) return RECLAIM_ERR_DOMAIN_NOT_FOUND;
    if (domain->stats.nr_folios != 0U || domain->stats.nr_unevictable_folios != 0U) {
        return RECLAIM_ERR_DOMAIN_NOT_EMPTY;
    }
    reclaim_domain_hash_remove(engine, domain);
    reclaim_domain_sorted_remove(engine, domain);
    reclaim_free(engine, domain);
    engine->event_seq++;
    return RECLAIM_OK;
}

int reclaim_engine_set_swappiness(struct reclaim_engine *engine,
                                  uint64_t cgroup_id,
                                  int inherited,
                                  uint32_t swappiness)
{
    struct reclaim_domain *domain;
    if (engine == NULL || swappiness > 200U) return RECLAIM_ERR_INVALID_ARGUMENT;
    domain = reclaim_find_domain(engine, cgroup_id);
    if (domain == NULL) return RECLAIM_ERR_DOMAIN_NOT_FOUND;
    domain->config.override_swappiness = inherited == 0;
    domain->config.swappiness = inherited == 0 ? swappiness : engine->config.default_swappiness;
    engine->event_seq++;
    return RECLAIM_OK;
}

int reclaim_engine_set_swap_enabled(struct reclaim_engine *engine,
                                    uint64_t cgroup_id,
                                    int inherited,
                                    bool enabled)
{
    struct reclaim_domain *domain;
    if (engine == NULL) return RECLAIM_ERR_INVALID_ARGUMENT;
    domain = reclaim_find_domain(engine, cgroup_id);
    if (domain == NULL) return RECLAIM_ERR_DOMAIN_NOT_FOUND;
    domain->config.override_swap_enabled = inherited == 0;
    domain->config.swap_enabled = inherited == 0 ? enabled : engine->config.default_swap_enabled;
    engine->event_seq++;
    return RECLAIM_OK;
}

const struct reclaim_page *reclaim_engine_get_page(const struct reclaim_engine *engine,
                                                   uint64_t page_id)
{
    return engine == NULL ? NULL : reclaim_find_page_const(engine, page_id);
}

int reclaim_engine_get_domain_stats(const struct reclaim_engine *engine,
                                   uint64_t cgroup_id,
                                   struct reclaim_domain_stats *stats)
{
    const struct reclaim_domain *domain;
    if (engine == NULL || stats == NULL) return RECLAIM_ERR_INVALID_ARGUMENT;
    domain = reclaim_find_domain_const(engine, cgroup_id);
    if (domain == NULL) return RECLAIM_ERR_DOMAIN_NOT_FOUND;
    *stats = domain->stats;
    return RECLAIM_OK;
}

void reclaim_engine_get_stats(const struct reclaim_engine *engine,
                              struct reclaim_engine_stats *stats)
{
    if (engine != NULL && stats != NULL) *stats = engine->stats;
}

uint64_t reclaim_engine_event_seq(const struct reclaim_engine *engine)
{
    return engine == NULL ? 0U : engine->event_seq;
}
