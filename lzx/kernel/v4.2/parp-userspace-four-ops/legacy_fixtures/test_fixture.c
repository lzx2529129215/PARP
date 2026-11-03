#ifdef NDEBUG
#undef NDEBUG
#endif
#include <assert.h>
#include "get_wss_predictor.h"

int main(void)
{
    wss_pred_i64 wss[4] = {111, 112, 113, 114};
    wss_pred_i64 pagefault[4] = {222, 223, 224, 225};
    unsigned int i;
    assert(get_wss_predictor("gimp", 1, 1, 0, wss, pagefault) == WSS_PRED_MISSING_FUTURE);
    assert(get_wss_predictor("gimp", 2, 1, 0, wss, pagefault) == WSS_PRED_DATA_UNAVAILABLE);
    assert(get_wss_predictor("gimp", 4, 1, 0, wss, pagefault) == WSS_PRED_MISSING_FUTURE);
    assert(get_wss_predictor("gimp", 5, 1, 0, wss, pagefault) == WSS_PRED_DATA_UNAVAILABLE);
    for (i = 0; i < 4; ++i) {
        assert(wss[i] == 111 + i);
        assert(pagefault[i] == 222 + i);
    }
    assert(get_wss_predictor("gimp", 3, 1, 0, wss, pagefault) == WSS_PRED_OK);
    for (i = 0; i < 4; ++i)
        assert(wss[i] == 20 + 10 * i);
    assert(pagefault[0] == 0 && pagefault[1] == 2 && pagefault[2] == 3 && pagefault[3] == 4);
    return 0;
}
