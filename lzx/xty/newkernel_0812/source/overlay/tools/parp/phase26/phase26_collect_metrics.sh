#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0
set -euo pipefail
source "$(dirname "$0")/_common.sh"
phase26_init "$@"

duration=${PHASE26_METRICS_DURATION:-60}
interval=${PHASE26_METRICS_INTERVAL:-2}
[[ $duration =~ ^[0-9]+$ ]] && [[ $interval =~ ^[0-9]+$ ]]
((duration <= 600 && interval >= 1))
mkdir -p "$PHASE26_OUTPUT_ROOT/metrics"
end=$((SECONDS + duration))
while ((SECONDS < end)); do
	now=$(date --iso-8601=seconds)
	awk -v now="$now" '/^(MemTotal|MemAvailable|SwapTotal|SwapFree):/ {printf "%s,%s,%s\n",now,$1,$2}' \
		/proc/meminfo >>"$PHASE26_OUTPUT_ROOT/metrics/meminfo.csv"
	awk -v now="$now" '/^(SwapTotal|SwapFree):/ {printf "%s,%s,%s\n",now,$1,$2}' \
		/proc/meminfo >>"$PHASE26_OUTPUT_ROOT/metrics/swap.csv"
	awk -v now="$now" '{print now "," $0}' /proc/pressure/memory \
		>>"$PHASE26_OUTPUT_ROOT/metrics/memory_psi.csv"
	awk -v now="$now" '{print now "," $0}' /proc/loadavg \
		>>"$PHASE26_OUTPUT_ROOT/metrics/load.csv"
	awk -v now="$now" '/^(pgscan|pgsteal|pswp|oom_kill)/ {print now "," $1 "," $2}' \
		/proc/vmstat >>"$PHASE26_OUTPUT_ROOT/metrics/vmstat.csv"
	if ((PHASE26_DRY_RUN)); then break; fi
	sleep "$interval"
done
phase26_finish
