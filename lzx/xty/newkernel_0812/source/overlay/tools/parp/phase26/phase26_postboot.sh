#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0
set -euo pipefail
source "$(dirname "$0")/_common.sh"
phase26_init "$@"

release=$(phase26_runtime_release)
if [[ $(uname -r) != "$release" ]]; then
	phase26_log "PARP_PHASE26_TARGET_KERNEL_NOT_BOOTED running=$(uname -r) target=$release"
	exit 79
fi
mount_args=(--output-root "$PHASE26_OUTPUT_ROOT")
((PHASE26_DRY_RUN)) && mount_args+=(--dry-run)
"$PHASE26_SCRIPT_DIR/phase26_mount_interfaces.sh" "${mount_args[@]}"
config="/boot/config-$release"
for symbol in PARP DAMON DAMON_VADDR DAMON_SYSFS MEMCG LRU_GEN; do
	grep -q "^CONFIG_${symbol}=y" "$config"
done
test -d /sys/kernel/debug/parp
test -d /sys/kernel/tracing/events/parp
test -e /sys/kernel/debug/parp/scan_budget_apply_domain
test -d /sys/kernel/mm/damon/admin
restore_args=(--output-root "$PHASE26_OUTPUT_ROOT")
((PHASE26_DRY_RUN)) && restore_args+=(--dry-run)
"$PHASE26_SCRIPT_DIR/phase26_restore_observe.sh" "${restore_args[@]}"
phase26_state_set boot_verified true
phase26_state_set stage '"POSTBOOT"'
phase26_finish
