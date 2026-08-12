#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0
set -euo pipefail
source "$(dirname "$0")/_common.sh"
phase26_init "$@"

if ! mountpoint -q /sys/kernel/debug; then
	phase26_run_root mount -t debugfs debugfs /sys/kernel/debug
fi
if ! mountpoint -q /sys/kernel/tracing; then
	phase26_run_root mount -t tracefs tracefs /sys/kernel/tracing
fi
phase26_finish
