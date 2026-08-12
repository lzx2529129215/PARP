#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0
set -euo pipefail
source "$(dirname "$0")/_common.sh"
phase26_init "$@"

build_dir=$(phase26_state_get build_dir)
mkdir -p "$build_dir"
if [[ ! -f "$build_dir/.config" ]]; then
	phase25_config="$PHASE26_PROJECT_ROOT/MGLRU-test/v4-parp/build/phase25-observe/.config"
	test -r "$phase25_config"
	phase26_run cp "$phase25_config" "$build_dir/.config"
	phase26_run "$PHASE26_WORK_TREE/scripts/config" --file "$build_dir/.config" \
		-e PARP -e MEMCG -e CGROUPS -e LRU_GEN -e LRU_GEN_ENABLED \
		-e DAMON -e DAMON_VADDR -e DAMON_SYSFS -e PARP_DAMON_ALIGNMENT \
		-e DEBUG_FS -e TRACING -e TRACEPOINTS -e FTRACE -e SWAP \
		--set-str SYSTEM_TRUSTED_KEYS "" \
		--set-str SYSTEM_REVOCATION_KEYS "" \
		-d LOCALVERSION_AUTO --set-str LOCALVERSION -parp-v4-phase26-observe
	phase26_run make -C "$PHASE26_WORK_TREE" O="$build_dir" \
		LOCALVERSION= olddefconfig
fi
for symbol in PARP MEMCG CGROUPS LRU_GEN DAMON DAMON_VADDR DAMON_SYSFS \
		DEBUG_FS TRACING TRACEPOINTS FTRACE SWAP; do
	grep -q "^CONFIG_${symbol}=y" "$build_dir/.config"
done
grep -q '^CONFIG_SYSTEM_TRUSTED_KEYS=""$' "$build_dir/.config"
if grep -q '^CONFIG_SYSTEM_REVOCATION_KEYS=' "$build_dir/.config"; then
	grep -q '^CONFIG_SYSTEM_REVOCATION_KEYS=""$' "$build_dir/.config"
fi
! grep -q 'debian/canonical-.*certs.pem' "$build_dir/.config"
phase26_run make -C "$PHASE26_WORK_TREE" O="$build_dir" LOCALVERSION= \
	-j"$(nproc)" bzImage modules
release=$(make -s -C "$PHASE26_WORK_TREE" O="$build_dir" \
	LOCALVERSION= kernelrelease)
test "$release" = 6.17.13-parp-v4-phase26-observe
phase26_state_set target_kernel_release "\"$release\""
phase26_state_set source_head "\"$(git -C "$PHASE26_WORK_TREE" rev-parse HEAD)\""
phase26_finish
