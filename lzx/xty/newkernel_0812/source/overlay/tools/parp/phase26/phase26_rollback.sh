#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0
set -euo pipefail
source "$(dirname "$0")/_common.sh"
phase26_init "$@"

release=$(phase26_state_get target_kernel_release)
if [[ $(uname -r) = "$release" && -d /sys/kernel/debug/parp ]]; then
	"$PHASE26_SCRIPT_DIR/phase26_restore_observe.sh" --output-root "$PHASE26_OUTPUT_ROOT" || true
	phase26_log "running target kernel retained; reboot manually into 5.15 before removal"
	exit 0
fi
if [[ ${PHASE26_REMOVE_KERNEL:-0} = 1 ]]; then
	[[ $(uname -r) != "$release" ]]
	mapfile -t packages < <(dpkg-query -W -f='${binary:Package}\n' 2>/dev/null | \
		grep -F "$release" || true)
	((${#packages[@]})) && phase26_run_root dpkg -r "${packages[@]}"
	phase26_run_root update-grub
fi
phase26_finish
