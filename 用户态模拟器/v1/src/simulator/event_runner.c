#include "myself_kswapd/event.h"

#include <stdio.h>
#include <string.h>

static int validate_now(struct reclaim_engine *engine)
{
    struct reclaim_validation_report report;
    return reclaim_engine_validate(engine, &report);
}

int reclaim_event_apply(struct reclaim_engine *engine,
                        const struct reclaim_event *event,
                        struct reclaim_trace_state *state)
{
    struct reclaim_domain_stats domain_stats;
    const struct reclaim_page *page;
    struct reclaim_validation_report report;
    int error;

    if (engine == NULL || event == NULL) return RECLAIM_ERR_INVALID_ARGUMENT;
    switch (event->type) {
    case RECLAIM_EVENT_GROUP_CREATE:
        return reclaim_engine_create_domain(engine, event->args.group.cgroup_id);
    case RECLAIM_EVENT_GROUP_DESTROY:
        return reclaim_engine_destroy_domain(engine, event->args.group.cgroup_id);
    case RECLAIM_EVENT_GROUP_SET_SWAPPINESS:
        return reclaim_engine_set_swappiness(engine, event->args.swappiness.cgroup_id,
                                             event->args.swappiness.inherited,
                                             event->args.swappiness.value);
    case RECLAIM_EVENT_GROUP_SET_SWAP_ENABLED:
        return reclaim_engine_set_swap_enabled(engine, event->args.swap.cgroup_id,
                                               event->args.swap.inherited,
                                               event->args.swap.enabled);
    case RECLAIM_EVENT_PAGE_ADD:
        return reclaim_engine_add_page(engine, event->args.add.page_id,
                                       event->args.add.cgroup_id, event->args.add.type,
                                       event->args.add.order);
    case RECLAIM_EVENT_PAGE_ACCESS:
        return reclaim_engine_access_page(engine, event->args.page_cgroup.page_id,
                                          event->args.page_cgroup.cgroup_id);
    case RECLAIM_EVENT_PAGE_REMOVE:
        return reclaim_engine_remove_page(engine, event->args.page_cgroup.page_id);
    case RECLAIM_EVENT_PAGE_RECHARGE:
        return reclaim_engine_recharge_page(engine, event->args.page_cgroup.page_id,
                                            event->args.page_cgroup.cgroup_id);
    case RECLAIM_EVENT_PAGE_MIGRATE:
        return reclaim_engine_migrate_page(engine, event->args.migrate.old_page_id,
                                           event->args.migrate.new_page_id);
    case RECLAIM_EVENT_PAGE_EXEC_OUTCOME:
        return reclaim_engine_set_page_outcome(engine, event->args.outcome.page_id,
                                               event->args.outcome.outcome);
    case RECLAIM_EVENT_AGE_GROUP:
        return reclaim_engine_age_group(engine, event->args.group.cgroup_id);
    case RECLAIM_EVENT_AGE_ALL:
        return reclaim_engine_age_all(engine);
    case RECLAIM_EVENT_RECLAIM_GROUP:
        error = reclaim_engine_reclaim_group(engine, event->args.reclaim_group.cgroup_id,
                                             event->args.reclaim_group.target_pages,
                                             state == NULL ? NULL : &state->last_result);
        if (state != NULL && error == RECLAIM_OK) state->has_last_result = true;
        return error;
    case RECLAIM_EVENT_RECLAIM_ALL:
        error = reclaim_engine_reclaim_all(engine, event->args.reclaim_all.target_pages,
                                           state == NULL ? NULL : &state->last_result);
        if (state != NULL && error == RECLAIM_OK) state->has_last_result = true;
        return error;
    case RECLAIM_EVENT_VALIDATE:
        return reclaim_engine_validate(engine, &report);
    case RECLAIM_EVENT_DUMP:
        return RECLAIM_OK;
    case RECLAIM_EVENT_ASSERT_PAGE_MISSING:
        return reclaim_engine_get_page(engine, event->args.page_cgroup.page_id) == NULL ?
            RECLAIM_OK : RECLAIM_ERR_VALIDATION;
    case RECLAIM_EVENT_ASSERT_PAGE_STATE:
        page = reclaim_engine_get_page(engine, event->args.assert_state.page_id);
        return page != NULL && page->state == event->args.assert_state.state ?
            RECLAIM_OK : RECLAIM_ERR_VALIDATION;
    case RECLAIM_EVENT_ASSERT_PAGE_LRU:
        page = reclaim_engine_get_page(engine, event->args.assert_lru.page_id);
        return page != NULL && page->lru_kind == event->args.assert_lru.kind ?
            RECLAIM_OK : RECLAIM_ERR_VALIDATION;
    case RECLAIM_EVENT_ASSERT_DOMAIN_PAGES:
        error = reclaim_engine_get_domain_stats(engine, event->args.assert_domain.cgroup_id,
                                                &domain_stats);
        return error != RECLAIM_OK ? error :
            domain_stats.nr_base_pages == event->args.assert_domain.base_pages ?
                RECLAIM_OK : RECLAIM_ERR_VALIDATION;
    case RECLAIM_EVENT_ASSERT_LAST_STOP_REASON:
        return state != NULL && state->has_last_result &&
               state->last_result.stop_reason == event->args.assert_stop.reason ?
            RECLAIM_OK : RECLAIM_ERR_VALIDATION;
    default:
        return RECLAIM_ERR_INVALID_ARGUMENT;
    }
}

