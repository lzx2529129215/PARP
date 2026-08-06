#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
BASE_ID="${TEST4B2_SUITE_ID:-test4b2_low_pressure_shadow_$(date +%Y%m%d_%H%M%S)}"
OUT="$ROOT/outputs/runtime_monitor/${BASE_ID}_summary"
sessions=()
run_rc=0
for index in 1 2 3; do
  session="${BASE_ID}_r${index}"; sessions+=("$ROOT/outputs/runtime_monitor/$session")
  set +e
  SESSION_ID="$session" TEST4B_MODE=shadow TEST4B_RECLAIM_MODE=shadow \
    bash "$ROOT/runtime_monitor/scripts/run_test4b2_shadow.sh"
  rc=$?
  set -e
  if [ "$rc" -ne 0 ]; then run_rc=1; fi
done
set +e
python3 "$ROOT/runtime_monitor/scripts/aggregate_test4b2_shadow.py" --sessions "${sessions[@]}" --output-dir "$OUT"
aggregate_rc=$?
set -e
echo "$OUT"
if [ "$run_rc" -ne 0 ] || [ "$aggregate_rc" -ne 0 ]; then exit 1; fi
