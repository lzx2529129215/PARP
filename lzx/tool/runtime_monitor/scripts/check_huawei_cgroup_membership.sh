#!/usr/bin/env bash
set -euo pipefail

SLICE="huawei-test.slice"
SESSION_DIR=""
APP_PATTERN="wps|WPS|qq|QQ|nautilus|Files|dde-file-manager"
WATCH_SECONDS=0
INTERVAL_S=1
MEMORY_MAX_TEST="4G"

usage() {
    cat <<EOF
Usage:
  $0 [--slice huawei-test.slice] [--session-dir DIR] [--app-pattern REGEX] [--watch-seconds N] [--interval-s N]

Checks whether automation-launched WPS / QQ / FILES processes are under the
same parent user slice cgroup, and records cgroup memory files. This script
does not perform prefetch, eviction, swap, MGLRU, debugfs, or page-cache actions.
EOF
}

while (($# > 0)); do
    case "$1" in
        --slice) SLICE="$2"; shift 2 ;;
        --session-dir) SESSION_DIR="$2"; shift 2 ;;
        --app-pattern) APP_PATTERN="$2"; shift 2 ;;
        --watch-seconds) WATCH_SECONDS="$2"; shift 2 ;;
        --interval-s) INTERVAL_S="$2"; shift 2 ;;
        --memory-max-test) MEMORY_MAX_TEST="$2"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Unknown argument: $1" >&2; usage >&2; exit 2 ;;
    esac
done

if [[ -z "$SESSION_DIR" ]]; then
    SESSION_DIR="outputs/runtime_monitor/cgroup_check_$(date +%Y%m%d_%H%M%S)"
fi
REVIEW_DIR="$SESSION_DIR/review"
mkdir -p "$REVIEW_DIR"

CHECKS_CSV="$REVIEW_DIR/cgroup_membership_checks.csv"
REPORT_MD="$REVIEW_DIR/cgroup_membership_report.md"
PIDS_TSV="$REVIEW_DIR/cgroup_membership_processes.tsv"
MEMORY_TSV="$REVIEW_DIR/cgroup_membership_memory.tsv"
STATUS_TXT="$REVIEW_DIR/cgroup_membership_systemctl_status.txt"
CGLS_TXT="$REVIEW_DIR/cgroup_membership_systemd_cgls.txt"

: > "$STATUS_TXT"
: > "$CGLS_TXT"
printf 'sample_ts\tpid\tapp\tcmdline\tproc_cgroup\tleaf_cgroup\tunder_parent_slice\n' > "$PIDS_TSV"
printf 'sample_ts\tcontrol_group\tcgroup_path\tmemory_max\tmemory_current\tmemory_events\n' > "$MEMORY_TSV"

csv_escape() {
    local value="${1:-}"
    value="${value//$'\n'/; }"
    value="${value//\"/\"\"}"
    printf '"%s"' "$value"
}

add_check() {
    local check="$1" result="$2" observed="$3" details="$4"
    {
        csv_escape "$check"; printf ','
        csv_escape "$result"; printf ','
        csv_escape "$observed"; printf ','
        csv_escape "$details"; printf '\n'
    } >> "$CHECKS_CSV"
}

read_cmdline() {
    local pid="$1"
    if [[ -r "/proc/$pid/cmdline" ]]; then
        tr '\0' ' ' < "/proc/$pid/cmdline" | sed 's/[[:space:]]*$//'
    elif [[ -r "/proc/$pid/comm" ]]; then
        cat "/proc/$pid/comm"
    fi
}

classify_app() {
    local text
    text="$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')"
    if [[ "$text" =~ run_automation\.sh|app_automation\.py|check_huawei_cgroup_membership\.sh ]]; then
        printf 'UNKNOWN'
        return
    fi
    if [[ "$text" =~ wps|wpsoffice|kingsoft ]]; then
        printf 'WPS'
    elif [[ "$text" =~ linuxqq|tencent|qq|腾讯 ]]; then
        printf 'QQ'
    elif [[ "$text" =~ nautilus|org.gnome.nautilus|files|dde-file-manager ]]; then
        printf 'FILES'
    else
        printf 'UNKNOWN'
    fi
}

