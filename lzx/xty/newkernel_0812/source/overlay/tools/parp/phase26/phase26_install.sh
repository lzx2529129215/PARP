#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0
set -euo pipefail
source "$(dirname "$0")/_common.sh"
phase26_init "$@"
phase26_require_snapshot_marker

release=$(phase26_state_get target_kernel_release)
build_dir=$(phase26_state_get build_dir)
test -n "$release"
test "$(sha256sum "$build_dir/arch/x86/boot/bzImage" | cut -d' ' -f1)" = \
	"$(phase26_state_get file_hashes.bzImage)"
test -e /boot/vmlinuz-5.15.0-136-generic
phase26_run df -h / /boot /boot/efi

if compgen -G "$PHASE26_OUTPUT_ROOT/packages/*.deb" >/dev/null; then
	mapfile -t packages < <(find "$PHASE26_OUTPUT_ROOT/packages" -maxdepth 1 \
		-type f -name '*.deb' -print | sort)
	phase26_run_root dpkg -i "${packages[@]}"
else
	stage="$PHASE26_OUTPUT_ROOT/packages/staged-root"
	test -d "$stage/lib/modules/$release"
	phase26_run_root cp -a "$stage/lib/modules/$release" /lib/modules/
	for name in vmlinuz System.map config; do
		phase26_run_root install -m 0644 "$stage/boot/$name-$release" "/boot/$name-$release"
	done
	phase26_run_root depmod -a "$release"
fi
phase26_run_root update-initramfs -c -k "$release"
phase26_run_root update-grub
test -e "/boot/vmlinuz-$release"
test -e "/boot/initrd.img-$release"
test -d "/lib/modules/$release"
test -e /boot/vmlinuz-5.15.0-136-generic
phase26_state_set install_status '"INSTALLED"'
phase26_state_set grub_status '"UPDATED_DEFAULT_UNCHANGED"'
phase26_state_set preboot_complete true
phase26_finish
