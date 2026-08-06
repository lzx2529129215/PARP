#!/usr/bin/env bash
set -euo pipefail

# A and B are independent capacity probes.  The zero-ballast three-App
# baseline is an additional hard gate before C; the suite never increases a
# safety threshold to force an allocation run.
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
run_phase() {
  local phase="$1" output session status
  output="$(TEST4B_CAL_PHASE="$phase" bash "$ROOT/runtime_monitor/scripts/run_test4b_calibration.sh")"
  session="$(printf '%s\n' "$output" | tail -n 1)"
  status="$(python3 - "$session/review/calibration_report.json" <<'PY'
import json,sys
print(json.load(open(sys.argv[1], encoding='utf-8'))['status'])
PY
)"
  printf '%s %s %s\n' "$phase" "$status" "$session"
  [ "$status" = "CALIBRATED_SAFE" ]
}

run_phase A_FILE
run_phase B_ANON
run_phase C_BASELINE_STABLE
run_phase C_MIXED
