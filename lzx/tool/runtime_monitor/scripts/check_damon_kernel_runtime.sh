#!/usr/bin/env bash
# 检查已启动内核的 DAMON 运行时能力；只读，不写 sysfs、debugfs 或 MGLRU 接口。
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
OUTPUT_DIR="$PROJECT_ROOT/outputs/mglru/damon_kernel_runtime_$(date +%Y%m%d_%H%M%S)"
EXPECTED_RELEASE=""
REQUIRE_VADDR=false

usage() {
    cat <<'EOF'
用法：
  bash runtime_monitor/scripts/check_damon_kernel_runtime.sh \
    [--expected-release <release>] [--output-dir <dir>] [--require-vaddr]

只读取当前已启动内核的 DAMON 配置、sysfs 和 tracefs 能力；不会创建 DAMON 实例，
不会写 lru_gen_pages，也不会修改 MGLRU 策略。
EOF
}

while (($#)); do
    case "$1" in
        --expected-release)
            EXPECTED_RELEASE="${2:?--expected-release 需要参数}"
            shift 2
            ;;
        --require-vaddr)
            REQUIRE_VADDR=true
            shift
            ;;
        --output-dir)
            OUTPUT_DIR="${2:?--output-dir 需要参数}"
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "未知参数: $1" >&2
            usage >&2
            exit 2
            ;;
    esac
done

mkdir -p "$OUTPUT_DIR"

RELEASE="$(uname -r)"
CONFIG_SOURCE=""
CONFIG_FILE=""
if [[ -r /proc/config.gz ]]; then
    CONFIG_SOURCE="/proc/config.gz"
elif [[ -r "/boot/config-$RELEASE" ]]; then
    CONFIG_SOURCE="/boot/config-$RELEASE"
    CONFIG_FILE="$CONFIG_SOURCE"
elif [[ -r "/lib/modules/$RELEASE/build/.config" ]]; then
    CONFIG_SOURCE="/lib/modules/$RELEASE/build/.config"
    CONFIG_FILE="$CONFIG_SOURCE"
fi

config_value() {
    local name="$1"
    if [[ "$CONFIG_SOURCE" == "/proc/config.gz" ]]; then
        zgrep -E "^(CONFIG_${name}=|# CONFIG_${name} is not set)" /proc/config.gz 2>/dev/null | head -n 1 || true
    elif [[ -n "$CONFIG_FILE" ]]; then
        grep -E "^(CONFIG_${name}=|# CONFIG_${name} is not set)" "$CONFIG_FILE" 2>/dev/null | head -n 1 || true
    fi
}

config_enabled() {
    local value
    value="$(config_value "$1")"
    [[ "$value" == "CONFIG_$1=y" || "$value" == "CONFIG_$1=m" ]]
}

tracefs_root=""
for candidate in /sys/kernel/tracing /sys/kernel/debug/tracing; do
    if [[ -d "$candidate" ]]; then
        tracefs_root="$candidate"
        break
    fi
done

damon_admin="/sys/kernel/mm/damon/admin"
tracepoint_dir=""
tracepoint_format=""
tracepoint_enable=""
if [[ -n "$tracefs_root" ]]; then
    tracepoint_dir="$tracefs_root/events/damon/damon_aggregated"
    tracepoint_format="$tracepoint_dir/format"
    tracepoint_enable="$tracepoint_dir/enable"
fi

vaddr_found=false
vaddr_paths=()
if [[ -d "$damon_admin" ]]; then
    while IFS= read -r path; do
        vaddr_paths+=("$path")
        vaddr_found=true
    done < <(find "$damon_admin" -type f -name avail_operations -readable -print 2>/dev/null | while IFS= read -r path; do
        if grep -qw vaddr "$path" 2>/dev/null; then
            printf '%s\n' "$path"
        fi
    done)
fi

check_value() {
    local value="$1"
    if [[ -n "$value" ]]; then
        printf '%s' "$value"
    else
        printf '%s' 'UNKNOWN'
    fi
}

overall=PASS
needs_root=false
if [[ -n "$EXPECTED_RELEASE" && "$RELEASE" != "$EXPECTED_RELEASE" ]]; then
    overall=FAIL
fi
for config in DAMON DAMON_VADDR DAMON_SYSFS TRACEPOINTS TRACING FTRACE; do
    if ! config_enabled "$config"; then
        overall=FAIL
    fi
done
if [[ ! -d "$damon_admin" || ! -e "$tracepoint_format" ]]; then
    overall=FAIL
fi
if [[ "$overall" == PASS && ( ! -w "$damon_admin" || ! -r "$tracepoint_format" || ! -w "$tracepoint_enable" ) ]]; then
    needs_root=true
fi
if [[ "$REQUIRE_VADDR" == true && "$vaddr_found" != true ]]; then
    overall=FAIL
fi
if [[ "$overall" == PASS && "$needs_root" == true ]]; then
    overall=SUPPORTED_NEEDS_ROOT
fi

if [[ "$vaddr_found" == true ]]; then
    vaddr_check_result=PASS
elif [[ -d "$damon_admin" ]]; then
    vaddr_check_result=NOT_OBSERVED_NO_CONTEXT
else
    vaddr_check_result=NOT_AVAILABLE
fi

