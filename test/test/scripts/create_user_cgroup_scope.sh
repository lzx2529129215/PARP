#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 2 ]]; then
  echo "usage: $0 UNIT_NAME COMMAND [ARG ...]" >&2
  exit 2
fi
unit="$1"
shift
systemd-run --user --unit="$unit" --collect \
  --property=MemoryAccounting=yes \
  --property=CPUAccounting=yes \
  --property=IOAccounting=yes \
  -- "$@"
control_group="$(systemctl --user show "$unit.service" -p ControlGroup --value)"
printf '/sys/fs/cgroup%s\n' "$control_group"