control_group() {
    systemctl --user show "$SLICE" -p ControlGroup --value 2>/dev/null || true
}

memory_value() {
    local path="$1" name="$2"
    if [[ -r "$path/$name" ]]; then
        cat "$path/$name" 2>/dev/null | tr '\n' ';' | sed 's/;$//'
    else
        printf ''
    fi
}

sample_once() {
    local ts cg cg_path pids pid cmd proc_cg leaf under app memory_max memory_current memory_events
    ts="$(date '+%Y-%m-%d %H:%M:%S')"
    {
        echo "===== $ts systemctl --user status $SLICE ====="
        systemctl --user status "$SLICE" --no-pager || true
        echo
    } >> "$STATUS_TXT"
    cg="$(control_group)"
    if [[ -n "$cg" ]]; then
        cg_path="/sys/fs/cgroup$cg"
        {
            echo "===== $ts systemd-cgls $SLICE ====="
            systemd-cgls --user "$SLICE" --no-pager || systemd-cgls "$SLICE" --no-pager || true
            echo
        } >> "$CGLS_TXT"
        memory_max="$(memory_value "$cg_path" memory.max)"
        memory_current="$(memory_value "$cg_path" memory.current)"
        memory_events="$(memory_value "$cg_path" memory.events)"
        printf '%s\t%s\t%s\t%s\t%s\t%s\n' "$ts" "$cg" "$cg_path" "$memory_max" "$memory_current" "$memory_events" >> "$MEMORY_TSV"
    else
        cg_path=""
        printf '%s\t\t\t\t\t\n' "$ts" >> "$MEMORY_TSV"
    fi

    pids="$(pgrep -f "$APP_PATTERN" 2>/dev/null || true)"
    for pid in $pids; do
        [[ -d "/proc/$pid" ]] || continue
        cmd="$(read_cmdline "$pid")"
        [[ -n "$cmd" ]] || continue
        app="$(classify_app "$cmd")"
        [[ "$app" != "UNKNOWN" ]] || continue
        proc_cg="$(cat "/proc/$pid/cgroup" 2>/dev/null | tr '\n' ';' | sed 's/;$//')"
        leaf="$(printf '%s' "$proc_cg" | awk -F: '{print $3}' | tail -n 1)"
        if [[ -n "$cg" && "$leaf" == "$cg"* ]]; then
            under="yes"
        else
            under="no"
        fi
        printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$ts" "$pid" "$app" "$cmd" "$proc_cg" "$leaf" "$under" >> "$PIDS_TSV"
    done
}

printf 'check,result,observed,details\n' > "$CHECKS_CSV"

# Safe set/read verification. The value is intentionally high to avoid OOM.
set_property_result="not_attempted"
set_property_details=""
before_memory_max="$(systemctl --user show "$SLICE" -p MemoryMax --value 2>/dev/null || true)"
if systemctl --user set-property "$SLICE" "MemoryMax=$MEMORY_MAX_TEST" >/tmp/huawei_cgroup_set_property.out 2>/tmp/huawei_cgroup_set_property.err; then
    set_property_result="PASS"
    set_property_details="MemoryMax set to $MEMORY_MAX_TEST via systemctl --user set-property"
else
    set_property_result="FAIL"
    set_property_details="$(cat /tmp/huawei_cgroup_set_property.err 2>/dev/null || true)"
fi
after_memory_max="$(systemctl --user show "$SLICE" -p MemoryMax --value 2>/dev/null || true)"

if [[ "$WATCH_SECONDS" =~ ^[0-9]+$ ]] && (( WATCH_SECONDS > 0 )); then
    end=$((SECONDS + WATCH_SECONDS))
    while (( SECONDS < end )); do
        sample_once
        sleep "$INTERVAL_S"
    done
else
    sample_once
fi

CG="$(control_group)"
CG_PATH=""
if [[ -n "$CG" ]]; then
    CG_PATH="/sys/fs/cgroup$CG"
fi

