#!/usr/bin/env bash
set -euo pipefail

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
output="${1:-$root_dir/results/strict-cgroup-smoke-$(date +%Y%m%d-%H%M%S)}"
duration="${DURATION_SECONDS:-1.0}"

if [[ -e "$output" ]]; then
  echo "Output path already exists: $output" >&2
  exit 2
fi
if ! sudo -n true; then
  echo "sudo credentials are not cached; run sudo -v first" >&2
  exit 3
fi
mkdir -p "$(dirname "$output")"

bash "$root_dir/scripts/enable_runtime_accounting.sh"

# A transient system service provides a dedicated cgroup with memory/cpu/io
# accounting even when the desktop user manager delegates only memory/pids.
unit="memsched-strict-cgroup-$$"
sudo -n systemd-run --wait --collect --pipe --quiet \
  --unit="$unit" \
  --uid="$(id -u)" \
  --gid="$(id -g)" \
  --property=MemoryAccounting=yes \
  --property=CPUAccounting=yes \
  --property=IOAccounting=yes \
  --setenv="PYTHONPATH=$root_dir" \
  /usr/bin/python3 "$root_dir/tests/integration/linux_pipeline.py" \
    --output "$output" --duration "$duration" --current-cgroup

python3 - "$output/run/summary.json" <<'PY'
import json
import sys
from pathlib import Path

summary = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
cgroup = summary.get("cgroup", {})
if not cgroup.get("valid"):
    raise SystemExit("strict cgroup smoke failed: " + "; ".join(cgroup.get("invalid_reasons", [])))
print(json.dumps({"strict_cgroup_valid": True, "output": str(Path(sys.argv[1]).parent)}, ensure_ascii=False))
PY
