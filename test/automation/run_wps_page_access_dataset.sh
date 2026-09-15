#!/usr/bin/env bash
# WPS 0010--0070 文件页访问数据集：先每场景 3 次试采，通过门槛后再做 10 组
# “重启 WPS + 紧接温缓存重复”，共 20 次正式采集。脚本从不 drop_caches。
set -euo pipefail

test_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
project_root="$(cd "${test_root}/.." && pwd)"
service_root="${project_root}/lzx/service"
uid_value="$(id -u)"
phase="${WPS_PAGE_ACCESS_PHASE:-all}"
resume="${WPS_PAGE_ACCESS_RESUME:-0}"
display_value="${DISPLAY:-:0}"
xauthority_value="${XAUTHORITY:-}"
campaign_id="${WPS_PAGE_ACCESS_CAMPAIGN_ID:-wps_page_access_$(date +%Y%m%d_%H%M%S)}"
output_root="${WPS_PAGE_ACCESS_OUTPUT_ROOT:-${test_root}/outputs/wps_page_access_dataset/${campaign_id}}"
fixture_catalog="${output_root}/fixture_catalog.private.json"
current_monitor_pid=""

declare -A scenarios=(
    [0010]="wps_perf_0010_templates.json"
    [0020]="wps_perf_0020_new_documents.json"
    [0030]="wps_perf_0030_ten_documents.json"
    [0040]="wps_perf_0040_word.json"
    [0050]="wps_perf_0050_presentation.json"
    [0060]="wps_perf_0060_spreadsheet.json"
    [0070]="wps_perf_0070_pdf.json"
)

cleanup() {
    if [[ -n "${current_monitor_pid}" ]] && kill -0 "${current_monitor_pid}" 2>/dev/null; then
        kill -INT "${current_monitor_pid}" 2>/dev/null || true
        wait "${current_monitor_pid}" 2>/dev/null || true
    fi
}
trap cleanup EXIT INT TERM

case "${phase}" in
    all|pilot|formal) ;;
    *) echo "WPS_PAGE_ACCESS_PHASE must be all, pilot or formal" >&2; exit 2 ;;
esac
if systemctl --user is-active --quiet parp-runtime-monitor.service; then
    echo "Stop parp-runtime-monitor.service before dedicated collection." >&2
    exit 1
fi
for unit in "parp-process-events@${uid_value}.service" "parp-file-events@${uid_value}.service"; do
    if ! sudo -n systemctl is-active --quiet "${unit}"; then
        echo "Required helper is not active: ${unit}" >&2
        exit 1
    fi
done
if ! sudo -n test -r /sys/kernel/mm/page_idle/bitmap || ! sudo -n test -w /sys/kernel/mm/page_idle/bitmap; then
    echo "page_idle bitmap is unavailable; no fallback is permitted." >&2
    exit 1
fi
if ! cmp -s "${service_root}/runtime_monitor/ebpf/file_events.bpf.c" /usr/local/libexec/parp-file-events.bpf.c \
    || ! cmp -s "${service_root}/runtime_monitor/helpers/ebpf_file_event_helper.py" /usr/local/libexec/parp-ebpf-file-events \
    || ! cmp -s "${service_root}/runtime_monitor/core/page_access_window.py" /usr/local/libexec/parp_page_access_window.py; then
    echo "Installed eBPF helper is older than the workspace; run install_service.sh first." >&2
    exit 1
fi

mkdir -p "${output_root}"
python3 "${test_root}/automation/build_page_access_fixture_catalog.py" \
    "${test_root}/samples/wps" "${fixture_catalog}"