static int is_ignorable(const char *line)
{
    while (*line == ' ' || *line == '\t' || *line == '\r' || *line == '\n') line++;
    return *line == '\0' || *line == '#';
}

int reclaim_trace_run(struct reclaim_engine *engine,
                      const char *filename,
                      FILE *input,
                      bool validate_each_event,
                      bool validate_at_end,
                      reclaim_output_fn output,
                      void *output_context,
                      size_t *failed_line)
{
    char line[256];
    char diagnostic[512];
    size_t line_number = 0U;
    struct reclaim_trace_state state = {0};
    if (engine == NULL || input == NULL) return RECLAIM_ERR_INVALID_ARGUMENT;
    while (fgets(line, sizeof(line), input) != NULL) {
        struct reclaim_event event;
        int error;
        line_number++;
        if (is_ignorable(line)) continue;
        error = reclaim_event_parse(filename, line_number, line, &event,
                                    diagnostic, sizeof(diagnostic));
        if (error != RECLAIM_OK) {
            if (output != NULL) output(output_context, diagnostic);
            if (failed_line != NULL) *failed_line = line_number;
            return error;
        }
        error = reclaim_event_apply(engine, &event, &state);
        if (error != RECLAIM_OK) {
            (void)snprintf(diagnostic, sizeof(diagnostic), "%s:%zu: %s: %s",
                           filename == NULL ? "<trace>" : filename, line_number,
                           event.raw, reclaim_error_name((enum reclaim_error)error));
            if (output != NULL) output(output_context, diagnostic);
            if (failed_line != NULL) *failed_line = line_number;
            return error;
        }
        if (event.type == RECLAIM_EVENT_DUMP && output != NULL) {
            (void)reclaim_engine_dump(engine, output, output_context);
        }
        if (validate_each_event && validate_now(engine) != RECLAIM_OK) {
            (void)snprintf(diagnostic, sizeof(diagnostic), "%s:%zu: validator failure",
                           filename == NULL ? "<trace>" : filename, line_number);
            if (output != NULL) output(output_context, diagnostic);
            if (failed_line != NULL) *failed_line = line_number;
            return RECLAIM_ERR_VALIDATION;
        }
    }
    if (validate_at_end && validate_now(engine) != RECLAIM_OK) return RECLAIM_ERR_VALIDATION;
    return RECLAIM_OK;
}
