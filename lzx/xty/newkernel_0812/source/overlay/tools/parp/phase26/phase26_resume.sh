#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0
set -euo pipefail
source "$(dirname "$0")/_common.sh"
phase26_init "$@"
phase26_require_snapshot_marker

release=$(phase26_runtime_release 2>/dev/null || true)
target=$(phase26_state_get target_kernel_release)
installed=$(phase26_state_get install_status)
if [[ -n "$release" && $(uname -r) = "$release" ]]; then
	phase26_log "RESUME_POSTBOOT"
	exec "$PHASE26_SCRIPT_DIR/phase26_postboot.sh" --output-root "$PHASE26_OUTPUT_ROOT"
fi
if [[ $installed = INSTALLED ]]; then
	phase26_log "PARP_PHASE26_TARGET_KERNEL_NOT_BOOTED target=$target"
	exit 79
fi
phase26_log "RESUME_PREBOOT install_status=$installed"
phase26_finish
