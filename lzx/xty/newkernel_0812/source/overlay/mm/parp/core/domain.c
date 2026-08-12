// SPDX-License-Identifier: GPL-2.0
#include "../internal.h"

static enum parp_mode parp_mode = PARP_MODE_OBSERVE;
static enum parp_evidence_mode parp_evidence_mode = PARP_EVIDENCE_ONLY;

enum parp_mode parp_get_mode(void)
{
	return READ_ONCE(parp_mode);
}

int parp_set_mode(enum parp_mode mode)
{
	if (mode < PARP_MODE_DISABLED || mode > PARP_MODE_APPLY)
		return -EINVAL;
	WRITE_ONCE(parp_mode, mode);
	return 0;
}

enum parp_evidence_mode parp_get_evidence_mode(void)
{
	return READ_ONCE(parp_evidence_mode);
}

int parp_set_evidence_mode(enum parp_evidence_mode mode)
{
	if (mode < PARP_EVIDENCE_ONLY || mode > PARP_MODEL_TEST)
		return -EINVAL;
	WRITE_ONCE(parp_evidence_mode, mode);
	return 0;
}
