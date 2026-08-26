#!/usr/bin/env bash

start_bpf_collector() {
  local duration="$1"
  local output="$2"
  local start_file="${3:-}"
  local stop_file="${4:-}"
  BPF_PID=""
  if ! command -v bpftrace >/dev/null 2>&1; then
    return 0
  fi
  sudo -v
  local ready_file="$output/bpf.ready"
  rm -f "$ready_file"
  bash "$root_dir/scripts/run_bpf_collector.sh" "$duration" "$output" "$ready_file" "$start_file" "$stop_file" &
  BPF_PID=$!
  for _ in {1..300}; do
    [[ -e "$ready_file" ]] && return 0
    if ! kill -0 "$BPF_PID" 2>/dev/null; then
      wait "$BPF_PID" || true
      echo "bpftrace collector exited before probes became ready" >&2
      return 4
    fi
    sleep 0.1
  done
  kill -TERM "$BPF_PID" 2>/dev/null || true
  wait "$BPF_PID" || true
  echo "timed out waiting for bpftrace probe readiness" >&2
  return 4
}

wait_bpf_collector() {
  if [[ -n "${BPF_PID:-}" ]]; then
    if ! wait "$BPF_PID"; then
      echo "WARN: eBPF summary is invalid; inspect reclaim-events-summary.json" >&2
    fi
  fi
}

cancel_bpf_collector() {
  if [[ -n "${BPF_PID:-}" ]] && kill -0 "$BPF_PID" 2>/dev/null; then
    kill -TERM "$BPF_PID" 2>/dev/null || true
    wait "$BPF_PID" 2>/dev/null || true
  fi
}

require_memory_ceiling() {
  local max_gb="$1"
  local output="$2"
  local args=(--max-memory-gb "$max_gb" --output "$output/memory-pressure-baseline.json")
  if [[ "${ALLOW_UNCONSTRAINED_MEMORY:-0}" == "1" ]]; then
    args+=(--allow-unconstrained)
  fi
  python3 -m memsched_exp.pressure_guard "${args[@]}"
}

record_memory_state() {
  python3 -m memsched_exp.pressure_guard \
    --max-memory-gb 1000000 --allow-unconstrained --output "$1"
}

wait_for_protocol_marker() {
  local marker="$1"
  local timeout="${2:-60}"
  python3 -m memsched_exp.protocol wait --path "$marker" --timeout "$timeout" >/dev/null
}

write_protocol_marker() {
  local marker="$1"
  local event="$2"
  python3 -m memsched_exp.protocol mark --path "$marker" --event "$event" >/dev/null
}

create_run_manifest() {
  local output="$1"
  local scenario="$2"
  local cache_state="$3"
  local repetition="${4:-1}"
  if [[ -z "${EXPERIMENT_VARIANT:-}" ]]; then
    printf '\n'
    return 0
  fi
  if [[ -z "${EXPERIMENT_SEED:-}" ]]; then
    echo "EXPERIMENT_SEED is required when EXPERIMENT_VARIANT is set" >&2
    return 2
  fi
  local seed=$((EXPERIMENT_SEED + repetition - 1))
  local manifest="$output/manifest.json"
  local policy_state="$output/policy-state.json"
  local args=(
    --variant "$EXPERIMENT_VARIANT" --scenario "$scenario" --seed "$seed"
    --repetition "$repetition" --cache-state "$cache_state" --output "$manifest"
  )
  [[ -z "${KERNEL_COMMIT:-}" ]] || args+=(--kernel-commit "$KERNEL_COMMIT")
  [[ -z "${POLICY_MODE:-}" ]] || args+=(--policy-mode "$POLICY_MODE")
  [[ -z "${APPLY_COMPILED:-}" ]] || args+=(--apply-compiled "$APPLY_COMPILED")
  [[ -z "${MODEL_PROVENANCE:-}" ]] || args+=(--model-provenance "$MODEL_PROVENANCE")
  local policy_adapter="${POLICY_ADAPTER:-none}"
  if [[ "$policy_adapter" == "parp" ]]; then
    local policy_root="${POLICY_DEBUGFS_ROOT:-/sys/kernel/debug/parp}"
    if [[ ! -d "$policy_root" ]]; then
      echo "PARP policy adapter requested but debugfs root is unavailable: $policy_root" >&2
      return 2
    fi
    local policy_args=(--root "$policy_root" --output "$policy_state")
    [[ -z "${POLICY_MODE:-}" ]] || policy_args+=(--expected-mode "$POLICY_MODE")
    [[ -z "${APPLY_COMPILED:-}" ]] || policy_args+=(--expected-apply-compiled "$APPLY_COMPILED")
    [[ -z "${MODEL_PROVENANCE:-}" ]] || policy_args+=(--expected-model-provenance "$MODEL_PROVENANCE")
    python3 -m memsched_exp.policy_state "${policy_args[@]}" >/dev/null
    args+=(--policy-state-file "$policy_state")
  elif [[ "$policy_adapter" != "none" ]]; then
    echo "Unsupported POLICY_ADAPTER: $policy_adapter" >&2
    return 2
  fi
  python3 -m memsched_exp.schema "${args[@]}" >/dev/null
  printf '%s\n' "$manifest"
}
