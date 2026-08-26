#!/usr/bin/env bash
set -euo pipefail

session_pid="$(pgrep -u "$(id -u)" -n gnome-shell)"
while IFS= read -r -d '' item; do
    case "$item" in
        DISPLAY=*|XAUTHORITY=*|XDG_RUNTIME_DIR=*|DBUS_SESSION_BUS_ADDRESS=*)
            export "$item"
            ;;
    esac
done < "/proc/$session_pid/environ"

session_id="$(loginctl list-sessions --no-legend | awk -v uid="$(id -u)" '$2 == uid { print $1; exit }')"
if [[ -n "$session_id" ]]; then
    loginctl unlock-session "$session_id" || true
fi

gsettings set org.gnome.desktop.session idle-delay 'uint32 0'
gsettings set org.gnome.desktop.screensaver lock-enabled false

printf 'DISPLAY=%s\n' "${DISPLAY:-}"
printf 'XAUTHORITY=%s\n' "${XAUTHORITY:-}"
printf 'idle-delay=%s\n' "$(gsettings get org.gnome.desktop.session idle-delay)"
printf 'lock-enabled=%s\n' "$(gsettings get org.gnome.desktop.screensaver lock-enabled)"
