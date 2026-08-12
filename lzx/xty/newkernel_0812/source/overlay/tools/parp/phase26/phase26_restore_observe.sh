#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0
set -euo pipefail
source "$(dirname "$0")/_common.sh"
phase26_init "$@"

parp_dir=/sys/kernel/debug/parp
test -d "$parp_dir"
phase26_run_root sh -c "printf '1\\n' > '$parp_dir/scan_budget_mode'"
phase26_run_root sh -c "printf '0\\n' > '$parp_dir/scan_budget_apply_domain'"
phase26_run_root sh -c "printf '1\\n' > '$parp_dir/mode'"
phase26_run_root sh -c "printf '0\\n' > '$parp_dir/evidence_mode'"
phase26_require_observe "$parp_dir"
test "$(phase26_read_root "$parp_dir/scan_budget_apply_domain")" = 0
phase26_state_set observe_restored true
phase26_finish
