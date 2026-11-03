#ifdef NDEBUG
#undef NDEBUG
#endif
#include <assert.h>
#include <stdio.h>
#include "get_wss_predictor.h"

static void reset(wss_pred_i64 wss[4], wss_pred_i64 pagefault[4])
{
    unsigned int i;
    for (i = 0; i < 4; ++i) {
        wss[i] = 111 + i;
        pagefault[i] = 222 + i;
    }
}

static void unchanged(const wss_pred_i64 wss[4], const wss_pred_i64 pagefault[4])
{
    unsigned int i;
    for (i = 0; i < 4; ++i) {
        assert(wss[i] == 111 + i);
        assert(pagefault[i] == 222 + i);
    }
}

static void reject(const char *app, wss_pred_u32 seq, wss_pred_u32 op,
                   wss_pred_u64 pressure, int expected)
{
    wss_pred_i64 wss[4], pagefault[4];
    reset(wss, pagefault);
    assert(get_wss_predictor(app, seq, op, pressure, wss, pagefault) == expected);
    unchanged(wss, pagefault);
}

int main(void)
{
    wss_pred_i64 wss[4], pagefault[4], overlap[8];
    unsigned int i;
    const wss_pred_i64 expected_wss[4] = {289120256ULL, 485830656ULL, 475205632ULL, 553238528ULL};
    const wss_pred_i64 expected_fault[4] = {64992ULL, 120144ULL, 25773ULL, 45476ULL};
    assert(get_wss_predictor("gimp", 1, 1, 0, wss, pagefault) == WSS_PRED_OK);
    for (i = 0; i < 4; ++i) {
        assert(wss[i] == expected_wss[i]);
        assert(pagefault[i] == expected_fault[i]);
    }
    assert(get_wss_predictor("GIMP", 1, 4, 0, wss, pagefault) == WSS_PRED_OK);
    assert(wss[3] == 48627712ULL && pagefault[3] == 3ULL);
    for (i = 5; i <= 8; ++i) {
        unsigned int j;
        unsigned int available = 8 - i;
        assert(get_wss_predictor("gimp", 1, i, 0, wss, pagefault) == WSS_PRED_OK);
        for (j = 0; j < 4; ++j) {
            if (j >= available) {
                assert(wss[j] == -1 && pagefault[j] == -1);
            } else {
                assert(wss[j] >= 0 && pagefault[j] >= 0);
            }
        }
    }
    reject("gimp", 0, 1, 0, WSS_PRED_INVALID_ARGUMENT);
    reject("gimp", 1, 0, 0, WSS_PRED_INVALID_ARGUMENT);
    reject("gimp", 1, 9, 0, WSS_PRED_INVALID_ARGUMENT);
    reject("gimp", 1, 1, 2, WSS_PRED_INVALID_ARGUMENT);
    reject("gimp", 1, 1, 8ULL * 1024ULL * 1024ULL * 1024ULL, WSS_PRED_INVALID_ARGUMENT);
    reject("gimp", 999, 1, 0, WSS_PRED_NOT_FOUND);
    reject("firefox", 1, 1, 0, WSS_PRED_UNSUPPORTED_APP);
    reject("gimp_extra", 1, 1, 0, WSS_PRED_UNSUPPORTED_APP);
    reject("", 1, 1, 0, WSS_PRED_INVALID_ARGUMENT);
    reject(0, 1, 1, 0, WSS_PRED_INVALID_ARGUMENT);
    reset(wss, pagefault);
    assert(get_wss_predictor("gimp", 1, 1, 0, 0, pagefault) == WSS_PRED_INVALID_ARGUMENT);
    assert(get_wss_predictor("gimp", 1, 1, 0, wss, 0) == WSS_PRED_INVALID_ARGUMENT);
    assert(get_wss_predictor("gimp", 1, 1, 0, wss, wss) == WSS_PRED_INVALID_ARGUMENT);
    unchanged(wss, pagefault);
    for (i = 0; i < 8; ++i)
        overlap[i] = 1000 + i;
    assert(get_wss_predictor("gimp", 1, 1, 0, overlap, overlap + 1) == WSS_PRED_INVALID_ARGUMENT);
    for (i = 0; i < 8; ++i)
        assert(overlap[i] == 1000 + i);
    puts("Four-stage C contract tests passed.");
    return 0;
}