status_summary="$(systemctl --user status "$SLICE" --no-pager 2>/dev/null | sed -n '1,8p' || true)"
slice_active="FAIL"
if printf '%s' "$status_summary" | grep -q 'Active: active'; then
    slice_active="PASS"
fi

slice_unit_exists="FAIL"
if systemctl --user status "$SLICE" --no-pager >/dev/null 2>&1; then
    slice_unit_exists="PASS"
fi

control_group_non_empty="FAIL"
[[ -n "$CG" ]] && control_group_non_empty="PASS"

cgroup_path_exists="FAIL"
[[ -n "$CG_PATH" && -d "$CG_PATH" ]] && cgroup_path_exists="PASS"

apps_found="$(awk -F'\t' 'NR>1{seen[$3]=1} END{for (app in seen) print app}' "$PIDS_TSV" | sort | paste -sd ' ' -)"
app_processes_found="FAIL"
for app in WPS QQ FILES; do
    if ! printf ' %s ' "$apps_found" | grep -q " $app "; then
        app_processes_found="FAIL"
        break
    fi
    app_processes_found="PASS"
done

escaped_count="$(awk -F'\t' 'NR>1 && $7!="yes"{count++} END{print count+0}' "$PIDS_TSV")"
all_under="FAIL"
[[ "$escaped_count" == "0" && "$(wc -l < "$PIDS_TSV")" -gt 1 ]] && all_under="PASS"

unique_leaf_count="$(awk -F'\t' 'NR>1{seen[$6]=1} END{count=0; for (leaf in seen) count++; print count+0}' "$PIDS_TSV")"
same_leaf="FAIL"
[[ "$unique_leaf_count" == "1" && "$(wc -l < "$PIDS_TSV")" -gt 1 ]] && same_leaf="PASS"

escaped_result="FAIL"
[[ "$escaped_count" == "0" ]] && escaped_result="PASS"

latest_memory_max="$(awk -F'\t' 'NR>1 && $4!=""{v=$4} END{print v}' "$MEMORY_TSV")"
latest_memory_current="$(awk -F'\t' 'NR>1 && $5!=""{v=$5} END{print v}' "$MEMORY_TSV")"
latest_memory_events="$(awk -F'\t' 'NR>1 && $6!=""{v=$6} END{print v}' "$MEMORY_TSV")"

memory_max_readable="FAIL"; [[ -n "$latest_memory_max" ]] && memory_max_readable="PASS"
memory_current_readable="FAIL"; [[ -n "$latest_memory_current" ]] && memory_current_readable="PASS"
memory_events_readable="FAIL"; [[ -n "$latest_memory_events" ]] && memory_events_readable="PASS"

add_check "slice_unit_exists" "$slice_unit_exists" "$SLICE" "queried with systemctl --user"
add_check "slice_active_during_automation" "$slice_active" "$status_summary" "active slice expected during automation/watch window"
add_check "control_group_non_empty" "$control_group_non_empty" "$CG" ""
add_check "cgroup_path_exists" "$cgroup_path_exists" "$CG_PATH" ""
add_check "app_processes_found" "$app_processes_found" "$apps_found" "expected WPS QQ FILES"
add_check "all_app_processes_under_parent_slice" "$all_under" "escaped_count=$escaped_count" "parent ControlGroup=$CG"
add_check "all_app_processes_same_leaf_cgroup" "$same_leaf" "unique_leaf_count=$unique_leaf_count" "FAIL here is acceptable when apps use different per-app scopes"
add_check "escaped_processes_count" "$escaped_result" "$escaped_count" ""
add_check "memory_max_readable" "$memory_max_readable" "$latest_memory_max" ""
add_check "memory_current_readable" "$memory_current_readable" "$latest_memory_current" ""
add_check "memory_events_readable" "$memory_events_readable" "$latest_memory_events" ""
add_check "parent_memory_max_settable" "$set_property_result" "before=$before_memory_max after=$after_memory_max" "$set_property_details"

