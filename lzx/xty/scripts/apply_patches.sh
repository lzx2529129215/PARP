#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR=${1:-$(pwd)}
BUNDLE_DIR=$(cd "$(dirname "$0")/.." && pwd)

cd "$ROOT_DIR"

while IFS= read -r patch; do
  [ -z "$patch" ] && continue
  echo "Applying $patch"
  patch -p1 --forward < "$BUNDLE_DIR/patches/$patch"
done < "$BUNDLE_DIR/patches/series.txt"
