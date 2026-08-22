#!/usr/bin/env bash
set -euo pipefail

base_dir="/home/wency/cgroup_setting"
timestamp="$(date +%Y%m%d-%H%M%S)"
unit="bilibili-sliced-${timestamp}"
events="${base_dir}/runs/${timestamp}/migration-events.jsonl"

mkdir -p "$(dirname "$events")"
systemd-run --user --unit="$unit" --property=Delegate=yes --collect \
  python3 "${base_dir}/manage_bilibili_cgroups.py" --events "$events" -- "$@"

echo "unit=${unit}.service"
echo "events=$events"
