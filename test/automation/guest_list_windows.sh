#!/usr/bin/env bash
set -u

output="${1:?usage: guest_list_windows.sh OUTPUT.txt}"
session_pid="$(pgrep -u "$(id -u)" -n gnome-shell)"
while IFS= read -r -d '' item; do
    case "$item" in
        DISPLAY=*|XAUTHORITY=*|XDG_RUNTIME_DIR=*|DBUS_SESSION_BUS_ADDRESS=*) export "$item" ;;
    esac
done < "/proc/$session_pid/environ"

{
    echo "DISPLAY=${DISPLAY:-}"
    echo "XAUTHORITY=${XAUTHORITY:-}"
    echo "ACTIVE=$(xdotool getactivewindow 2>/dev/null || true)"
    while IFS= read -r window_id; do
        [[ -n "$window_id" ]] || continue
        echo "--- id=$window_id ---"
        xdotool getwindowname "$window_id" 2>/dev/null || true
        xdotool getwindowgeometry --shell "$window_id" 2>/dev/null || true
        xprop -id "$window_id" WM_CLASS _NET_WM_NAME _NET_WM_PID 2>/dev/null || true
    done < <(xdotool search --onlyvisible --name '.*' 2>/dev/null || true)
} > "$output" 2>&1
