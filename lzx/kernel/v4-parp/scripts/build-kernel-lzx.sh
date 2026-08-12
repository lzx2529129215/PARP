#!/bin/sh
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
project_root=$(CDPATH= cd -- "$script_dir/.." && pwd)

source_tree="$project_root/src/linux-6.17.13-parp-lzx"
build_tree="$project_root/build/effective-tier-live-shadow-r9-lzx"

[ -f "$source_tree/Makefile" ] || {
	printf 'error: kernel source is missing: %s\n' "$source_tree" >&2
	exit 1
}
[ -f "$build_tree/.config" ] || {
	printf 'error: incremental build configuration is missing: %s\n' "$build_tree" >&2
	exit 1
}
if [ "$#" -eq 0 ]; then
	set -- -j"${PARP_JOBS:-$(nproc)}" bzImage
fi

exec make -C "$source_tree" O="$build_tree" "$@"
