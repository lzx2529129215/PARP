#!/usr/bin/env bash
set -euo pipefail

TRACEFS=/sys/kernel/tracing
operation="${1:-}"
instance="${2:-}"
buffer_kb="${3:-16384}"

if [[ ! "$instance" =~ ^parp-accept-[a-zA-Z0-9_-]+$ ]]; then
  echo "invalid trace instance: $instance" >&2
  exit 2
fi

path="$TRACEFS/instances/$instance"

set_events() {
  local value="$1"
  local event
  # Include memcg reclaim timing in the acceptance trace. #lzx
  for event in \
    exceptions/page_fault_user \
    exceptions/page_fault_kernel \
    parp/parp_effective_tier_decision \
    parp/parp_effective_tier_access \
    parp/parp_effective_tier_outcome \
    vmscan/mm_vmscan_direct_reclaim_begin \
    vmscan/mm_vmscan_direct_reclaim_end \
    vmscan/mm_vmscan_memcg_reclaim_begin \
    vmscan/mm_vmscan_memcg_reclaim_end \
    vmscan/mm_vmscan_kswapd_wake \
    vmscan/mm_vmscan_kswapd_sleep; do
    if [[ -e "$path/events/$event/enable" ]]; then
      printf '%s\n' "$value" >"$path/events/$event/enable"
    fi
  done
}

case "$operation" in
  setup)
    mkdir -p "$path"
    printf '0\n' >"$path/tracing_on"
    printf 'nop\n' >"$path/current_tracer"
    printf '%s\n' "$buffer_kb" >"$path/buffer_size_kb"
    printf '1\n' >"$path/options/record-tgid" 2>/dev/null || true
    set_events 0
    : >"$path/trace"
    ;;
  enable)
    set_events 1
    printf '1\n' >"$path/tracing_on"
    ;;
  disable)
    [[ -d "$path" ]] || exit 0
    printf '0\n' >"$path/tracing_on"
    set_events 0
    ;;
  filter-pids)
    pid_csv="${3:-}"
    if [[ ! "$pid_csv" =~ ^[0-9]+(,[0-9]+)*$ ]]; then
      echo "invalid pid list" >&2
      exit 2
    fi
    filter=""
    IFS=',' read -r -a pids <<<"$pid_csv"
    for pid in "${pids[@]}"; do
      if [[ -n "$filter" ]]; then filter+=" || "; fi
      filter+="common_pid == $pid"
    done
    for event in exceptions/page_fault_user exceptions/page_fault_kernel; do
      if [[ -e "$path/events/$event/filter" ]]; then
        printf '%s\n' "$filter" >"$path/events/$event/filter"
      fi
    done
    ;;
  stream)
    exec cat "$path/trace_pipe"
    ;;
  stop-stream)
    pkill -TERM -f "^cat ${path}/trace_pipe$" 2>/dev/null || true
    ;;
  stats)
    find "$path/per_cpu" -mindepth 2 -maxdepth 2 -name stats -print0 \
      | sort -z | xargs -0 -r -n1 sh -c 'echo "===$0"; cat "$0"'
    ;;
  cleanup)
    if [[ -d "$path" ]]; then
      printf '0\n' >"$path/tracing_on" 2>/dev/null || true
      set_events 0 || true
      rmdir "$path"
    fi
    ;;
  *)
    echo "usage: $0 {setup|enable|disable|filter-pids|stream|stop-stream|stats|cleanup} INSTANCE [ARG]" >&2
    exit 2
    ;;
esac
