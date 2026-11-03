/* SPDX-License-Identifier: GPL-2.0 */
#ifndef PARP_GET_WSS_PREDICTOR_H
#define PARP_GET_WSS_PREDICTOR_H

#ifdef __KERNEL__
#include <linux/types.h>
typedef u32 wss_pred_u32;
typedef u64 wss_pred_u64;
typedef s64 wss_pred_i64;
#else
#include <stdint.h>
typedef uint32_t wss_pred_u32;
typedef uint64_t wss_pred_u64;
typedef int64_t wss_pred_i64;
#endif

#if defined(_WIN32) && defined(WSS_PREDICTOR_BUILD_DLL)
#define WSS_PRED_API __declspec(dllexport)
#else
#define WSS_PRED_API
#endif

#ifdef __cplusplus
extern "C" {
#endif

enum wss_predictor_status {
    WSS_PRED_OK = 0,
    WSS_PRED_INVALID_ARGUMENT = -1,
    WSS_PRED_NOT_FOUND = -2,
    WSS_PRED_MISSING_FUTURE = -4,
    WSS_PRED_DATA_UNAVAILABLE = -5,
    WSS_PRED_UNSUPPORTED_APP = -6
};

#define WSS_PREDICTOR_API_VERSION 3U
#define WSS_PREDICTOR_HORIZON 4U
#define WSS_PRED_NO_VALUE ((wss_pred_i64)-1)

/*
 * Application-selected lookup for the NEXT FOUR operation stages.
 * app_name: nonempty NUL-terminated name; ASCII case-insensitive (gimp/GIMP).
 * Currently only GIMP is registered. Unknown apps never fall back to GIMP.
 * sequence_num: application-local sequence number.
 * current_op_id: CURRENT operation number, not the first target operation.
 * pressure_gib: pressure in GiB, exactly 0, 8, 12 or 14; NOT headroom bytes.
 * wss_bytes[i], pagefault[i]: total WSS bytes and fault count for
 * current_op_id + i + 1, where i ranges from 0 through 3.
 * A valid query writes all eight slots. Future operations beyond the sequence
 * end are padded with -1, including all four slots at the last operation.
 * Errors leave BOTH arrays unchanged. Zero is a valid data value.
 * Each output must point to four writable SIGNED 64-bit elements, no overlap.
 * No allocation, file I/O or mutable prediction state is used.
 */
WSS_PRED_API int get_wss_predictor(const char *app_name,
                                  wss_pred_u32 sequence_num,
                                  wss_pred_u32 current_op_id,
                                  wss_pred_u64 pressure_gib,
                                  wss_pred_i64 wss_bytes[WSS_PREDICTOR_HORIZON],
                                  wss_pred_i64 pagefault[WSS_PREDICTOR_HORIZON]);

#ifdef __cplusplus
}
#endif
#endif
