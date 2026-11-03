// SPDX-License-Identifier: GPL-2.0
/* Synthetic ABI fixture for tests ONLY; never used as a predictor fallback. */
#ifdef _WIN32
#ifndef WSS_PREDICTOR_BUILD_DLL
#define WSS_PREDICTOR_BUILD_DLL
#endif
#endif
#include "wss_predictor_compat.h"

static int fixture_name(const char *name)
{
	static const char expected[] = "fixture-only";
	unsigned int i;

	if (!name)
		return 0;
	for (i = 0; i < sizeof(expected); i++)
		if (name[i] != expected[i])
			return 0;
	return 1;
}

int get_wss_predictor(const char *app_name, wss_pred_u32 sequence_num,
		     wss_pred_u32 current_op_id, wss_pred_u64 pressure_gib,
		     wss_pred_i64 wss[4], wss_pred_i64 pagefault[4])
{
	unsigned int i;

	if (!fixture_name(app_name) || sequence_num != 123 || pressure_gib != 14)
		return -22;
	if (current_op_id == 5)
		return -2;
	for (i = 0; i < 4; i++) {
		wss[i] = 200;
		pagefault[i] = 1;
	}
	if (current_op_id == 2) {
		wss[1] = wss[2] = 500;
		pagefault[1] = 1000;
		wss[3] = pagefault[3] = -1;
	} else if (current_op_id == 3) {
		for (i = 0; i < 4; i++)
			wss[i] = pagefault[i] = -1;
	} else if (current_op_id == 4) {
		wss[1] = pagefault[1] = -1; /* Deliberately invalid hole. */
	}
	return 0;
}
