#!/usr/bin/env bash
# Collect an observe-only WPS page-hotset dataset.  The eBPF helper runs in
# page-hotset profile, so it emits compressed one-second page ranges only.
set -euo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
service_root="${project_root}/../lzx/service"
rounds="${WPS_HOTSET_ROUNDS:-35}"
duration_s="${WPS_HOTSET_DURATION_S:-1100}"
display_value="${DISPLAY:-:0}"
xauthority_value="${XAUTHORITY:-/run/user/1000/.mutter-Xwaylandauth.1C65U3}"
session_id="${WPS_HOTSET_SESSION_ID:-wps_hotset_dataset_$(date +%Y%m%d_%H%M%S)}"
output_root="${service_root}/outputs/runtime_monitor/${session_id}"

if systemctl --user is-active --quiet parp-runtime-monitor.service; then
    echo "The resident parp-runtime-monitor.service must be stopped before dedicated collection." >&2
    exit 1
fi
if ! systemctl is-active --quiet parp-file-events@1000.service; then
    echo "parp-file-events@1000.service is not active." >&2
    exit 1
fi

mkdir -p "${output_root}"
printf 'SESSION_ID=%s\nOUTPUT_ROOT=%s\nROUNDS=%s\n' "${session_id}" "${output_root}" "${rounds}"

(
    cd "${service_root}"
    python3 -u runtime_monitor/monitor.py \
        --output-dir "${output_root}" \
        --session-id "${session_id}" \
        --duration "${duration_s}" \
        --process-event-source connector \
        --process-connector-stale-timeout-s 600 \
        --foreground-backend x11 \
        --direct-x11-events \
        --file-event-source ebpf \
        --file-event-profile page-hotset \
        --file-event-stale-timeout-s 30 \
        --require-ebpf-file-events \
        --enable-page-hotset-shadow \
        --suppress-event-trigger-logs \
        >"${output_root}/monitor.stdout.log" 2>&1 &
    monitor_pid=$!
    sleep 3

    DISPLAY="${display_value}" XAUTHORITY="${xauthority_value}" \
        "${project_root}/automation/run_automation.sh" \
        --scenario "${project_root}/configs/automation/wps_page_hotset_dataset.json" \
        --display "${display_value}" \
        --xauthority "${xauthority_value}" \
        --trace-output "${output_root}/model/automation_trace.csv" \
        --session-id "${session_id}" \
        --scenario-id wps_page_hotset_dataset \
        --test-slice huawei-test.slice \
        --var "WPS_HOTSET_ROUNDS=${rounds}" \
        >"${output_root}/automation.stdout.log" 2>&1
    automation_rc=$?

    wait "${monitor_pid}"
    monitor_rc=$?
    printf 'AUTOMATION_RC=%s\nMONITOR_RC=%s\n' "${automation_rc}" "${monitor_rc}" \
        >"${output_root}/run_status.txt"
    exit $((automation_rc || monitor_rc))
)
