#!/usr/bin/env bash
# Observe-only trace collector.  Application automation is intentionally external.
set -euo pipefail

usage() {
	printf 'usage: %s --output DIR --run-id ID --duration SEC\n' "$0" >&2
}

output=
run_id=
duration=
while (($#)); do
	case "$1" in
	--output) output=${2:?}; shift 2 ;;
	--run-id) run_id=${2:?}; shift 2 ;;
	--duration) duration=${2:?}; shift 2 ;;
	*) usage; exit 2 ;;
	esac
done

[[ -n $output && -n $run_id && $duration =~ ^[1-9][0-9]*$ ]] || {
	usage
	exit 2
}
[[ $EUID -eq 0 ]] || {
	printf 'ERROR: trace collection requires root\n' >&2
	exit 77
}
[[ $run_id =~ ^[A-Za-z0-9_-]+$ ]] || {
	printf 'ERROR: unsafe run id\n' >&2
	exit 2
}

trace_root=/sys/kernel/tracing
format=$trace_root/events/parp/parp_region_evidence/format
[[ -r $format ]] || {
	printf 'PARP_PHASE27_KERNEL_TRACE_EXTENSION_REBUILD_REQUIRED: no trace format\n' >&2
	exit 42
}
for field in sample_timestamp_ns bind_generation foreground_epoch_id model_version \
	region_start region_end dev_major dev_minor inode file_version \
	file_size_bytes file_page_count vma_signature sample_interval_us \
	aggregation_interval_us; do
	grep -Eq "field:.*[[:space:]]${field};" "$format" || {
		printf 'PARP_PHASE27_KERNEL_TRACE_EXTENSION_REBUILD_REQUIRED: missing %s\n' "$field" >&2
		exit 42
	}
done

for mode_file in /sys/kernel/debug/parp/mode \
	/sys/kernel/debug/parp/scan_budget_mode \
	/sys/kernel/debug/parp/page_level_mode; do
	if [[ -r $mode_file ]] && ! grep -Eiq 'observe|disabled' "$mode_file"; then
		printf 'ERROR: non-Observe PARP mode at %s\n' "$mode_file" >&2
		exit 78
	fi
done

instance=$trace_root/instances/parp_phase27_${run_id}
[[ ! -e $instance ]] || {
	printf 'ERROR: trace instance already exists: %s\n' "$instance" >&2
	exit 73
}
mkdir -p "$output/raw/trace" "$output/state" "$instance"
trace_pid=
cleanup() {
	set +e
	printf 0 > "$instance/tracing_on"
	if [[ -n ${trace_pid:-} ]]; then
		kill "$trace_pid" 2>/dev/null
		wait "$trace_pid" 2>/dev/null
	fi
	for enable in "$instance"/events/parp/enable "$instance"/events/damon/enable; do
		[[ -e $enable ]] && printf 0 > "$enable"
	done
	rmdir "$instance" 2>/dev/null
}
trap cleanup EXIT INT TERM

printf mono > "$instance/trace_clock"
printf 32768 > "$instance/buffer_size_kb"
printf 1 > "$instance/events/parp/enable"
[[ -e $instance/events/damon/damon_aggregated/enable ]] && \
	printf 1 > "$instance/events/damon/damon_aggregated/enable"
printf 1 > "$instance/tracing_on"
cat "$instance/trace_pipe" > "$output/raw/trace/parp_phase27.raw" &
trace_pid=$!
start_ns=$(date +%s%N)
sleep "$duration"
end_ns=$(date +%s%N)
printf 0 > "$instance/tracing_on"

lost=0
if [[ -r $instance/per_cpu/cpu0/stats ]]; then
	lost=$(awk '/overrun|dropped events/ {sum += $NF} END {print sum + 0}' \
		"$instance"/per_cpu/cpu*/stats)
fi
printf '{"schema_version":1,"source":"RUNTIME_LEVEL3B_DATASET_FRESH","run_id":"%s","start_ns":%s,"end_ns":%s,"lost_event_count":%s,"kernel_write":false,"apply":false}\n' \
	"$run_id" "$start_ns" "$end_ns" "$lost" > "$output/state/trace_collection.json"
