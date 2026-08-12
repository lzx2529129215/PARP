#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0
set -euo pipefail
source "$(dirname "$0")/_common.sh"
phase26_init "$@"

release=$(phase26_state_get target_kernel_release)
test -n "$release"
entry="Ubuntu, with Linux $release"
grep -F "$entry" /boot/grub/grub.cfg | tee -a "$PHASE26_LOG"
phase26_log "MANUAL_MENU Advanced options for Ubuntu -> $entry"
if [[ ${PHASE26_SET_ONCE:-0} = 1 ]]; then
	phase26_run_root grub-reboot "Advanced options for Ubuntu>$entry"
	phase26_state_set grub_status '"ONE_TIME_ENTRY_SET"'
else
	phase26_log "one-time selection not changed; set PHASE26_SET_ONCE=1 explicitly"
fi
phase26_finish
