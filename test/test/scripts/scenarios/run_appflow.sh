#!/usr/bin/env bash
set -euo pipefail

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
source "$root_dir/scripts/lib/experiment.sh"
level="${1:-medium}"
data_file="${DATA_FILE:-$root_dir/data/appflow-1.2GiB.bin}"
duration="${DURATION_SECONDS:-120}"
small_mb="${SMALL_APP_MB:-100}"
large_mb="${LARGE_APP_MB:-1024}"
output="${OUTPUT_DIR:-$root_dir/results/appflow-${level}-$(date +%Y%m%d-%H%M%S)}"
unit="memexp-appflow-${level}-$RANDOM"
start_file="$output/workload-start.marker"
stop_file="$output/workload-stop.marker"
system_ready="$output/collector-ready.json"
cgroup_ready="$output/cgroup/collector-ready.json"
system_done="$output/collector-done.json"
cgroup_done="$output/cgroup/collector-done.json"
mkdir -p "$output"
rm -f "$start_file" "$stop_file" "$system_ready" "$cgroup_ready" "$system_done" "$cgroup_done"
require_memory_ceiling "${MAX_TEST_MEMORY_GB:-8}" "$output"

case "$level" in
  low) small_count=5; large_count=0 ;;
  medium) small_count=15; large_count=0 ;;
  high) small_count=15; large_count=2 ;;
  *) echo "level must be low, medium, or high" >&2; exit 2 ;;
esac

if ! python3 "$root_dir/workloads/io_cold_launch.py" verify --path "$data_file" --size-gb 1.2 >/dev/null; then
  python3 "$root_dir/workloads/io_cold_launch.py" create --path "$data_file" --size-gb 1.2
fi

workers=()
worker_logs=()
cleanup() {
  for pid in "${workers[@]:-}"; do kill -TERM "$pid" 2>/dev/null || true; done
  [[ -z "${collector_pid:-}" ]] || kill -TERM "$collector_pid" 2>/dev/null || true
  [[ -z "${cgroup_collector_pid:-}" ]] || kill -TERM "$cgroup_collector_pid" 2>/dev/null || true
  [[ -z "${service_launcher_pid:-}" ]] || kill -TERM "$service_launcher_pid" 2>/dev/null || true
  systemctl --user stop "$unit.service" 2>/dev/null || true
  systemctl --user reset-failed "$unit.service" 2>/dev/null || true
  cancel_bpf_collector
  wait 2>/dev/null || true
}
trap cleanup EXIT INT TERM

for ((index=0; index<small_count; index++)); do
  python3 "$root_dir/workloads/memory_worker.py" --mb "$small_mb" \
    --duration "$((duration + 60))" --label "appflow-small-$index" \
    --mode resident-idle \
    >"$output/small-$index.jsonl" &
  workers+=("$!")
  worker_logs+=("$output/small-$index.jsonl")
done
for ((index=0; index<large_count; index++)); do
  python3 "$root_dir/workloads/memory_worker.py" --mb "$large_mb" \
    --duration "$((duration + 60))" --label "appflow-large-$index" \
    --mode resident-idle \
    >"$output/large-$index.jsonl" &
  workers+=("$!")
  worker_logs+=("$output/large-$index.jsonl")
done
for ((attempt=0; attempt<600; attempt++)); do
  ready_count=0
  for log in "${worker_logs[@]}"; do
    grep -q '"event": "ready"' "$log" 2>/dev/null && ((ready_count+=1)) || true
  done
  [[ "$ready_count" == "${#worker_logs[@]}" ]] && break
  for pid in "${workers[@]}"; do
    kill -0 "$pid" 2>/dev/null || { echo "background worker exited before ready" >&2; exit 4; }
  done
  sleep 0.1
done
if [[ "$ready_count" != "${#worker_logs[@]}" ]]; then
  echo "timed out waiting for AppFlow background workers" >&2
  exit 4
fi
record_memory_state "$output/memory-pressure-loaded.json"

if [[ "${DROP_CACHES:-0}" == "1" ]]; then
  sync
  echo 3 | sudo tee /proc/sys/vm/drop_caches >/dev/null
fi
python3 -m memsched_exp.cache_state evict \
  --path "$data_file" --max-resident-ratio "${MAX_COLD_RESIDENT_RATIO:-0.01}" \
  --output "$output/cache-eviction.json"
manifest_path="$(create_run_manifest "$output" "appflow-$level" strict-cold "${EXPERIMENT_REPETITION:-1}")"
manifest_args=()
[[ -z "$manifest_path" ]] || manifest_args=(--manifest-file "$manifest_path")

start_bpf_collector "$duration" "$output" "$start_file" "$stop_file"

python3 -m memsched_exp.cli collect \
  --name "appflow-$level" --duration "$duration" --interval 0.5 \
  --scenario appflow --cache-state strict-cold --ready-file "$system_ready" \
  "${manifest_args[@]}" \
  --start-file "$start_file" --stop-file "$stop_file" --done-file "$system_done" \
  --output "$output" &
collector_pid=$!

systemd-run --user --unit="$unit" --collect --pipe --wait --quiet \
  --property=MemoryAccounting=yes --property=CPUAccounting=yes --property=IOAccounting=yes \
  -- python3 -m memsched_exp.start_marker --marker "$start_file" \
    --ready-file "$system_ready" --ready-file "$cgroup_ready" \
    --stop-marker "$stop_file" --done-file "$system_done" --done-file "$cgroup_done" \
    --done-timeout 60 -- python3 "$root_dir/workloads/io_cold_launch.py" read \
      --path "$data_file" --block-kb "${BLOCK_KB:-128}" \
  >"$output/cold-launch.jsonl" 2>"$output/cold-launch.stderr" &
service_launcher_pid=$!

cgroup_path=""
for _ in {1..300}; do
  control_group="$(systemctl --user show "$unit.service" -p ControlGroup --value 2>/dev/null || true)"
  if [[ -n "$control_group" && -d "/sys/fs/cgroup$control_group" ]]; then
    cgroup_path="/sys/fs/cgroup$control_group"
    break
  fi
  sleep 0.02
done
if [[ -z "$cgroup_path" ]]; then
  echo "Could not resolve AppFlow target cgroup" >"$output/cgroup.error"
  exit 4
fi
printf '%s\n' "$cgroup_path" >"$output/cgroup.path"
python3 -m memsched_exp.cli collect \
  --name "appflow-$level-target" --duration "$duration" --interval 0.5 \
  --scenario appflow --cache-state strict-cold --ready-file "$cgroup_ready" \
  "${manifest_args[@]}" \
  --start-file "$start_file" --stop-file "$stop_file" --done-file "$cgroup_done" \
  --cgroup "$cgroup_path" --output "$output/cgroup" &
cgroup_collector_pid=$!

wait "$service_launcher_pid"
wait "$collector_pid"
wait "$cgroup_collector_pid"
wait_bpf_collector
echo "$output"
