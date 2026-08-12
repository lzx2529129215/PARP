#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0
set -euo pipefail

script_dir=$(cd "$(dirname "$0")" && pwd)
source_tree=$(cd "$script_dir/../.." && pwd)
working_config="/boot/config-$(uname -r)"
build_dir=""
index=1
configure_only=0

while (($#)); do
	case "$1" in
	--working-config) working_config=$2; shift 2 ;;
	--build-dir) build_dir=$2; shift 2 ;;
	--index) index=$2; shift 2 ;;
	--configure-only) configure_only=1; shift ;;
	*) echo "unknown argument: $1" >&2; exit 2 ;;
	esac
done
[[ -n $build_dir && $index =~ ^[1-9][0-9]*$ ]]
[[ -r $working_config ]]
mkdir -p "$build_dir"

root_disk=$(findmnt -no SOURCE / | sed 's/[0-9]*$//')
root_name=$(basename "$root_disk")
device_path=$(readlink -f "/sys/class/block/$root_name/device")
host=$(sed -n 's#.*\(/host[0-9]*\)/.*#\1#p' <<<"$device_path" | tr -d /)
root_driver=$(<"/sys/class/scsi_host/$host/proc_name")
[[ $root_driver = mptspi ]] || {
	echo "unsupported discovered root controller driver: $root_driver" >&2
	exit 1
}

if [[ ! -f $build_dir/.config ]]; then
	cp "$working_config" "$build_dir/.config"
	make -C "$source_tree" O="$build_dir" LOCALVERSION= olddefconfig
	"$source_tree/scripts/config" --file "$build_dir/.config" \
		--set-str SYSTEM_TRUSTED_KEYS "" \
		--set-str SYSTEM_REVOCATION_KEYS "" \
		--set-str SYSTEM_BLACKLIST_HASH_LIST "" \
		-d LOCALVERSION_AUTO \
		--set-str LOCALVERSION "-parp-v4-phase26-bootfix${index}-observe" \
		-e BLK_DEV_INITRD -e DEVTMPFS -e DEVTMPFS_MOUNT \
		-e PROC_FS -e SYSFS -e TMPFS -e PCI -e PCIEPORTBUS \
		-e SCSI -e BLK_DEV_SD -e SCSI_SPI_ATTRS \
		-e FUSION -e FUSION_SPI -e EXT4_FS -e JBD2 -e CRC16 \
		-e MSDOS_PARTITION -e EFI_PARTITION \
		-e PARP -e MEMCG -e CGROUPS -e LRU_GEN -e LRU_GEN_ENABLED \
		-e DAMON -e DAMON_VADDR -e DAMON_SYSFS \
		-e PARP_DAMON_ALIGNMENT -e DEBUG_FS -e TRACING \
		-e TRACEPOINTS -e FTRACE -e SWAP
	make -C "$source_tree" O="$build_dir" LOCALVERSION= olddefconfig
fi

required=(BLK_DEV_INITRD DEVTMPFS DEVTMPFS_MOUNT PROC_FS SYSFS TMPFS
	PCI PCIEPORTBUS SCSI BLK_DEV_SD SCSI_SPI_ATTRS FUSION FUSION_SPI
	EXT4_FS JBD2 CRC16 MSDOS_PARTITION PARP MEMCG CGROUPS LRU_GEN DAMON
	DAMON_VADDR DAMON_SYSFS DEBUG_FS TRACING TRACEPOINTS FTRACE SWAP)
for symbol in "${required[@]}"; do
	grep -q "^CONFIG_${symbol}=y$" "$build_dir/.config" || {
		echo "required built-in/config option missing: CONFIG_$symbol" >&2
		exit 1
	}
done
! grep -Eq '^CONFIG_SYSTEM_(TRUSTED|REVOCATION)_KEYS="[^\"]+' \
	"$build_dir/.config"
grep -q 'static enum parp_scan_budget_mode scan_budget_mode =' \
	"$source_tree/mm/parp/core/scan_budget.c"
grep -q 'PARP_SCAN_BUDGET_OBSERVE' "$source_tree/mm/parp/core/scan_budget.c"
grep -q 'static enum parp_mode parp_mode = PARP_MODE_OBSERVE' \
	"$source_tree/mm/parp/core/domain.c"
grep -q 'static enum parp_evidence_mode parp_evidence_mode = PARP_EVIDENCE_ONLY' \
	"$source_tree/mm/parp/core/domain.c"

make -C "$source_tree" O="$build_dir" LOCALVERSION= prepare
release=$(make -s -C "$source_tree" O="$build_dir" LOCALVERSION= kernelrelease)
[[ $release = "6.17.13-parp-v4-phase26-bootfix${index}-observe" ]]
printf 'root_driver=%s\nkernelrelease=%s\n' "$root_driver" "$release"
((configure_only)) && exit 0

jobs=$(nproc)
((jobs > 4)) && jobs=4
make -C "$source_tree" O="$build_dir" LOCALVERSION= -j"$jobs" bzImage modules