final_result="PASS"
for required in "$slice_unit_exists" "$slice_active" "$control_group_non_empty" "$cgroup_path_exists" "$app_processes_found" "$all_under" "$escaped_result" "$memory_max_readable" "$memory_current_readable" "$memory_events_readable" "$set_property_result"; do
    if [[ "$required" != "PASS" ]]; then
        final_result="FAIL"
        break
    fi
done
add_check "final_result" "$final_result" "$final_result" "same leaf is informational unless explicitly required"

leaf_summary="$(awk -F'\t' 'NR>1{seen[$6]=1} END{for (leaf in seen) print leaf}' "$PIDS_TSV" | sort)"
process_summary="$(awk -F'\t' 'NR>1{print "- PID "$2" ["$3"] leaf="$6" under_parent="$7" cmdline="$4}' "$PIDS_TSV")"

{
    echo "# Huawei cgroup membership report"
    echo
    echo "- check_time: $(date '+%Y-%m-%d %H:%M:%S')"
    echo "- session_dir: \`$SESSION_DIR\`"
    echo "- slice: \`$SLICE\`"
    echo "- final_result: **$final_result**"
    echo "- no prefetch, eviction, swap, MGLRU, debugfs, or page cache action was performed."
    echo
    echo "## systemctl status summary"
    echo '```text'
    printf '%s\n' "$status_summary"
    echo '```'
    echo
    echo "## ControlGroup"
    echo "- ControlGroup: \`$CG\`"
    echo "- cgroup_path: \`$CG_PATH\`"
    echo "- active_during_check: $slice_active"
    echo
    echo "## systemd-cgls summary"
    echo '```text'
    tail -n 80 "$CGLS_TXT"
    echo '```'
    echo
    echo "## Detected app processes"
    if [[ -s "$PIDS_TSV" && "$(wc -l < "$PIDS_TSV")" -gt 1 ]]; then
        printf '%s\n' "$process_summary"
    else
        echo "- No matching WPS / QQ / FILES process was found."
    fi
    echo
    echo "## Membership conclusions"
    echo "- all_app_processes_under_parent_slice: $all_under"
    echo "- escaped_processes_count: $escaped_count"
    echo "- all_app_processes_same_leaf_cgroup: $same_leaf"
    if [[ "$same_leaf" != "PASS" ]]; then
        echo "- Apps are under the same parent slice but not the same leaf cgroup. This is expected for per-app scopes."
        echo "- observed leaf cgroups:"
        while IFS= read -r leaf; do
            [[ -n "$leaf" ]] && echo "  - \`$leaf\`"
        done <<< "$leaf_summary"
    fi
    echo
    echo "## Memory files"
    echo "- systemctl MemoryMax before: \`$before_memory_max\`"
    echo "- systemctl MemoryMax after: \`$after_memory_max\`"
    echo "- memory.max: \`$latest_memory_max\`"
    echo "- memory.current: \`$latest_memory_current\`"
    echo "- memory.events:"
    echo '```text'
    printf '%s\n' "$latest_memory_events" | tr ';' '\n'
    echo '```'
    echo "- parent_memory_max_settable: $set_property_result"
    echo
    echo "## Output files"
    echo "- checks_csv: \`$CHECKS_CSV\`"
    echo "- process_samples: \`$PIDS_TSV\`"
    echo "- memory_samples: \`$MEMORY_TSV\`"
    echo
    echo "## Final conclusion"
    if [[ "$final_result" == "PASS" ]]; then
        echo "PASS: WPS / QQ / FILES processes were found during automation and all observed target processes were under the huawei-test.slice parent cgroup. They are allowed to be in different leaf scope cgroups. Parent cgroup memory.max, memory.current, and memory.events were readable, and MemoryMax was settable via systemd with a safe high value. No memory scheduling action was performed."
    else
        echo "FAIL: One or more required cgroup membership or memory file checks failed. Inspect \`$CHECKS_CSV\` and \`$PIDS_TSV\`."
    fi
} > "$REPORT_MD"

echo "saved: $CHECKS_CSV"
echo "saved: $REPORT_MD"
echo "saved: $PIDS_TSV"
echo "saved: $MEMORY_TSV"
echo "final_result: $final_result"
