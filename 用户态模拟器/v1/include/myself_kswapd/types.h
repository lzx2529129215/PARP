#ifndef MYSELF_KSWAPD_TYPES_H
#define MYSELF_KSWAPD_TYPES_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

enum reclaim_page_type {
    RECLAIM_PAGE_ANON = 0,
    RECLAIM_PAGE_FILE = 1
};

enum reclaim_page_state {
    RECLAIM_PAGE_NEW = 0,
    RECLAIM_PAGE_ON_LRU = 1,
    RECLAIM_PAGE_ISOLATED = 2,
    RECLAIM_PAGE_UNEVICTABLE = 3
};

enum reclaim_lru_kind {
    RECLAIM_LRU_NONE = 0,
    RECLAIM_LRU_INACTIVE_ANON = 1,
    RECLAIM_LRU_ACTIVE_ANON = 2,
    RECLAIM_LRU_INACTIVE_FILE = 3,
    RECLAIM_LRU_ACTIVE_FILE = 4
};

enum reclaim_sim_outcome {
    RECLAIM_SIM_SUCCESS = 0,
    RECLAIM_SIM_PUTBACK = 1,
    RECLAIM_SIM_ACTIVATE = 2,
    RECLAIM_SIM_BUSY = 3,
    RECLAIM_SIM_DIRTY = 4,
    RECLAIM_SIM_WRITEBACK = 5,
    RECLAIM_SIM_UNEVICTABLE = 6
};

struct reclaim_list_node {
    struct reclaim_list_node *prev;
    struct reclaim_list_node *next;
    void *owner;
    void *list;
};

struct reclaim_list {
    struct reclaim_list_node head;
    uint64_t nr_folios;
    uint64_t nr_base_pages;
};

struct reclaim_page {
    uint64_t page_id;
    uint64_t charge_cgroup_id;
    uint64_t last_access_cgroup_id;
    enum reclaim_page_type type;
    enum reclaim_page_state state;
    enum reclaim_lru_kind lru_kind;
    uint32_t order;
    uint32_t flags;
    uint64_t last_access_seq;
    uint64_t last_age_seq;
    uint32_t access_count;
    bool referenced;
    bool shared;
    enum reclaim_sim_outcome next_sim_outcome;
    struct reclaim_list_node lru_node;
    struct reclaim_page *hash_next;
};

const char *reclaim_page_type_name(enum reclaim_page_type type);
const char *reclaim_page_state_name(enum reclaim_page_state state);
const char *reclaim_lru_kind_name(enum reclaim_lru_kind kind);
const char *reclaim_sim_outcome_name(enum reclaim_sim_outcome outcome);
int reclaim_folio_base_pages(uint32_t order, uint64_t *pages);
bool reclaim_lru_is_active(enum reclaim_lru_kind kind);
bool reclaim_lru_is_anon(enum reclaim_lru_kind kind);
bool reclaim_lru_is_file(enum reclaim_lru_kind kind);

void reclaim_list_init(struct reclaim_list *list);
bool reclaim_list_empty(const struct reclaim_list *list);
void reclaim_list_push_front(struct reclaim_list *list, struct reclaim_list_node *node);
void reclaim_list_push_back(struct reclaim_list *list, struct reclaim_list_node *node);
void reclaim_list_remove(struct reclaim_list *list, struct reclaim_list_node *node);
void reclaim_list_move_back(struct reclaim_list *list, struct reclaim_list_node *node);
struct reclaim_list_node *reclaim_list_front(const struct reclaim_list *list);
struct reclaim_list_node *reclaim_list_back(const struct reclaim_list *list);

#endif
