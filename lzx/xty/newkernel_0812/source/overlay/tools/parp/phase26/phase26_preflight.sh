#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0
set -euo pipefail
source "$(dirname "$0")/_common.sh"
phase26_init "$@"
phase26_require_snapshot_marker

phase26_run git -C "$PHASE26_WORK_TREE" fsck --no-progress
for tree in \
	"$PHASE26_PROJECT_ROOT/MGLRU-test/v4-parp/upstream/linux-6.17.13-clean" \
	"$PHASE26_PROJECT_ROOT/MGLRU-test/v4-parp/work/linux-6.17.13-parp-v4" \
	"$PHASE26_PROJECT_ROOT/MGLRU-test/v4-parp/work/linux-6.17.13-parp-v4-phase25"; do
	test -z "$(git -C "$tree" status --porcelain)"
done
test "$(git -C "$PHASE26_PROJECT_ROOT/MGLRU-test/v4-parp/upstream/linux-6.17.13-clean" rev-parse HEAD)" = \
	6609c4d49ebe220a5c40d3105c3f0e68f569ba1a
phase26_run df -h / /boot /boot/efi
phase26_run free -h
phase26_run uname -a
phase26_log "cgroup_fs=$(stat -fc %T /sys/fs/cgroup) memory_reclaim=$([[ -e /sys/fs/cgroup/memory.reclaim ]] && echo yes || echo no)"
phase26_finish
