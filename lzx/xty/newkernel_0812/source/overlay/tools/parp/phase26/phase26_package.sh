#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0
set -euo pipefail
source "$(dirname "$0")/_common.sh"
phase26_init "$@"

build_dir=$(phase26_state_get build_dir)
release=$(phase26_state_get target_kernel_release)
test -n "$release"
mkdir -p "$PHASE26_OUTPUT_ROOT/packages"
if command -v fakeroot >/dev/null 2>&1 && command -v dpkg-buildpackage >/dev/null 2>&1; then
	if phase26_run make -C "$PHASE26_WORK_TREE" O="$build_dir" \
			-j"$(nproc)" LOCALVERSION= KDEB_PKGVERSION="1phase26" \
			bindeb-pkg; then
		find "$(dirname "$build_dir")" "$(dirname "$PHASE26_WORK_TREE")" \
			-maxdepth 1 -type f -name "*${release}*.*deb" \
			-exec cp -n {} "$PHASE26_OUTPUT_ROOT/packages/" \;
	else
		phase26_log "BINDEB_PKG_UNAVAILABLE; using staged-root fallback (see preceding logged error)"
	fi
fi
if ! compgen -G "$PHASE26_OUTPUT_ROOT/packages/*.deb" >/dev/null; then
	stage="$PHASE26_OUTPUT_ROOT/packages/staged-root"
	mkdir -p "$stage/boot" "$stage/lib/modules"
	phase26_run make -C "$PHASE26_WORK_TREE" O="$build_dir" \
		LOCALVERSION= INSTALL_MOD_PATH="$stage" modules_install
	cp -f "$build_dir/arch/x86/boot/bzImage" "$stage/boot/vmlinuz-$release"
	cp -f "$build_dir/System.map" "$stage/boot/System.map-$release"
	cp -f "$build_dir/.config" "$stage/boot/config-$release"
	phase26_log "FALLBACK staged modules and boot files"
fi
phase26_finish
