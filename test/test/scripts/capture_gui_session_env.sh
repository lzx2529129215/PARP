#!/usr/bin/env bash
set -euo pipefail

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
output="${1:-$root_dir/.env.vm.local}"

if [[ -z "${DISPLAY:-}" ]]; then
  echo "DISPLAY is empty. Run this script from a terminal opened inside the Linux desktop session." >&2
  exit 2
fi

if ! command -v wmctrl >/dev/null 2>&1; then
  echo "wmctrl is missing; install it before capturing the GUI session." >&2
  exit 3
fi

if ! wmctrl -m >/dev/null 2>&1; then
  echo "wmctrl cannot access DISPLAY=$DISPLAY. Log in using an Xorg session and retry." >&2
  exit 4
fi

runtime_dir="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
bus_address="${DBUS_SESSION_BUS_ADDRESS:-unix:path=$runtime_dir/bus}"
xauthority="${XAUTHORITY:-}"
if [[ -z "$xauthority" && -f "$HOME/.Xauthority" ]]; then
  xauthority="$HOME/.Xauthority"
fi

umask 077
{
  printf 'DISPLAY=%q\n' "$DISPLAY"
  printf 'XAUTHORITY=%q\n' "$xauthority"
  printf 'XDG_RUNTIME_DIR=%q\n' "$runtime_dir"
  printf 'DBUS_SESSION_BUS_ADDRESS=%q\n' "$bus_address"
  printf 'XDG_SESSION_TYPE=%q\n' "${XDG_SESSION_TYPE:-x11}"
} >"$output"

echo "Captured GUI session environment: $output"
echo "Verify from VS Code Remote SSH with:"
echo "  bash scripts/with_gui_session.sh wmctrl -m"
