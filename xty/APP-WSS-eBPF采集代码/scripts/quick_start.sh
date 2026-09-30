#!/usr/bin/env bash
# Minimal generic collector test.  It does not require WPS or any GUI session.
set -euo pipefail

project="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
workdir="$(mktemp -d "${TMPDIR:-$project}/.quickstart.XXXXXX")"
ready_file="$workdir/workload_ready.json"
collector_dir="$workdir/collector"
run_id="quickstart-$(date +%Y%m%d-%H%M%S)"
launcher_pid=""
workload_pid=""
cgroup=""

cleanup() {
    [[ -n "$workload_pid" ]] && kill -TERM "$workload_pid" 2>/dev/null || true
    [[ -n "$launcher_pid" ]] && kill -TERM "$launcher_pid" 2>/dev/null || true
    if [[ -n "$cgroup" ]]; then
        sudo -n rmdir "$cgroup" 2>/dev/null || true
    fi
}
trap cleanup EXIT

echo "[1/6] Checking environment"
python3 "$project/scripts/check_environment.py"
echo "[2/6] Building eBPF collector"
make -C "$project"
echo "[3/6] Starting a small anonymous-memory workload in a new cgroup"
export APP_WSS_LOG_DIR="$workdir/logs"
mkdir -p "$APP_WSS_LOG_DIR"
launch_info="$(bash "$project/scripts/launch_app_in_cgroup.sh" \
    --name quickstart --run-id "$run_id" -- \
    python3 "$project/scripts/phase7_workload.py" microbench \
    --mode anon --size-mib 16 --ready-file "$ready_file" \
    --backing-dir "$workdir/workload-files" --active-seconds 1 --start-timeout 45)"
printf '%s\n' "$launch_info"
launcher_pid="$(printf '%s\n' "$launch_info" | sed -n 's/^launcher_pid=//p')"
cgroup="$(printf '%s\n' "$launch_info" | sed -n 's/^cgroup=//p')"
for _ in {1..100}; do
    [[ -s "$ready_file" ]] && break
    sleep 0.1
done
[[ -s "$ready_file" ]] || { echo "[FAIL] workload did not become ready" >&2; exit 1; }
workload_pid="$(sed -n 's/.*"pid": \([0-9][0-9]*\).*/\1/p' "$ready_file")"
[[ -n "$workload_pid" ]] || { echo "[FAIL] workload PID missing" >&2; exit 1; }
echo "[4/6] Collecting one five-second strict real-WSS window"
python3 "$project/scripts/phase8_collector_real_wss_split.py" \
    --project "$project" --cgroup "$cgroup" --run-id "$run_id" \
    --test-type QUICK_START --application synthetic-anon --operation-id touch-pages \
    --operation-name "touch anonymous pages" --output-dir "$collector_dir" \
    --window-ms 5000 --interval-ms 500 -- \
    bash -c "kill -USR1 $workload_pid; sleep 1"
echo "[5/6] Verifying summary fields"
python3 - "$collector_dir/phase8_summary.json" <<'PY'
import json
import sys

summary = json.load(open(sys.argv[1], encoding="utf-8"))
required = ("real_wss_total_bytes", "rss_total_bytes", "page_fault_total_count")
missing = [key for key in required if key not in summary]
if missing or summary.get("data_quality_status") not in {"PASS", "WARN"}:
    raise SystemExit(f"[FAIL] summary check: missing={missing}; status={summary.get('data_quality_status')}")
print("[PASS] required summary fields present")
PY
echo "[6/6] PASS: collector output is $collector_dir"
