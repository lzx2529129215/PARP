#!/usr/bin/env bash
set -euo pipefail

SRC_ROOT="${1:-$PWD}"
BUILD_DIR="${2:-$(dirname "$SRC_ROOT")/build-linux-6.17.13-parp}"
BUNDLE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
JOBS="${JOBS:-$(nproc)}"

SRC_ROOT="$(realpath "$SRC_ROOT")"
mkdir -p "$BUILD_DIR"
cp "$BUNDLE_DIR/configs/current.config" "$BUILD_DIR/.config"
if [[ ! -x "$SRC_ROOT/scripts/config" ]]; then
  echo "error: missing $SRC_ROOT/scripts/config" >&2
  exit 1
fi
"$SRC_ROOT/scripts/config" --file "$BUILD_DIR/.config" \
  -e PARP \
  -e PARP_TIER2_WATERMARK \
  -e PARP_RECLAIM_BIN_SCORE
if [[ "${PARP_EXPERIMENT_APPLY:-0}" == "1" ]]; then
  "$SRC_ROOT/scripts/config" --file "$BUILD_DIR/.config" \
    --enable PARP_EFFECTIVE_TIER_EXPERIMENTAL_APPLY \
    --set-str LOCALVERSION "-myks-l03-apply"
  echo "Experimental effective-tier Apply is compiled; runtime mode still defaults to OFF."
fi
make -C "$SRC_ROOT" O="$BUILD_DIR" olddefconfig
grep -E '^CONFIG_(LOCALVERSION|PARP_TIER2_WATERMARK|PARP_RECLAIM_BIN_SCORE)=' "$BUILD_DIR/.config"
make -C "$SRC_ROOT" O="$BUILD_DIR" -j"$JOBS" bzImage modules

echo "Build complete. To install on a test VM, run:"
echo "  sudo make -C '$SRC_ROOT' O='$BUILD_DIR' modules_install install"
echo "Then reboot into the new kernel and run scripts/verify_runtime.sh"
