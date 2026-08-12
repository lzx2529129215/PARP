#!/usr/bin/env bash
set -euo pipefail

SRC_ROOT="${1:-$PWD}"
BUILD_DIR="${2:-$(dirname "$SRC_ROOT")/build-linux-6.17.13-parp}"
BUNDLE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
JOBS="${JOBS:-$(nproc)}"

SRC_ROOT="$(realpath "$SRC_ROOT")"
mkdir -p "$BUILD_DIR"
cp "$BUNDLE_DIR/configs/current.config" "$BUILD_DIR/.config"
make -C "$SRC_ROOT" O="$BUILD_DIR" olddefconfig
make -C "$SRC_ROOT" O="$BUILD_DIR" -j"$JOBS" bzImage modules

echo "Build complete. To install on a test VM, run:"
echo "  sudo make -C '$SRC_ROOT' O='$BUILD_DIR' modules_install install"
echo "Then reboot into the new kernel and run scripts/verify_runtime.sh"
