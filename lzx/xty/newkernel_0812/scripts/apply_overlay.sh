#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo "Usage: $0 <linux-hwe-6.17-parp-source-root>" >&2
  echo "This applies source/overlay and source/delete_paths.txt to turn the initial v4-parp HWE tree into linux-6.17.13-parp." >&2
}

if [[ $# -ne 1 ]]; then
  usage
  exit 2
fi

BUNDLE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC_ROOT="$(realpath "$1")"
OVERLAY="$BUNDLE_DIR/source/overlay"
DELETE_LIST="$BUNDLE_DIR/source/delete_paths.txt"

if [[ ! -f "$SRC_ROOT/Makefile" || ! -d "$SRC_ROOT/mm" || ! -d "$SRC_ROOT/include" ]]; then
  echo "error: $SRC_ROOT does not look like a Linux source tree" >&2
  exit 1
fi

case "$SRC_ROOT" in
  /|/home|/home/*|/usr|/usr/*|/tmp)
    echo "error: refusing suspicious source root: $SRC_ROOT" >&2
    exit 1
    ;;
esac

if [[ ! -f "$SRC_ROOT/mm/tier2_watermark.c" && ! -f "$SRC_ROOT/include/linux/tier2_watermark.h" ]]; then
  echo "warning: old HWE tier2_watermark files were not found; continuing because the tree may already be partially updated" >&2
fi

while IFS= read -r rel || [[ -n "$rel" ]]; do
  [[ -z "$rel" || "$rel" == \#* ]] && continue
  target="$SRC_ROOT/$rel"
  real_parent="$(realpath -m "$(dirname "$target")")"
  case "$real_parent" in
    "$SRC_ROOT"|"$SRC_ROOT"/*) ;;
    *) echo "error: refusing delete outside source root: $rel" >&2; exit 1 ;;
  esac
  if [[ -e "$target" || -L "$target" ]]; then
    rm -rf -- "$target"
  fi
done < "$DELETE_LIST"

if command -v rsync >/dev/null 2>&1; then
  rsync -a "$OVERLAY/" "$SRC_ROOT/"
else
  (cd "$OVERLAY" && tar -cf - .) | (cd "$SRC_ROOT" && tar -xf -)
fi

echo "Applied overlay to $SRC_ROOT"
echo "Next: use scripts/build_install_verify.sh or build manually with configs/current.config"
