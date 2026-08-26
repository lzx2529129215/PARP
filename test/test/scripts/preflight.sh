#!/usr/bin/env bash
set -euo pipefail

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
profile="core"
required_kernel_prefix="${MEMSCHED_REQUIRED_KERNEL_PREFIX:-}"
policy_adapter="none"

usage() {
  cat <<'EOF'
Usage: scripts/preflight.sh [options]

Options:
  --profile core|gui|bpf|all       Capability profile to validate (default: core)
  --require-kernel-prefix PREFIX   Optional release prefix for a controlled experiment
  --policy-adapter none|parp       Optional policy interface to validate (default: none)
  -h, --help                       Show this help

The default core profile is kernel-version independent and does not require PARP.
EOF
}

while (($#)); do
  case "$1" in
    --profile)
      profile="${2:?--profile requires a value}"
      shift 2
      ;;
    --require-kernel-prefix)
      required_kernel_prefix="${2:?--require-kernel-prefix requires a value}"
      shift 2
      ;;
    --policy-adapter)
      policy_adapter="${2:?--policy-adapter requires a value}"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

case "$profile" in
  core|gui|bpf|all) ;;
  *)
    echo "Unsupported profile: $profile" >&2
    exit 2
    ;;
esac

case "$policy_adapter" in
  none|parp) ;;
  *)
    echo "Unsupported policy adapter: $policy_adapter" >&2
    exit 2
    ;;
esac

fail=0
tracepoints_ok=1
kernel="$(uname -r)"
if [[ -n "$required_kernel_prefix" && "$kernel" != "$required_kernel_prefix"* ]]; then
  echo "FAIL kernel: expected prefix $required_kernel_prefix, got $kernel" >&2
  fail=1
else
  echo "OK kernel discovered: $kernel"
fi

if [[ "$(stat -fc %T /sys/fs/cgroup 2>/dev/null || true)" != "cgroup2fs" ]]; then
  echo "FAIL cgroup: unified cgroup v2 is required" >&2
  fail=1
else
  echo "OK cgroup v2"
  controllers="$(cat /sys/fs/cgroup/cgroup.controllers 2>/dev/null || true)"
  for controller in memory cpu io; do
    if [[ " $controllers " == *" $controller "* ]]; then
      echo "OK cgroup controller: $controller"
    else
      echo "FAIL missing cgroup controller: $controller" >&2
      fail=1
    fi
  done
fi

for file in /proc/vmstat /proc/pressure/memory; do
  if [[ -r "$file" ]]; then
    echo "OK readable: $file"
  else
    echo "FAIL unreadable: $file" >&2
    fail=1
  fi
done

trace_root=/sys/kernel/tracing
if [[ ! -d "$trace_root/events" && -d /sys/kernel/debug/tracing/events ]]; then
  trace_root=/sys/kernel/debug/tracing
fi
for event in \
  vmscan/mm_vmscan_direct_reclaim_begin \
  vmscan/mm_vmscan_direct_reclaim_end \
  vmscan/mm_vmscan_kswapd_wake \
  vmscan/mm_vmscan_kswapd_sleep \
  oom/mark_victim; do
  if [[ -e "$trace_root/events/$event/id" ]]; then
    echo "OK tracepoint: $event"
  else
    echo "WARN missing tracepoint: $event (vmstat fallback remains available)" >&2
    tracepoints_ok=0
  fi
done

if command -v python3 >/dev/null 2>&1; then
  echo "OK command: python3"
else
  echo "FAIL missing command: python3" >&2
  fail=1
fi

if PYTHONPATH="$root_dir${PYTHONPATH:+:$PYTHONPATH}" \
  python3 -c 'import memsched_exp.cli, memsched_exp.protocol, memsched_exp.schema' >/dev/null 2>&1; then
  echo "OK memsched_exp package (installed or source tree)"
else
  echo "FAIL memsched_exp package cannot be imported" >&2
  fail=1
fi

if python3 -c 'import os; raise SystemExit(0 if hasattr(os, "posix_fadvise") and hasattr(os, "POSIX_FADV_DONTNEED") else 1)' >/dev/null 2>&1; then
  echo "OK per-file cold-cache eviction"
else
  echo "WARN Python lacks POSIX_FADV_DONTNEED; strict file-cache validation is unavailable" >&2
fi

if [[ "$profile" == "gui" || "$profile" == "all" ]]; then
  for command in systemd-run wmctrl; do
    if command -v "$command" >/dev/null 2>&1; then
      echo "OK GUI command: $command"
    else
      echo "FAIL missing GUI command: $command" >&2
      fail=1
    fi
  done
  if systemctl --user show-environment >/dev/null 2>&1; then
    echo "OK systemd user manager"
  else
    echo "FAIL systemd user manager is unavailable" >&2
    fail=1
  fi
fi

if command -v bpftrace >/dev/null 2>&1; then
  echo "OK command: bpftrace"
else
  echo "WARN missing command: bpftrace (exact reclaim-event collection disabled)" >&2
  if [[ "$profile" == "bpf" || "$profile" == "all" ]]; then
    fail=1
  fi
fi
if [[ "$profile" == "bpf" || "$profile" == "all" ]]; then
  if command -v stdbuf >/dev/null 2>&1; then
    echo "OK command: stdbuf"
  else
    echo "FAIL missing command: stdbuf (required for bpftrace readiness signaling)" >&2
    fail=1
  fi
fi
if [[ "$tracepoints_ok" != "1" && ("$profile" == "bpf" || "$profile" == "all") ]]; then
  echo "FAIL required reclaim/OOM tracepoints are unavailable for the bpf profile" >&2
  fail=1
fi

if [[ "$policy_adapter" == "parp" ]]; then
  policy_root="${POLICY_DEBUGFS_ROOT:-/sys/kernel/debug/parp}"
  for file in effective_tier_mode effective_tier_stats effective_tier_config; do
    if [[ -r "$policy_root/$file" ]]; then
      echo "OK optional PARP policy state: $policy_root/$file"
    else
      echo "FAIL unreadable optional PARP policy state: $policy_root/$file" >&2
      fail=1
    fi
  done
fi

exit "$fail"
