#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0
set -euo pipefail
source "$(dirname "$0")/_common.sh"
phase26_init "$@"

release=$(phase26_runtime_release)
[[ $(uname -r) = "$release" ]] || { phase26_log "TARGET_KERNEL_NOT_BOOTED"; exit 79; }
"$PHASE26_SCRIPT_DIR/phase26_restore_observe.sh" --output-root "$PHASE26_OUTPUT_ROOT"
if [[ -z ${DISPLAY:-} && -z ${WAYLAND_DISPLAY:-} ]]; then
	phase26_state_set level3b_observe_status '"GUI_SESSION_GATED"'
	phase26_log "GUI_SESSION_GATED"
	exit 81
fi
scope_config="$PHASE26_PROJECT_ROOT/configs/runtime_app_scope.json"
scenario="$PHASE26_PROJECT_ROOT/configs/automation/scenario_local_wps_files_qq_auto_login.json"
test -r "$scope_config"
test -r "$scenario"
phase26_log "GUI ready scope_config=$scope_config scenario=$scenario"
phase26_state_set level3b_observe_status '"READY_FOR_OBSERVE_AUTOMATION"'
if [[ ${PHASE26_RUN_GUI_AUTOMATION:-0} = 1 ]]; then
	phase26_run "$PHASE26_PROJECT_ROOT/automation/run_automation.sh" "$scenario"
else
	phase26_log "automation not started by wrapper; POSTBOOT orchestrator will collect baseline first"
fi
phase26_finish
