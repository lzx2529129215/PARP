#include "myself_kswapd/types.h"

#include <stddef.h>

void reclaim_list_init(struct reclaim_list *list)
{
    list->head.prev = &list->head;
    list->head.next = &list->head;
    list->head.owner = NULL;
    list->head.list = list;
    list->nr_folios = 0U;
    list->nr_base_pages = 0U;
}

bool reclaim_list_empty(const struct reclaim_list *list)
{
    return list->head.next == &list->head;
}

static void link_between(struct reclaim_list_node *previous,
                         struct reclaim_list_node *next,
                         struct reclaim_list_node *node,
                         struct reclaim_list *list)
{
    node->prev = previous;
    node->next = next;
    node->list = list;
    previous->next = node;
    next->prev = node;
    list->nr_folios++;
}

void reclaim_list_push_front(struct reclaim_list *list, struct reclaim_list_node *node)
{
    link_between(&list->head, list->head.next, node, list);
}

void reclaim_list_push_back(struct reclaim_list *list, struct reclaim_list_node *node)
{
    link_between(list->head.prev, &list->head, node, list);
}

void reclaim_list_remove(struct reclaim_list *list, struct reclaim_list_node *node)
{
    if (node->list != list) {
        return;
    }
    node->prev->next = node->next;
    node->next->prev = node->prev;
    node->prev = NULL;
    node->next = NULL;
    node->list = NULL;
    if (list->nr_folios > 0U) {
        list->nr_folios--;
    }
}

void reclaim_list_move_back(struct reclaim_list *list, struct reclaim_list_node *node)
{
    if (node->list != list) {
        return;
    }
    if (list->head.prev == node) {
        return;
    }
    node->prev->next = node->next;
    node->next->prev = node->prev;
    node->prev = list->head.prev;
    node->next = &list->head;
    list->head.prev->next = node;
    list->head.prev = node;
}

struct reclaim_list_node *reclaim_list_front(const struct reclaim_list *list)
{
    return reclaim_list_empty(list) ? NULL : list->head.next;
}

struct reclaim_list_node *reclaim_list_back(const struct reclaim_list *list)
{
    return reclaim_list_empty(list) ? NULL : list->head.prev;
}
