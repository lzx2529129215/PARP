/* SPDX-License-Identifier: GPL-2.0 */
#include "get_wss_predictor.h"

struct wss_lookup_row {
    wss_pred_u32 list_num;
    wss_pred_u32 pressure_gib;
    wss_pred_u32 op_id;
    wss_pred_u64 wss;
    wss_pred_u64 pgfault;
    unsigned int valid_mask;
};

#ifndef WSS_PREDICTOR_DATA_HEADER
#define WSS_PREDICTOR_DATA_HEADER "gimp_lookup_data.h"
#endif
#include WSS_PREDICTOR_DATA_HEADER

struct wss_app_table {
    const char *name;
    const struct wss_lookup_row *rows;
    unsigned int count;
    wss_pred_u32 max_operation;
};

/* Add a generated app-specific table and one registry entry for a new app. */
static const struct wss_app_table app_tables[] = {
    {"gimp", gimp_lookup_rows, GIMP_LOOKUP_RECORD_COUNT, GIMP_LOOKUP_MAX_OPERATION}
};

static int same_app_name(const char *left, const char *right)
{
    while (*left && *right) {
        unsigned char a = (unsigned char)*left++;
        unsigned char b = (unsigned char)*right++;
        if (a >= 'A' && a <= 'Z')
            a = (unsigned char)(a + ('a' - 'A'));
        if (b >= 'A' && b <= 'Z')
            b = (unsigned char)(b + ('a' - 'A'));
        if (a != b)
            return 0;
    }
    return *left == *right;
}

static const struct wss_app_table *find_app(const char *name)
{
    unsigned int i;
    for (i = 0; i < sizeof(app_tables) / sizeof(app_tables[0]); ++i) {
        if (same_app_name(name, app_tables[i].name))
            return &app_tables[i];
    }
    return (const struct wss_app_table *)0;
}

static const struct wss_lookup_row *find_row(const struct wss_app_table *table,
                                            wss_pred_u32 seq,
                                            wss_pred_u32 pressure,
                                            wss_pred_u32 op)
{
    unsigned int low = 0;
    unsigned int high = table->count;

    while (low < high) {
        unsigned int mid = low + (high - low) / 2;
        const struct wss_lookup_row *row = &table->rows[mid];
        if (row->list_num == seq && row->pressure_gib == pressure && row->op_id == op)
            return row;
        if (row->list_num < seq ||
            (row->list_num == seq && row->pressure_gib < pressure) ||
            (row->list_num == seq && row->pressure_gib == pressure && row->op_id < op))
            low = mid + 1;
        else
            high = mid;
    }
    return (const struct wss_lookup_row *)0;
}

int get_wss_predictor(const char *app_name, wss_pred_u32 sequence_num,
                     wss_pred_u32 current_op_id, wss_pred_u64 pressure_gib,
                     wss_pred_i64 wss_bytes[WSS_PREDICTOR_HORIZON],
                     wss_pred_i64 pagefault[WSS_PREDICTOR_HORIZON])
{
    const struct wss_lookup_row *future;
    wss_pred_i64 future_wss[WSS_PREDICTOR_HORIZON];
    wss_pred_i64 future_fault[WSS_PREDICTOR_HORIZON];
    const struct wss_app_table *table;
    unsigned int i;
    unsigned int j;

    if (!app_name || !*app_name || !wss_bytes || !pagefault ||
        wss_bytes == pagefault || !sequence_num || !current_op_id ||
        (pressure_gib != 0 && pressure_gib != 8 && pressure_gib != 12 && pressure_gib != 14))
        return WSS_PRED_INVALID_ARGUMENT;
    for (i = 0; i < WSS_PREDICTOR_HORIZON; ++i) {
        for (j = 0; j < WSS_PREDICTOR_HORIZON; ++j) {
            if (wss_bytes + i == pagefault + j)
                return WSS_PRED_INVALID_ARGUMENT;
        }
    }
    table = find_app(app_name);
    if (!table)
        return WSS_PRED_UNSUPPORTED_APP;
    if (current_op_id > table->max_operation)
        return WSS_PRED_INVALID_ARGUMENT;
    if (!find_row(table, sequence_num, (wss_pred_u32)pressure_gib, current_op_id))
        return WSS_PRED_NOT_FOUND;
    for (i = 0; i < WSS_PREDICTOR_HORIZON; ++i) {
        if (table->max_operation - current_op_id <= i) {
            future_wss[i] = WSS_PRED_NO_VALUE;
            future_fault[i] = WSS_PRED_NO_VALUE;
            continue;
        }
        future = find_row(table, sequence_num, (wss_pred_u32)pressure_gib,
                          current_op_id + i + 1);
        if (!future)
            return WSS_PRED_MISSING_FUTURE;
        if ((future->valid_mask & 3U) != 3U ||
            future->wss > 0x7fffffffffffffffULL ||
            future->pgfault > 0x7fffffffffffffffULL)
            return WSS_PRED_DATA_UNAVAILABLE;
        future_wss[i] = (wss_pred_i64)future->wss;
        future_fault[i] = (wss_pred_i64)future->pgfault;
    }
    for (i = 0; i < WSS_PREDICTOR_HORIZON; ++i) {
        wss_bytes[i] = future_wss[i];
        pagefault[i] = future_fault[i];
    }
    return WSS_PRED_OK;
}
