#include "myself_kswapd/event.h"

#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static int parse_error(const char *filename,
                       size_t line,
                       const char *raw,
                       const char *reason,
                       char *message,
                       size_t message_size)
{
    if (message != NULL && message_size > 0U) {
        (void)snprintf(message, message_size, "%s:%zu: %s: %s",
                       filename == NULL ? "<trace>" : filename, line,
                       raw == NULL ? "" : raw, reason);
    }
    return RECLAIM_ERR_PARSE;
}

static int number(const char *token, uint64_t *value)
{
    char *end;
    unsigned long long parsed;
    if (token == NULL || token[0] == '-' || token[0] == '\0') return -1;
    errno = 0;
    parsed = strtoull(token, &end, 10);
    if (errno != 0 || end == token || *end != '\0') return -1;
    *value = (uint64_t)parsed;
    return 0;
}

static int bounded_number(const char *token, uint32_t max, uint32_t *value)
{
    uint64_t parsed;
    if (number(token, &parsed) != 0 || parsed > max) return -1;
    *value = (uint32_t)parsed;
    return 0;
}

static int finish(void)
{
    return strtok(NULL, " \t\r\n") == NULL ? 0 : -1;
}

static int page_type(const char *token, enum reclaim_page_type *type)
{
    if (token == NULL) return -1;
    if (strcmp(token, "ANON") == 0) *type = RECLAIM_PAGE_ANON;
    else if (strcmp(token, "FILE") == 0) *type = RECLAIM_PAGE_FILE;
    else return -1;
    return 0;
}

static int page_state(const char *token, enum reclaim_page_state *state)
{
    if (token == NULL) return -1;
    if (strcmp(token, "NEW") == 0) *state = RECLAIM_PAGE_NEW;
    else if (strcmp(token, "ON_LRU") == 0) *state = RECLAIM_PAGE_ON_LRU;
    else if (strcmp(token, "ISOLATED") == 0) *state = RECLAIM_PAGE_ISOLATED;
    else if (strcmp(token, "UNEVICTABLE") == 0) *state = RECLAIM_PAGE_UNEVICTABLE;
    else return -1;
    return 0;
}

static int lru_kind(const char *token, enum reclaim_lru_kind *kind)
{
    if (token == NULL) return -1;
    if (strcmp(token, "NONE") == 0) *kind = RECLAIM_LRU_NONE;
    else if (strcmp(token, "INACTIVE_ANON") == 0) *kind = RECLAIM_LRU_INACTIVE_ANON;
    else if (strcmp(token, "ACTIVE_ANON") == 0) *kind = RECLAIM_LRU_ACTIVE_ANON;
    else if (strcmp(token, "INACTIVE_FILE") == 0) *kind = RECLAIM_LRU_INACTIVE_FILE;
    else if (strcmp(token, "ACTIVE_FILE") == 0) *kind = RECLAIM_LRU_ACTIVE_FILE;
    else return -1;
    return 0;
}

static int outcome(const char *token, enum reclaim_sim_outcome *value)
{
    enum reclaim_sim_outcome candidate;
    if (token == NULL) return -1;
    for (candidate = RECLAIM_SIM_SUCCESS; candidate <= RECLAIM_SIM_UNEVICTABLE; candidate++) {
        if (strcmp(token, reclaim_sim_outcome_name(candidate)) == 0) {
            *value = candidate;
            return 0;
        }
    }
    return -1;
}

static int stop_reason(const char *token, enum reclaim_stop_reason *reason)
{
    static const char *const names[] = {
        "TARGET_REACHED", "NO_SCANNABLE_PAGES", "NO_PROGRESS",
        "PRIORITY_EXHAUSTED", "EXECUTOR_ERROR", "ROUND_LIMIT",
    };
    size_t i;
    if (token == NULL) return -1;
    for (i = 0U; i < sizeof(names) / sizeof(names[0]); i++) {
        if (strcmp(token, names[i]) == 0) {
            *reason = (enum reclaim_stop_reason)i;
            return 0;
        }
    }
    return -1;
}

