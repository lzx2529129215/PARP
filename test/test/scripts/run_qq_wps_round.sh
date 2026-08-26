#!/usr/bin/env bash
set -euo pipefail

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$root_dir/scripts/lib/experiment.sh"
config="${CONFIG_FILE:-$root_dir/configs/qq_wps.json}"
output_root="${1:-$root_dir/results/qq-wps-$(date +%Y%m%d-%H%M%S)}"
mkdir -p "$output_root"

config_scalar() {
  python3 -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))[sys.argv[2]])' "$config" "$1"
}

duration="${DURATION_SECONDS:-$(config_scalar collection_duration_s)}"
interval="${SAMPLE_INTERVAL_SECONDS:-$(config_scalar sample_interval_s)}"
cold_repetitions="${COLD_REPETITIONS:-$(config_scalar cold_start_repetitions)}"
hot_repetitions="${HOT_REPETITIONS:-$(config_scalar hot_start_repetitions)}"
drop_caches="${DROP_CACHES:-0}"
mapfile -t app_records < <(python3 -c 'import json,sys; [print(json.dumps(v, ensure_ascii=False)) for v in json.load(open(sys.argv[1], encoding="utf-8"))["apps"]]' "$config")

bash "$root_dir/scripts/preflight.sh" --profile gui
if ((hot_repetitions != 0)); then
  echo "This runner implements the requested first cold-start round only; set hot_start_repetitions to 0." >&2
  exit 2
fi

active_unit=""
active_process_names=()
background_pids=()
cleanup_active_app() {
  for pid in "${background_pids[@]:-}"; do kill -TERM "$pid" 2>/dev/null || true; done
  cancel_bpf_collector
  if [[ -n "$active_unit" ]]; then
    systemctl --user stop "$active_unit.service" 2>/dev/null || true
    systemctl --user reset-failed "$active_unit.service" 2>/dev/null || true
  fi
  for process_name in "${active_process_names[@]:-}"; do
    [[ -n "$process_name" ]] && pkill -TERM -x "$process_name" 2>/dev/null || true
  done
  active_unit=""
  active_process_names=()
  background_pids=()
}
trap cleanup_active_app EXIT INT TERM

run_one() {
  local record="$1"
  local repetition="$2"
  local name window_regex safe_name run_dir start_file cache_state repetition_label manifest_path
  local system_ready cgroup_ready system_done cgroup_done
  local -a command process_names manifest_args
  name="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["name"])' "$record")"
  window_regex="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["window_regex"])' "$record")"
  mapfile -d '' -t command < <(python3 -c 'import json,sys; [sys.stdout.write(v+"\0") for v in json.loads(sys.argv[1])["command"]]' "$record")
  mapfile -d '' -t process_names < <(python3 -c 'import json,sys; [sys.stdout.write(v+"\0") for v in json.loads(sys.argv[1])["process_names"]]' "$record")
  safe_name="${name//[^a-zA-Z0-9]/-}"
  printf -v repetition_label '%02d' "$repetition"
  run_dir="$output_root/$name-cold-r$repetition_label"
  active_unit="memexp-$safe_name-r$repetition_label"
  active_process_names=("${process_names[@]}")
  start_file="$run_dir/app-start.marker"
  system_ready="$run_dir/system/collector-ready.json"
  cgroup_ready="$run_dir/cgroup/collector-ready.json"
  system_done="$run_dir/system/collector-done.json"
  cgroup_done="$run_dir/cgroup/collector-done.json"
  cache_state="process-cold"
  [[ "$drop_caches" == "1" ]] && cache_state="strict-cold"
  mkdir -p "$run_dir"
  rm -f "$start_file" "$system_ready" "$cgroup_ready" "$system_done" "$cgroup_done"
  manifest_path="$(create_run_manifest "$run_dir" "$name-cold" "$cache_state" "$repetition")"
  manifest_args=()
  [[ -z "$manifest_path" ]] || manifest_args=(--manifest-file "$manifest_path")

  cleanup_active_app
  active_unit="memexp-$safe_name-r$repetition_label"
  active_process_names=("${process_names[@]}")
  sleep 3
  if [[ "$drop_caches" == "1" ]]; then
    sync
    echo 3 | sudo tee /proc/sys/vm/drop_caches >/dev/null
  fi

  python3 -m memsched_exp.app_metadata --command "${command[0]}" --output "$run_dir/app-metadata.json"
  python3 -m memsched_exp.cli collect \
    --name "$name-cold-r$repetition_label" --duration "$duration" --interval "$interval" \
    --scenario qq-wps --cache-state "$cache_state" --metadata-file "$run_dir/app-metadata.json" \
    "${manifest_args[@]}" \
    --ready-file "$system_ready" --start-file "$start_file" --done-file "$system_done" \
    --output "$run_dir/system" &
  local system_collector_pid=$!
  background_pids+=("$system_collector_pid")

  start_bpf_collector "$duration" "$run_dir" "$start_file"

  python3 -m memsched_exp.launch \
    --timeout 45 --window-regex "$window_regex" --start-file "$start_file" \
    --cgroup-path-file "$run_dir/cgroup.path" \
    --output "$run_dir/launch.json" -- \
    systemd-run --user --unit="$active_unit" --collect \
      --property=MemoryAccounting=yes --property=CPUAccounting=yes \
      --property=IOAccounting=yes \
      -- python3 -m memsched_exp.start_marker --marker "$start_file" \
        --ready-file "$system_ready" --ready-file "$cgroup_ready" -- "${command[@]}" &
  local launch_probe_pid=$!
  background_pids+=("$launch_probe_pid")

  local cgroup_path=""
  for _ in {1..300}; do
    local control_group
    control_group="$(systemctl --user show "$active_unit.service" -p ControlGroup --value 2>/dev/null || true)"
    if [[ -n "$control_group" && -d "/sys/fs/cgroup$control_group" ]]; then
      cgroup_path="/sys/fs/cgroup$control_group"
      break
    fi
    sleep 0.02
  done
  local cgroup_collector_pid=""
  if [[ -n "$cgroup_path" ]]; then
    printf '%s\n' "$cgroup_path" >"$run_dir/cgroup.path"
    python3 -m memsched_exp.cli collect \
      --name "$name-cold-cgroup-r$repetition_label" --duration "$duration" --interval "$interval" \
      --scenario qq-wps --cache-state "$cache_state" --metadata-file "$run_dir/app-metadata.json" \
      "${manifest_args[@]}" \
      --ready-file "$cgroup_ready" --start-file "$start_file" --done-file "$cgroup_done" \
      --cgroup "$cgroup_path" --output "$run_dir/cgroup" &
    cgroup_collector_pid=$!
    background_pids+=("$cgroup_collector_pid")
  else
    echo "Could not resolve transient service cgroup" >"$run_dir/cgroup.error"
    return 4
  fi

  wait "$launch_probe_pid" || true
  echo "Interact with $name for the ${duration}s synchronized collection window."
  wait "$system_collector_pid"
  [[ -z "$cgroup_collector_pid" ]] || wait "$cgroup_collector_pid" || true
  wait_bpf_collector
  cleanup_active_app
  sleep 2
}

for record in "${app_records[@]}"; do
  for ((repetition=1; repetition<=cold_repetitions; repetition++)); do
    run_one "$record" "$repetition"
  done
done

echo "QQ/WPS cold-start collection complete: $output_root"