if ((${#vaddr_paths[@]} == 0)); then
    vaddr_paths_json='[]'
else
    vaddr_paths_json="$(printf '%s\n' "${vaddr_paths[@]}" | python3 -c 'import json, sys; print(json.dumps([line.rstrip("\\n") for line in sys.stdin if line.rstrip("\\n")]))')"
fi

json_escape() {
    python3 -c 'import json, sys; print(json.dumps(sys.stdin.read()))'
}

report_json="$OUTPUT_DIR/damon_kernel_runtime_report.json"
report_md="$OUTPUT_DIR/damon_kernel_runtime_report.md"

{
    printf '{\n'
    printf '  "checked_at": %s,\n' "$(date +%s)"
    printf '  "uname_r": %s,\n' "$(printf '%s' "$RELEASE" | json_escape)"
    printf '  "expected_release": %s,\n' "$(printf '%s' "$EXPECTED_RELEASE" | json_escape)"
    printf '  "config_source": %s,\n' "$(printf '%s' "$CONFIG_SOURCE" | json_escape)"
    printf '  "config": {\n'
    for config in DAMON DAMON_VADDR DAMON_SYSFS TRACEPOINTS TRACING FTRACE BPF BPF_SYSCALL DEBUG_INFO_BTF; do
        printf '    "%s": %s' "$config" "$(printf '%s' "$(check_value "$(config_value "$config")")" | json_escape)"
        if [[ "$config" == DEBUG_INFO_BTF ]]; then printf '\n'; else printf ',\n'; fi
    done
    printf '  },\n'
    printf '  "damon_admin": {"path": %s, "exists": %s, "readable": %s, "writable": %s},\n' \
        "$(printf '%s' "$damon_admin" | json_escape)" \
        "$([[ -d "$damon_admin" ]] && printf true || printf false)" \
        "$([[ -r "$damon_admin" ]] && printf true || printf false)" \
        "$([[ -w "$damon_admin" ]] && printf true || printf false)"
    printf '  "vaddr_available": %s,\n' "$vaddr_found"
    printf '  "vaddr_check_result": %s,\n' "$(printf '%s' "$vaddr_check_result" | json_escape)"
    printf '  "require_vaddr": %s,\n' "$REQUIRE_VADDR"
    printf '  "vaddr_avail_operations_files": %s,\n' "$vaddr_paths_json"
    printf '  "tracefs_root": %s,\n' "$(printf '%s' "$tracefs_root" | json_escape)"
    printf '  "damon_aggregated": {"path": %s, "format_readable": %s, "enable_writable": %s},\n' \
        "$(printf '%s' "$tracepoint_dir" | json_escape)" \
        "$([[ -r "$tracepoint_format" ]] && printf true || printf false)" \
        "$([[ -w "$tracepoint_enable" ]] && printf true || printf false)"
    printf '  "effective_uid": %s,\n' "$(id -u)"
    printf '  "final_result": %s\n' "$(printf '%s' "$overall" | json_escape)"
    printf '}\n'
} > "$report_json"

{
    printf '# DAMON 内核运行态检查\n\n'
    printf -- '- 检查时间：`%s`\n' "$(date -Is)"
    printf -- '- 当前内核：`%s`\n' "$RELEASE"
    printf -- '- 期望内核：`%s`\n' "${EXPECTED_RELEASE:-未指定}"
    printf -- '- 配置来源：`%s`\n' "${CONFIG_SOURCE:-未找到}"
    printf -- '- 有效 UID：`%s`\n' "$(id -u)"
    printf -- '- 最终结果：`%s`\n\n' "$overall"
    printf '## 配置\n\n'
    for config in DAMON DAMON_VADDR DAMON_SYSFS TRACEPOINTS TRACING FTRACE BPF BPF_SYSCALL DEBUG_INFO_BTF; do
        printf -- '- `%s`：`%s`\n' "$config" "$(check_value "$(config_value "$config")")"
    done
    printf '\n## 接口\n\n'
    printf -- '- DAMON admin：`%s`，存在=%s，可读=%s，可写=%s\n' "$damon_admin" \
        "$([[ -d "$damon_admin" ]] && printf true || printf false)" \
        "$([[ -r "$damon_admin" ]] && printf true || printf false)" \
        "$([[ -w "$damon_admin" ]] && printf true || printf false)"
    printf -- '- vaddr 可用：`%s`\n' "$vaddr_found"
    printf -- '- vaddr 检查结果：`%s`（未创建 DAMON context 时不会存在 `avail_operations`；使用 `--require-vaddr` 可将此项设为强制通过条件）\n' "$vaddr_check_result"
    printf -- '- tracefs：`%s`\n' "${tracefs_root:-未挂载}"
    printf -- '- damon_aggregated format：`%s`，可读=%s\n' "$tracepoint_format" \
        "$([[ -r "$tracepoint_format" ]] && printf true || printf false)"
    printf -- '- damon_aggregated enable：`%s`，可写=%s\n' "$tracepoint_enable" \
        "$([[ -w "$tracepoint_enable" ]] && printf true || printf false)"
    printf '\n本脚本只读检查，不会创建 DAMON 实例或 target，不会写 `lru_gen_pages`，也不会改变 MGLRU、Tier2 或页面保护状态。\n'
} > "$report_md"

printf 'DAMON runtime report: %s\n' "$report_json"
printf 'DAMON runtime report: %s\n' "$report_md"
printf 'final_result=%s\n' "$overall"

[[ "$overall" == PASS || "$overall" == SUPPORTED_NEEDS_ROOT ]]