int reclaim_event_parse(const char *filename,
                        size_t line_number,
                        const char *text,
                        struct reclaim_event *event,
                        char *error_message,
                        size_t error_message_size)
{
    char work[256];
    char raw[256];
    char *command;
    char *token;
    uint64_t first;

    if (text == NULL || event == NULL) {
        return parse_error(filename, line_number, text, "invalid parser argument",
                           error_message, error_message_size);
    }
    (void)snprintf(raw, sizeof(raw), "%s", text);
    raw[strcspn(raw, "\r\n")] = '\0';
    (void)snprintf(work, sizeof(work), "%s", text);
    token = strchr(work, '#');
    if (token != NULL) *token = '\0';
    command = strtok(work, " \t\r\n");
    if (command == NULL) return RECLAIM_ERR_PARSE;
    *event = (struct reclaim_event){.line_number = line_number};
    (void)snprintf(event->raw, sizeof(event->raw), "%s", raw);

    if (strcmp(command, "GROUP_CREATE") == 0 || strcmp(command, "GROUP_DESTROY") == 0 ||
        strcmp(command, "AGE_GROUP") == 0) {
        token = strtok(NULL, " \t\r\n");
        if (number(token, &first) != 0 || finish() != 0) goto invalid;
        event->args.group.cgroup_id = first;
        event->type = strcmp(command, "GROUP_CREATE") == 0 ? RECLAIM_EVENT_GROUP_CREATE :
            strcmp(command, "GROUP_DESTROY") == 0 ? RECLAIM_EVENT_GROUP_DESTROY :
            RECLAIM_EVENT_AGE_GROUP;
        return RECLAIM_OK;
    }
    if (strcmp(command, "GROUP_SET_SWAPPINESS") == 0) {
        token = strtok(NULL, " \t\r\n");
        if (number(token, &first) != 0) goto invalid;
        event->args.swappiness.cgroup_id = first;
        token = strtok(NULL, " \t\r\n");
        if (token == NULL) goto invalid;
        if (strcmp(token, "INHERIT") == 0) {
            event->args.swappiness.inherited = 1;
        } else if (bounded_number(token, 200U, &event->args.swappiness.value) == 0) {
            event->args.swappiness.inherited = 0;
        } else goto invalid;
        if (finish() != 0) goto invalid;
        event->type = RECLAIM_EVENT_GROUP_SET_SWAPPINESS;
        return RECLAIM_OK;
    }
    if (strcmp(command, "GROUP_SET_SWAP_ENABLED") == 0 || strcmp(command, "GROUP_SET_SWAP") == 0) {
        token = strtok(NULL, " \t\r\n");
        if (number(token, &first) != 0) goto invalid;
        event->args.swap.cgroup_id = first;
        token = strtok(NULL, " \t\r\n");
        if (token == NULL) goto invalid;
        if (strcmp(token, "INHERIT") == 0) event->args.swap.inherited = 1;
        else if (strcmp(token, "ON") == 0 || strcmp(token, "1") == 0) {
            event->args.swap.inherited = 0; event->args.swap.enabled = true;
        } else if (strcmp(token, "OFF") == 0 || strcmp(token, "0") == 0) {
            event->args.swap.inherited = 0; event->args.swap.enabled = false;
        } else goto invalid;
        if (finish() != 0) goto invalid;
        event->type = RECLAIM_EVENT_GROUP_SET_SWAP_ENABLED;
        return RECLAIM_OK;
    }
    if (strcmp(command, "PAGE_ADD") == 0) {
        token = strtok(NULL, " \t\r\n");
        if (number(token, &event->args.add.page_id) != 0) goto invalid;
        token = strtok(NULL, " \t\r\n");
        if (number(token, &event->args.add.cgroup_id) != 0) goto invalid;
        if (page_type(strtok(NULL, " \t\r\n"), &event->args.add.type) != 0) goto invalid;
        if (bounded_number(strtok(NULL, " \t\r\n"), 63U, &event->args.add.order) != 0 ||
            finish() != 0) goto invalid;
        event->type = RECLAIM_EVENT_PAGE_ADD;
        return RECLAIM_OK;
    }
    if (strcmp(command, "PAGE_ACCESS") == 0 || strcmp(command, "PAGE_RECHARGE") == 0) {
        token = strtok(NULL, " \t\r\n");
        if (number(token, &event->args.page_cgroup.page_id) != 0) goto invalid;
        if (number(strtok(NULL, " \t\r\n"), &event->args.page_cgroup.cgroup_id) != 0 ||
            finish() != 0) goto invalid;
        event->type = strcmp(command, "PAGE_ACCESS") == 0 ? RECLAIM_EVENT_PAGE_ACCESS :
                                                     RECLAIM_EVENT_PAGE_RECHARGE;
        return RECLAIM_OK;
    }
    if (strcmp(command, "PAGE_REMOVE") == 0) {
        if (number(strtok(NULL, " \t\r\n"), &event->args.page_cgroup.page_id) != 0 ||
            finish() != 0) goto invalid;
        event->type = RECLAIM_EVENT_PAGE_REMOVE;
        return RECLAIM_OK;
    }
    if (strcmp(command, "PAGE_MIGRATE") == 0) {
        if (number(strtok(NULL, " \t\r\n"), &event->args.migrate.old_page_id) != 0 ||
            number(strtok(NULL, " \t\r\n"), &event->args.migrate.new_page_id) != 0 ||
            finish() != 0) goto invalid;
        event->type = RECLAIM_EVENT_PAGE_MIGRATE;
        return RECLAIM_OK;
    }
    if (strcmp(command, "PAGE_EXEC_OUTCOME") == 0) {
        if (number(strtok(NULL, " \t\r\n"), &event->args.outcome.page_id) != 0 ||
            outcome(strtok(NULL, " \t\r\n"), &event->args.outcome.outcome) != 0 ||
            finish() != 0) goto invalid;
        event->type = RECLAIM_EVENT_PAGE_EXEC_OUTCOME;
        return RECLAIM_OK;
    }
    if (strcmp(command, "AGE_ALL") == 0) {
        if (finish() != 0) goto invalid;
        event->type = RECLAIM_EVENT_AGE_ALL;
        return RECLAIM_OK;
    }
    if (strcmp(command, "RECLAIM_GROUP") == 0) {
        if (number(strtok(NULL, " \t\r\n"), &event->args.reclaim_group.cgroup_id) != 0 ||
            number(strtok(NULL, " \t\r\n"), &event->args.reclaim_group.target_pages) != 0 ||
            finish() != 0) goto invalid;
        event->type = RECLAIM_EVENT_RECLAIM_GROUP;
        return RECLAIM_OK;
    }
    if (strcmp(command, "RECLAIM_ALL") == 0) {
        if (number(strtok(NULL, " \t\r\n"), &event->args.reclaim_all.target_pages) != 0 ||
            finish() != 0) goto invalid;
        event->type = RECLAIM_EVENT_RECLAIM_ALL;
        return RECLAIM_OK;
    }
    if (strcmp(command, "VALIDATE") == 0 || strcmp(command, "DUMP") == 0) {
        if (finish() != 0) goto invalid;
        event->type = strcmp(command, "VALIDATE") == 0 ? RECLAIM_EVENT_VALIDATE : RECLAIM_EVENT_DUMP;
        return RECLAIM_OK;
    }
    if (strcmp(command, "ASSERT_PAGE_MISSING") == 0) {
        if (number(strtok(NULL, " \t\r\n"), &event->args.page_cgroup.page_id) != 0 ||
            finish() != 0) goto invalid;
        event->type = RECLAIM_EVENT_ASSERT_PAGE_MISSING;
        return RECLAIM_OK;
    }
    if (strcmp(command, "ASSERT_PAGE_STATE") == 0) {
        if (number(strtok(NULL, " \t\r\n"), &event->args.assert_state.page_id) != 0 ||
            page_state(strtok(NULL, " \t\r\n"), &event->args.assert_state.state) != 0 ||
            finish() != 0) goto invalid;
        event->type = RECLAIM_EVENT_ASSERT_PAGE_STATE;
        return RECLAIM_OK;
    }
    if (strcmp(command, "ASSERT_PAGE_LRU") == 0) {
        if (number(strtok(NULL, " \t\r\n"), &event->args.assert_lru.page_id) != 0 ||
            lru_kind(strtok(NULL, " \t\r\n"), &event->args.assert_lru.kind) != 0 ||
            finish() != 0) goto invalid;
        event->type = RECLAIM_EVENT_ASSERT_PAGE_LRU;
        return RECLAIM_OK;
    }
    if (strcmp(command, "ASSERT_DOMAIN_PAGES") == 0) {
        if (number(strtok(NULL, " \t\r\n"), &event->args.assert_domain.cgroup_id) != 0 ||
            number(strtok(NULL, " \t\r\n"), &event->args.assert_domain.base_pages) != 0 ||
            finish() != 0) goto invalid;
        event->type = RECLAIM_EVENT_ASSERT_DOMAIN_PAGES;
        return RECLAIM_OK;
    }
    if (strcmp(command, "ASSERT_LAST_STOP_REASON") == 0) {
        if (stop_reason(strtok(NULL, " \t\r\n"), &event->args.assert_stop.reason) != 0 ||
            finish() != 0) goto invalid;
        event->type = RECLAIM_EVENT_ASSERT_LAST_STOP_REASON;
        return RECLAIM_OK;
    }

invalid:
    return parse_error(filename, line_number, raw, "invalid event syntax", error_message,
                       error_message_size);
}
