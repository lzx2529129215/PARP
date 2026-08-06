#!/usr/bin/env bash
set -u

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
RUN_ID="${1:-all_wps_$(date +%Y%m%d_%H%M%S)}"
OUTPUT_DIR="${PROJECT_ROOT}/outputs/runtime_monitor/${RUN_ID}"
STATUS_FILE="${OUTPUT_DIR}/status.tsv"

mkdir -p "${OUTPUT_DIR}"
printf 'case\tstate\texit_code\tstarted_at\tfinished_at\n' > "${STATUS_FILE}"

cleanup_wps() {
    pkill -x wpsoffice 2>/dev/null || true
    pkill -x wps 2>/dev/null || true
    pkill -x wpp 2>/dev/null || true
    pkill -x et 2>/dev/null || true
    pkill -x wpspdf 2>/dev/null || true
    sleep 3
}

for case_id in 0010 0020 0030 0040 0050 0060 0070; do
    case_dir="${OUTPUT_DIR}/${case_id}"
    mkdir -p "${case_dir}"
    started_at="$(date --iso-8601=seconds)"
    printf '%s\tSTARTED\t\t%s\t\n' "${case_id}" "${started_at}" >> "${STATUS_FILE}"

    cleanup_wps
    timeout --signal=TERM --kill-after=20s 900s \
        "${SCRIPT_DIR}/run_wps_case.sh" "${case_id}" \
        --session-id "${RUN_ID}_${case_id}" \
        --trace-output "${case_dir}/automation_trace.csv" \
        > "${case_dir}/run.log" 2>&1
    exit_code=$?

    finished_at="$(date --iso-8601=seconds)"
    if ((exit_code == 0)); then
        state="PASSED"
    elif ((exit_code == 124 || exit_code == 137)); then
        state="TIMEOUT"
    else
        state="FAILED"
    fi
    printf '%s\t%s\t%s\t%s\t%s\n' \
        "${case_id}" "${state}" "${exit_code}" "${started_at}" "${finished_at}" \
        >> "${STATUS_FILE}"
    printf '%s\n' "${case_id}" > "${OUTPUT_DIR}/last_completed_case"
    cleanup_wps
done

touch "${OUTPUT_DIR}/done"
