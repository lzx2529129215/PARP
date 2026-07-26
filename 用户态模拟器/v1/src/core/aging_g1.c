#include "internal.h"

static int age_domain(struct reclaim_engine *engine, struct reclaim_domain *domain)
{
    enum reclaim_lru_kind kinds[] = {
        RECLAIM_LRU_ACTIVE_ANON,
        RECLAIM_LRU_INACTIVE_ANON,
        RECLAIM_LRU_ACTIVE_FILE,
        RECLAIM_LRU_INACTIVE_FILE,
    };
    size_t kind_index;

    for (kind_index = 0U; kind_index < sizeof(kinds) / sizeof(kinds[0]); kind_index++) {
        struct reclaim_list *list = reclaim_domain_lru(domain, kinds[kind_index]);
        struct reclaim_list_node *node = list->head.next;
        while (node != &list->head) {
            struct reclaim_list_node *next = node->next;
            struct reclaim_page *page = node->owner;
            enum reclaim_lru_kind destination = page->lru_kind;
            bool move = false;

            if (page->referenced) {
                destination = reclaim_lru_is_active(page->lru_kind) ?
                    page->lru_kind :
                    (reclaim_lru_is_anon(page->lru_kind) ?
                         RECLAIM_LRU_ACTIVE_ANON : RECLAIM_LRU_ACTIVE_FILE);
                move = true;
            } else if (reclaim_lru_is_active(page->lru_kind)) {
                destination = reclaim_lru_is_anon(page->lru_kind) ?
                    RECLAIM_LRU_INACTIVE_ANON : RECLAIM_LRU_INACTIVE_FILE;
                move = true;
            }
            if (move) {
                reclaim_unlink_page(engine, page, domain);
                reclaim_link_page(engine, page, domain, destination, RECLAIM_PAGE_ON_LRU);
            }
            page->referenced = false;
            page->last_age_seq = engine->event_seq;
            node = next;
        }
    }
    return RECLAIM_OK;
}

static int age_group(void *context, struct reclaim_engine *engine, uint64_t cgroup_id)
{
    struct reclaim_domain *domain;
    (void)context;
    if (engine == NULL) return RECLAIM_ERR_INVALID_ARGUMENT;
    domain = reclaim_find_domain(engine, cgroup_id);
    if (domain == NULL) return RECLAIM_ERR_DOMAIN_NOT_FOUND;
    engine->event_seq++;
    return age_domain(engine, domain);
}

static int age_all(void *context, struct reclaim_engine *engine)
{
    struct reclaim_domain *domain;
    (void)context;
    if (engine == NULL) return RECLAIM_ERR_INVALID_ARGUMENT;
    engine->event_seq++;
    for (domain = engine->domains.sorted_head; domain != NULL; domain = domain->sorted_next) {
        int result = age_domain(engine, domain);
        if (result != RECLAIM_OK) return result;
    }
    return RECLAIM_OK;
}

static const struct reclaim_aging_ops g1_ops = {
    .age_group = age_group,
    .age_all = age_all,
    .context = NULL,
};

const struct reclaim_aging_ops *reclaim_g1_aging_ops(void)
{
    return &g1_ops;
}

int reclaim_engine_age_group(struct reclaim_engine *engine, uint64_t cgroup_id)
{
    if (engine == NULL || engine->aging_ops == NULL || engine->aging_ops->age_group == NULL) {
        return RECLAIM_ERR_NOT_SUPPORTED;
    }
    return engine->aging_ops->age_group(engine->aging_ops->context, engine, cgroup_id);
}

int reclaim_engine_age_all(struct reclaim_engine *engine)
{
    if (engine == NULL || engine->aging_ops == NULL || engine->aging_ops->age_all == NULL) {
        return RECLAIM_ERR_NOT_SUPPORTED;
    }
    return engine->aging_ops->age_all(engine->aging_ops->context, engine);
}
