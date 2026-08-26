#!/usr/bin/env bash
set -euo pipefail

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
source "$root_dir/scripts/lib/experiment.sh"
background_count="${1:-8}"
duration="${DURATION_SECONDS:-300}"
foreground_hold_seconds="$((duration + 60))"
foreground_command="${2:-python3 $root_dir/workloads/memory_worker.py --mb 256 --duration $foreground_hold_seconds --mode active --label foreground}"
background_mb="${BACKGROUND_MB:-180}"
output="${OUTPUT_DIR:-$root_dir/results/acclaim-bg${background_count}-$(date +%Y%m%d-%H%M%S)}"
unit="memexp-acclaim-fg-$RANDOM"
start_file="$output/foreground-start.marker"
system_ready="$output/collector-ready.json"
cgroup_ready="$output/foreground-cgroup/collector-ready.json"
system_done="$output/collector-done.json"
cgroup_done="$output/foreground-cgroup/collector-done.json"
mkdir -p "$output"
rm -f "$start_file" "$system_ready" "$cgroup_ready" "$system_done" "$cgroup_done"
require_memory_ceiling "${MAX_TEST_MEMORY_GB:-4}" "$output"

workers=()
worker_logs=()
cleanup() {
  for pid in "${workers[@]:-}"; do kill -TERM "$pid" 2>/dev/null || true; done
  [[ -z "${collector_pid:-}" ]] || kill -TERM "$collector_pid" 2>/dev/null || true
  [[ -z "${cgroup_collector_pid:-}" ]] || kill -TERM "$cgroup_collector_pid" 2>/dev/null || true
  systemctl --user stop "$unit.service" 2>/dev/null || true
  systemctl --user reset-failed "$unit.service" 2>/dev/null || true
  cancel_bpf_collector
  wait 2>/dev/null || true
}
trap cleanup EXIT INT TERM

for ((index=0; index<background_count; index++)); do
  python3 "$root_dir/workloads/memory_worker.py" \
    --mb "$background_mb" --duration "$((duration + 60))" --mode resident-idle \
    --label "acclaim-bg-$index" >"$output/background-$index.jsonl" &
  workers+=("$!")
  worker_logs+=("$output/background-$index.jsonl")
done
for ((attempt=0; attempt<600; attempt++)); do
  ready_count=0
  for log in "${worker_logs[@]}"; do
    grep -q '"event": "ready"' "$log" 2>/dev/null && ((ready_count+=1)) || true
  done
  [[ "$ready_count" == "$background_count" ]] && break
  for pid in "${workers[@]}"; do
    kill -0 "$pid" 2>/dev/null || { echo "background worker exited before ready" >&2; exit 4; }
  done
  sleep 0.1
done
if [[ "$ready_count" != "$background_count" ]]; then
  echo "timed out waiting for Acclaim background workers" >&2
  exit 4
fi
sleep "${BACKGROUND_STABILIZE_SECONDS:-10}"
record_memory_state "$output/memory-pressure-loaded.json"
manifest_path="$(create_run_manifest "$output" "acclaim-bg${background_count}" warm "${EXPERIMENT_REPETITION:-1}")"
manifest_args=()
[[ -z "$manifest_path" ]] || manifest_args=(--manifest-file "$manifest_path")

python3 -m memsched_exp.cli collect \
  --name "acclaim-bg${background_count}" --duration "$duration" --interval 1 \
  --scenario acclaim --cache-state warm --start-file "$start_file" \
  "${manifest_args[@]}" \
  --ready-file "$system_ready" --done-file "$system_done" \
  --output "$output" &
collector_pid=$!
start_bpf_collector "$duration" "$output" "$start_file"

systemd-run --user --unit="$unit" --collect \
  --property=MemoryAccounting=yes --property=CPUAccounting=yes --property=IOAccounting=yes \
  -- python3 -m memsched_exp.start_marker --marker "$start_file" \
    --ready-file "$system_ready" --ready-file "$cgroup_ready" -- bash -lc "$foreground_command"

cgroup_path=""
for _ in {1..300}; do
  control_group="$(systemctl --user show "$unit.service" -p ControlGroup --value 2>/dev/null || true)"
  if [[ -n "$control_group" && -d "/sys/fs/cgroup$control_group" ]]; then
    cgroup_path="/sys/fs/cgroup$control_group"
    break
  fi
  sleep 0.02
done
cgroup_collector_pid=""
if [[ -n "$cgroup_path" ]]; then
  printf '%s\n' "$cgroup_path" >"$output/foreground-cgroup.path"
  python3 -m memsched_exp.cli collect \
    --name "acclaim-bg${background_count}-foreground" --duration "$duration" --interval 1 \
    --scenario acclaim --cache-state warm --start-file "$start_file" \
    "${manifest_args[@]}" \
    --ready-file "$cgroup_ready" --done-file "$cgroup_done" \
    --cgroup "$cgroup_path" --output "$output/foreground-cgroup" &
  cgroup_collector_pid=$!
else
  echo "Could not resolve foreground service cgroup" >"$output/foreground-cgroup.error"
  exit 4
fi

main_pid="$(systemctl --user show "$unit.service" -p MainPID --value 2>/dev/null || true)"
printf '%s\n' "$main_pid" >"$output/foreground.pid"
wait "$collector_pid"
[[ -z "$cgroup_collector_pid" ]] || wait "$cgroup_collector_pid" || true
wait_bpf_collector
systemctl --user stop "$unit.service" 2>/dev/null || true
echo "$output"
