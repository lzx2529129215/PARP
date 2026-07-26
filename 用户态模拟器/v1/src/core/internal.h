#ifndef RECLAIM_CORE_INTERNAL_H
#define RECLAIM_CORE_INTERNAL_H

#include "myself_kswapd/engine.h"
#include "myself_kswapd/executor.h"

struct reclaim_page_table {
    struct reclaim_page **buckets;
    size_t bucket_count;
};

struct reclaim_domain {
    uint64_t cgroup_id;
    struct reclaim_list inactive_anon;
    struct reclaim_list active_anon;
    struct reclaim_list inactive_file;
    struct reclaim_list active_file;
    struct reclaim_domain_config config;
    struct reclaim_domain_stats stats;
    struct reclaim_domain *hash_next;
    struct reclaim_domain *sorted_prev;
    struct reclaim_domain *sorted_next;
};

struct reclaim_domain_table {
    struct reclaim_domain **buckets;
    size_t bucket_count;
    struct reclaim_domain *sorted_head;
};

struct reclaim_engine {
    struct reclaim_platform platform;
    struct reclaim_engine_config config;
    struct reclaim_page_table pages;
    struct reclaim_domain_table domains;
    const struct reclaim_aging_ops *aging_ops;
    const struct reclaim_executor_ops *executor_ops;
    void *executor_context;
    struct reclaim_engine_stats stats;
    uint64_t event_seq;
};

void *reclaim_alloc(struct reclaim_engine *engine, size_t size);
void *reclaim_calloc(struct reclaim_engine *engine, size_t count, size_t size);
void reclaim_free(struct reclaim_engine *engine, void *pointer);
size_t reclaim_page_bucket(const struct reclaim_engine *engine, uint64_t page_id);
size_t reclaim_domain_bucket(const struct reclaim_engine *engine, uint64_t cgroup_id);
struct reclaim_page *reclaim_find_page(struct reclaim_engine *engine, uint64_t page_id);
const struct reclaim_page *reclaim_find_page_const(const struct reclaim_engine *engine,
                                                   uint64_t page_id);
struct reclaim_domain *reclaim_find_domain(struct reclaim_engine *engine, uint64_t cgroup_id);
const struct reclaim_domain *reclaim_find_domain_const(const struct reclaim_engine *engine,
                                                       uint64_t cgroup_id);
struct reclaim_list *reclaim_domain_lru(struct reclaim_domain *domain,
                                        enum reclaim_lru_kind kind);
const struct reclaim_list *reclaim_domain_lru_const(const struct reclaim_domain *domain,
                                                    enum reclaim_lru_kind kind);
enum reclaim_lru_kind reclaim_initial_lru(enum reclaim_page_type type);
void reclaim_account_add(struct reclaim_engine *engine,
                         struct reclaim_domain *domain,
                         struct reclaim_page *page,
                         enum reclaim_lru_kind kind);
void reclaim_account_remove(struct reclaim_engine *engine,
                            struct reclaim_domain *domain,
                            struct reclaim_page *page,
                            enum reclaim_lru_kind kind);
int reclaim_link_page(struct reclaim_engine *engine,
                      struct reclaim_page *page,
                      struct reclaim_domain *domain,
                      enum reclaim_lru_kind kind,
                      enum reclaim_page_state state);
void reclaim_unlink_page(struct reclaim_engine *engine,
                         struct reclaim_page *page,
                         struct reclaim_domain *domain);
void reclaim_page_hash_insert(struct reclaim_engine *engine, struct reclaim_page *page);
void reclaim_page_hash_remove(struct reclaim_engine *engine, struct reclaim_page *page);
void reclaim_domain_hash_insert(struct reclaim_engine *engine, struct reclaim_domain *domain);
void reclaim_domain_hash_remove(struct reclaim_engine *engine, struct reclaim_domain *domain);
void reclaim_domain_sorted_insert(struct reclaim_engine *engine, struct reclaim_domain *domain);
void reclaim_domain_sorted_remove(struct reclaim_engine *engine, struct reclaim_domain *domain);

#endif