run_one() {
    local stage="$1" scenario_id="$2" repetition="$3" cache_condition="$4"
    local session_id="wps_${scenario_id}_${stage}_r$(printf '%02d' "${repetition}")"
    local session_dir="${output_root}/${stage}/${scenario_id}/${session_id}"
    local scenario_path="${test_root}/configs/automation/${scenarios[${scenario_id}]}"
    if [[ -e "${session_dir}" ]]; then
        # 续跑只能复用已经通过完整门禁的 session。失败、截断或带 .partial
        # 的目录仍会被拒绝，防止把不可信数据悄悄混入正式数据集。
        if [[ "${resume}" == "1" && "${stage}" == "pilot" ]] \
            && python3 "${test_root}/automation/validate_page_access_pilot.py" \
                --session "${session_dir}" >/dev/null; then
            echo "Reusing validated ${stage} session: ${session_dir}"
            return 0
        fi
        echo "Refusing to overwrite existing session: ${session_dir}" >&2
        return 1
    fi
    echo "Starting ${stage} session: scenario=${scenario_id} repetition=${repetition} cache=${cache_condition}"
    mkdir -p "${session_dir}"
    (
        cd "${service_root}"
        # 用 exec 让后台 job PID 就是 Python monitor；否则 $! 指向包装
        # subshell，SIGINT 只打到 shell，monitor 会在自动化结束后继续空采。
        exec python3 -u runtime_monitor/monitor.py \
            --output-dir "${session_dir}" \
            --session-id "${session_id}" \
            --duration 0 \
            --app-scope-config configs/runtime/runtime_app_scope.json \
            --process-event-source connector \
            --require-process-connector \
            --process-cgroup-routing systemd \
            --require-process-cgroup-routing \
            --foreground-backend x11 \
            --direct-x11-events \
            --file-event-source ebpf \
            --file-event-profile page-access-window \
            --page-access-target-app WPS \
            --page-access-window-ms 1000 \
            --page-access-fixture-catalog "${fixture_catalog}" \
            --page-access-scenario-id "${scenario_id}" \
            --page-access-repetition "${repetition}" \
            --page-access-cache-condition "${cache_condition}" \
            --require-page-idle \
            --require-ebpf-file-events \
            --test-slice huawei-test.slice \
            --suppress-event-trigger-logs \
            >"${session_dir}/monitor.stdout.log" 2>&1
    ) &
    current_monitor_pid=$!
    local ready=0
    for _ in $(seq 1 60); do
        if grep -q "page access windows: READY" "${session_dir}/monitor.stdout.log" 2>/dev/null; then
            ready=1
            break
        fi
        if ! kill -0 "${current_monitor_pid}" 2>/dev/null; then
            break
        fi
        sleep 0.5
    done
    if ((ready == 0)); then
        echo "Monitor failed to become ready: ${session_dir}" >&2
        cleanup
        current_monitor_pid=""
        return 1
    fi

    local automation_args=(
        --scenario "${scenario_path}"
        --display "${display_value}"
        --trace-output "${session_dir}/automation_trace.csv"
        --session-id "${session_id}"
        --scenario-id "${scenario_id}"
        --test-slice huawei-test.slice
    )
    if [[ -n "${xauthority_value}" ]]; then
        automation_args+=(--xauthority "${xauthority_value}")
    fi
    set +e
    DISPLAY="${display_value}" XAUTHORITY="${xauthority_value}" \
        "${test_root}/automation/run_automation.sh" \
        "${automation_args[@]}" \
        >"${session_dir}/automation.stdout.log" 2>&1
    local automation_rc=$?
    kill -INT "${current_monitor_pid}" 2>/dev/null
    wait "${current_monitor_pid}"
    local monitor_rc=$?
    set -e
    current_monitor_pid=""
    printf 'AUTOMATION_RC=%s\nMONITOR_RC=%s\nCACHE_CONDITION=%s\n' \
        "${automation_rc}" "${monitor_rc}" "${cache_condition}" \
        >"${session_dir}/run_status.txt"
    if ((automation_rc != 0 || monitor_rc != 0)); then
        echo "Session failed: ${session_dir}" >&2
        return 1
    fi
    echo "Completed ${stage} session: ${session_dir}"
}

if [[ "${phase}" == "all" || "${phase}" == "pilot" ]]; then
    for scenario_id in 0010 0020 0030 0040 0050 0060 0070; do
        for repetition in 1 2 3; do
            run_one pilot "${scenario_id}" "${repetition}" pilot
        done
    done
    python3 "${test_root}/automation/validate_page_access_pilot.py" \
        "${output_root}/pilot" \
        --expected-repetitions 3 \
        --report "${output_root}/pilot_validation.json"
fi

if [[ "${phase}" == "all" || "${phase}" == "formal" ]]; then
    if [[ ! -f "${output_root}/pilot_validation.json" ]] \
        || ! python3 -c 'import json,sys; sys.exit(0 if json.load(open(sys.argv[1]))["valid"] else 1)' "${output_root}/pilot_validation.json"; then
        echo "Formal collection is blocked until this campaign has a passing pilot_validation.json." >&2
        exit 1
    fi
    for scenario_id in 0010 0020 0030 0040 0050 0060 0070; do
        for pair in $(seq 1 10); do
            first=$((pair * 2 - 1))
            second=$((pair * 2))
            run_one formal "${scenario_id}" "${first}" restart_wps
            run_one formal "${scenario_id}" "${second}" warm_cache_repeat
        done
    done
fi

echo "WPS page-access dataset complete: ${output_root}"
