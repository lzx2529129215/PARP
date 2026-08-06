#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
SESSION_ID="${SESSION_ID:-session_files_001}"

cd "$PROJECT_ROOT"

mkdir -p "outputs/runtime_monitor/${SESSION_ID}/model"

automation/run_automation.sh \
  --scenario configs/automation/scenario_local_files.json \
  --trace-output "outputs/runtime_monitor/${SESSION_ID}/model/automation_trace.csv" \
  --session-id "$SESSION_ID" \
  --scenario-id scenario_local_files
