#!/usr/bin/env bash
set -euo pipefail

output="${1:?usage: guest_capture_wps.sh OUTPUT.xwd [root]}"
mode="${2:-window}"
session_pid="$(pgrep -u "$(id -u)" -n gnome-shell)"

while IFS= read -r -d '' item; do
    case "$item" in
        DISPLAY=*|XAUTHORITY=*|XDG_RUNTIME_DIR=*|DBUS_SESSION_BUS_ADDRESS=*)
            export "$item"
            ;;
    esac
done < "/proc/$session_pid/environ"

if [[ "$mode" == "root" ]]; then
    xwd -silent -root -out "$output"
    exit 0
fi

window_id=""
smallest_content_area=2147483647
while IFS= read -r candidate; do
    WIDTH=0
    HEIGHT=0
    eval "$(xdotool getwindowgeometry --shell "$candidate" 2>/dev/null || true)"
    area=$((WIDTH * HEIGHT))
    if ((area >= 100000 && area <= smallest_content_area)); then
        smallest_content_area=$area
        window_id="$candidate"
    fi
done < <(xdotool search --onlyvisible --class 'wps|wpsoffice|wpp|et|wpspdf')

if [[ -z "$window_id" ]]; then
    echo "No visible WPS window found" >&2
    exit 1
fi
xwd -silent -id "$window_id" -out "$output"
