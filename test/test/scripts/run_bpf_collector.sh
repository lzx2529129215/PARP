#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 2 || $# -gt 5 ]]; then
  echo "usage: $0 DURATION_SECONDS OUTPUT_DIRECTORY [READY_FILE] [START_FILE] [STOP_FILE]" >&2
  exit 2
fi

duration="$1"
output="$2"
root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ready_file="${3:-$output/bpf.ready}"
start_file="${4:-}"
stop_file="${5:-}"
effective_duration="$duration"
[[ -z "$start_file" ]] || effective_duration="$(python3 -c 'import sys; print(float(sys.argv[1]) + 5.0)' "$duration")"
mkdir -p "$output"
rm -f "$ready_file"
raw_events="$output/reclaim-events.raw.jsonl"
events="$output/reclaim-events.jsonl"
stderr_file="$output/reclaim-bpf.stderr"
rm -f "$raw_events" "$events" "$stderr_file"

if ! command -v bpftrace >/dev/null 2>&1; then
  echo "bpftrace is required; vmstat/cgroup collection can still run without it" >&2
  exit 3
fi

if ! sudo -n true; then
  echo "sudo credentials are not cached; run sudo -v before starting the background collector" >&2
  exit 4
fi

# Older bpftrace releases do not fully discover split source/output kernel
# trees. Build an explicit include search path when matching local headers are
# available; distro kernels with BTF continue to work without these arguments.
bpftrace_env=()
bpftrace_args=()
kernel_release="$(uname -r)"
kernel_build="$(readlink -f "/lib/modules/$kernel_release/build" 2>/dev/null || true)"
kernel_source=""
if [[ -n "$kernel_build" && -d "$kernel_build" ]]; then
  kernel_source="$(readlink -f "$kernel_build/source" 2>/dev/null || true)"
  [[ -n "$kernel_source" && -d "$kernel_source" ]] || kernel_source="$kernel_build"
  bpftrace_env+=("BPFTRACE_KERNEL_SOURCE=$kernel_source")
  case "$(uname -m)" in
    x86_64|i?86) kernel_arch=x86 ;;
    aarch64) kernel_arch=arm64 ;;
    arm*) kernel_arch=arm ;;
    ppc64*) kernel_arch=powerpc ;;
    s390x) kernel_arch=s390 ;;
    riscv*) kernel_arch=riscv ;;
    *) kernel_arch="$(uname -m)" ;;
  esac
  for include_dir in \
    "$kernel_build/include" \
    "$kernel_build/include/generated" \
    "$kernel_build/include/generated/uapi" \
    "$kernel_build/arch/$kernel_arch/include/generated" \
    "$kernel_build/arch/$kernel_arch/include/generated/uapi" \
    "$kernel_source/include" \
    "$kernel_source/include/uapi" \
    "$kernel_source/arch/$kernel_arch/include" \
    "$kernel_source/arch/$kernel_arch/include/uapi"; do
    [[ ! -d "$include_dir" ]] || bpftrace_args+=(-I "$include_dir")
  done
fi

sudo -n env "${bpftrace_env[@]}" timeout --signal=INT --kill-after=5 "${effective_duration}s" \
  stdbuf -oL bpftrace "${bpftrace_args[@]}" "$root_dir/bpf/reclaim.bt" \
  >"$raw_events" 2>"$stderr_file" &
collector_pid=$!
collector_start_ns=""
cleanup_collector() {
  if kill -0 "$collector_pid" 2>/dev/null; then
    sudo -n kill -INT "$collector_pid" 2>/dev/null || kill -INT "$collector_pid" 2>/dev/null || true
    wait "$collector_pid" 2>/dev/null || true
  fi
}
trap cleanup_collector EXIT INT TERM
for _ in {1..300}; do
  if grep -Eq '^Attaching [0-9]+ probes?\.\.\.$' "$raw_events" "$stderr_file" 2>/dev/null; then
    collector_start_ns="$(python3 -c 'import time; print(time.monotonic_ns())')"
    touch "$ready_file"
    break
  fi
  if ! kill -0 "$collector_pid" 2>/dev/null; then
    wait "$collector_pid" || status=$?
    echo "bpftrace exited before reporting collector_start" >&2
    exit "${status:-4}"
  fi
  sleep 0.1
done
if [[ ! -e "$ready_file" ]]; then
  kill -TERM "$collector_pid" 2>/dev/null || true
  wait "$collector_pid" || true
  echo "timed out waiting for bpftrace collector_start" >&2
  exit 4
fi
if [[ -n "$stop_file" ]]; then
  while kill -0 "$collector_pid" 2>/dev/null && [[ ! -e "$stop_file" ]]; do
    sleep 0.1
  done
  if kill -0 "$collector_pid" 2>/dev/null; then
    sleep "${BPF_STOP_GRACE_SECONDS:-0.5}"
    sudo -n kill -INT "$collector_pid" 2>/dev/null || kill -INT "$collector_pid" 2>/dev/null || true
  fi
fi
wait "$collector_pid" || status=$?
trap - EXIT INT TERM
status="${status:-0}"
if [[ "$status" -ne 0 && "$status" -ne 124 && "$status" -ne 130 ]]; then
  exit "$status"
fi
collector_stop_ns="$(python3 -c 'import time; print(time.monotonic_ns())')"
{
  printf '{"type":"collector_start","ts_ns":%s,"source":"userspace_lifecycle"}\n' "$collector_start_ns"
  [[ ! -f "$raw_events" ]] || sed -n '/^{/p' "$raw_events"
  printf '{"type":"collector_stop","ts_ns":%s,"source":"userspace_lifecycle"}\n' "$collector_stop_ns"
} >"$events"
parser_args=(
  --input "$events"
  --stderr "$stderr_file"
  --output "$output/reclaim-events-summary.json"
)
if [[ -n "$start_file" ]]; then
  parser_args+=(--start-file "$start_file" --duration "$duration")
fi
if [[ -n "$stop_file" ]]; then
  parser_args+=(--stop-file "$stop_file")
fi
python3 -m memsched_exp.bpf_events "${parser_args[@]}"
