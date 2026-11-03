#include <inttypes.h>
#include <stdio.h>
#include "get_wss_predictor.h"

int main(void)
{
    wss_pred_i64 wss[4], pagefault[4];
    unsigned int i;
    int status = get_wss_predictor("gimp", 1, 1, 0, wss, pagefault);
    if (status != WSS_PRED_OK) {
        fprintf(stderr, "Lookup failed: %d\n", status);
        return 1;
    }
    for (i = 0; i < 4; ++i)
        printf("operation=%u wss_bytes=%" PRId64 " pagefault=%" PRId64 "\n",
               i + 2, wss[i], pagefault[i]);
    return 0;
}
