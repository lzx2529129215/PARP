#!/usr/bin/env bash
set -euo pipefail

echo "uname -r: $(uname -r)"
echo "--- PARP config ---"
if [[ -r /boot/config-$(uname -r) ]]; then
  grep -E '^CONFIG_PARP|^CONFIG_MEMCG|^CONFIG_LRU_GEN' "/boot/config-$(uname -r)" || true
else
  zcat /proc/config.gz 2>/dev/null | grep -E '^CONFIG_PARP|^CONFIG_MEMCG|^CONFIG_LRU_GEN' || true
fi

echo "--- tier2 sysctls ---"
for f in /proc/sys/vm/tier2_predict_enabled /proc/sys/vm/tier2_predict_latency_ms /proc/sys/vm/tier2_predict_horizon_ratio; do
  if [[ -r "$f" ]]; then echo "$f=$(cat "$f")"; else echo "missing: $f"; fi
done

echo "--- cgroup tier2 files ---"
find /sys/fs/cgroup -maxdepth 3 \( -name 'memory.tier2_enabled' -o -name 'memory.tier2_stats' \) 2>/dev/null | head -20 || true
echo "--- expected tier2 files in a child cgroup ---"
tmp_cgroup="/sys/fs/cgroup/parp-kcfg-verify-$$"
if mkdir "$tmp_cgroup" 2>/dev/null; then
  find "$tmp_cgroup" -maxdepth 1 -type f -name 'memory.tier2_*' -printf '%f\n' 2>/dev/null | sort || true
  rmdir "$tmp_cgroup" 2>/dev/null || true
else
  echo "cannot create temporary cgroup; run as root to inspect child-cgroup files"
fi

echo "--- dmesg marker ---"
dmesg | grep -E 'PARP tier2|tier2_watermark' | tail -20 || true
