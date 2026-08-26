#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
evidence_dir="${1:-${PROJECT_ROOT}/outputs/runtime_monitor}"

if command -v convert >/dev/null 2>&1; then
  for source in "$evidence_dir"/0050_*.xwd; do
    [ -e "$source" ] || continue
    convert "$source" "${source%.xwd}.png"
  done
elif command -v xwdtopnm >/dev/null 2>&1 && command -v pnmtopng >/dev/null 2>&1; then
  for source in "$evidence_dir"/0050_*.xwd; do
    [ -e "$source" ] || continue
    xwdtopnm "$source" | pnmtopng > "${source%.xwd}.png"
  done
elif command -v ffmpeg >/dev/null 2>&1; then
  for source in "$evidence_dir"/0050_*.xwd; do
    [ -e "$source" ] || continue
    ffmpeg -loglevel error -y -i "$source" "${source%.xwd}.png"
  done
else
  printf '%s\n' 'No XWD-to-PNG converter is installed.' >&2
  exit 127
fi
