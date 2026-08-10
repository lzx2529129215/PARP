#!/usr/bin/env bash
set -euo pipefail

echo "[uname]"
uname -a
echo

echo "[sysctl]"
cat /proc/sys/vm/tier2_predict_enabled
cat /proc/sys/vm/tier2_predict_latency_ms
cat /proc/sys/vm/tier2_predict_horizon_ratio
echo

echo "[tracefs]"
if [ ! -d /sys/kernel/tracing ]; then
  sudo mount -t tracefs nodev /sys/kernel/tracing || true
fi
grep '^parp:' /sys/kernel/tracing/available_events
echo

echo "[cgroup-v1]"
find /sys/fs/cgroup -name memory.tier2_enabled -o -name memory.tier2_headroom 2>/dev/null | sort
echo

echo "[dmesg]"
sudo dmesg | grep -E 'PARP tier2|vmwgfx|SRSO|TSA' | tail -20
