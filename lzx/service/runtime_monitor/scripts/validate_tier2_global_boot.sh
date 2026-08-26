#!/usr/bin/env bash
set -u -o pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
EXPECTED_RELEASE="6.17.13-mglru-tier2"
TS="$(date +%Y%m%d_%H%M%S)"
OUT="$PROJECT_ROOT/outputs/mglru/tier2_global_boot_validation_${TS}"
SUMMARY="$OUT/tier2_global_boot_summary.md"
mkdir -p "$OUT"

pass=true
record_check() {
	local name="$1" result="$2" detail="$3"
	printf '%s,%s,%s\n' "$name" "$result" "${detail//,/;}" >> "$OUT/checks.csv"
	[[ "$result" == "PASS" ]] || pass=false
}

printf 'check,result,detail\n' > "$OUT/checks.csv"
uname -a > "$OUT/uname.txt"
cat /proc/version > "$OUT/proc_version.txt"
cat /proc/cmdline > "$OUT/proc_cmdline.txt"
sudo -n dmesg > "$OUT/dmesg.txt"
journalctl -b -k --no-pager > "$OUT/journalctl_boot_kernel.txt"
sudo -n find /sys/fs/pstore -maxdepth 1 -type f -print -exec cat {} \; \
	> "$OUT/pstore.txt" 2>&1 || true

release="$(uname -r)"
record_check kernel_release "$([[ "$release" == "$EXPECTED_RELEASE" ]] && echo PASS || echo FAIL)" "$release"

config="/boot/config-$release"
global_config=false
memcg_config=false
[[ -f "$config" ]] && grep -qx 'CONFIG_TIER2_WATERMARK=y' "$config" && global_config=true
[[ -f "$config" ]] && grep -qx 'CONFIG_TIER2_WATERMARK_MEMCG=y' "$config" && memcg_config=true
record_check global_config "$($global_config && echo PASS || echo FAIL)" "$config"
record_check memcg_config_disabled "$(! $memcg_config && echo PASS || echo FAIL)" "$config"

controllers="$(cat /sys/fs/cgroup/cgroup.controllers 2>/dev/null || true)"
record_check memory_controller "$([[ " $controllers " == *' memory '* ]] && echo PASS || echo FAIL)" "$controllers"
record_check user_memory_files "$([[ -r /sys/fs/cgroup/user.slice/memory.current && -r /sys/fs/cgroup/user.slice/memory.stat && -r /sys/fs/cgroup/user.slice/memory.events ]] && echo PASS || echo FAIL)" "/sys/fs/cgroup/user.slice"

test_cgroup="/sys/fs/cgroup/tier2-global-validation-${TS}.scope"
cgroup_result=FAIL
if sudo -n mkdir "$test_cgroup" 2> "$OUT/cgroup_create_error.txt"; then
	if [[ -r "$test_cgroup/memory.current" ]]; then
		cgroup_result=PASS
	fi
	sudo -n rmdir "$test_cgroup" 2> "$OUT/cgroup_remove_error.txt" || cgroup_result=FAIL
fi
record_check empty_cgroup_create_delete "$cgroup_result" "$test_cgroup"

mapfile -t memcg_files < <(find /sys/fs/cgroup -maxdepth 4 -name 'memory.tier2_*' -print 2>/dev/null)
printf '%s\n' "${memcg_files[@]}" > "$OUT/memory_tier2_files.txt"
record_check no_memcg_tier2_files "$([[ ${#memcg_files[@]} -eq 0 ]] && echo PASS || echo FAIL)" "count=${#memcg_files[@]}"

sysctl_path=/proc/sys/vm/tier2_wmark_enabled
debugfs_dir=/sys/kernel/debug/tier2_watermark
record_check global_sysctl "$([[ -r "$sysctl_path" ]] && echo PASS || echo FAIL)" "$sysctl_path"
enabled="$(cat "$sysctl_path" 2>/dev/null || echo missing)"
record_check global_default_disabled "$([[ "$enabled" == 0 ]] && echo PASS || echo FAIL)" "enabled=$enabled"
record_check global_debugfs "$(sudo -n test -r "$debugfs_dir/state" && sudo -n test -r "$debugfs_dir/stats" && echo PASS || echo FAIL)" "$debugfs_dir"
sudo -n cat "$debugfs_dir/state" > "$OUT/tier2_global_state.txt" 2>&1 || true
sudo -n cat "$debugfs_dir/stats" > "$OUT/tier2_global_stats.txt" 2>&1 || true

record_check mglru_markov_debugfs "$(sudo -n test -r /sys/kernel/debug/lru_gen_workload_markov && echo PASS || echo FAIL)" "/sys/kernel/debug/lru_gen_workload_markov"
record_check mglru_pages_debugfs "$(sudo -n test -r /sys/kernel/debug/lru_gen_pages && echo PASS || echo FAIL)" "/sys/kernel/debug/lru_gen_pages"

bad_re='BUG:|Oops:|Kernel panic|use-after-free|refcount.*(underflow|saturated)|lockdep.*WARNING|tier2.*(error|failed)'
if grep -Eiq "$bad_re" "$OUT/dmesg.txt" "$OUT/pstore.txt"; then
	record_check kernel_errors FAIL "matched critical kernel error pattern"
else
	record_check kernel_errors PASS "no critical kernel error pattern"
fi

final_result=FAIL
$pass && final_result=PASS
{
	echo '# Tier2 全局功能启动验证'
	echo
	echo "- timestamp: $TS"
	echo "- expected_release: \`$EXPECTED_RELEASE\`"
	echo "- running_release: \`$release\`"
	echo "- config: \`$config\`"
	echo "- global_enabled: $enabled"
	echo "- memory_tier2_files_count: ${#memcg_files[@]}"
	echo "- final_result: $final_result"
	echo
	echo '## 检查项'
	echo
	echo '| check | result | detail |'
	echo '|---|---|---|'
	tail -n +2 "$OUT/checks.csv" | while IFS=, read -r name result detail; do
		printf '| %s | %s | %s |\n' "$name" "$result" "$detail"
	done
} > "$SUMMARY"

echo "validation_dir=$OUT"
echo "summary=$SUMMARY"
echo "final_result=$final_result"
[[ "$final_result" == PASS ]]
