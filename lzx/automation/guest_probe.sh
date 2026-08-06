#!/usr/bin/env bash
set -u

output="${1:-/tmp/automation-wps-guest-probe.txt}"

{
    echo '=== identity ==='
    id
    getent passwd "$(id -un)"

    echo '=== sessions ==='
    who || true
    loginctl list-sessions --no-legend 2>/dev/null || true

    echo '=== desktop env from gnome-shell ==='
    pid="$(pgrep -u "$(id -u)" -n gnome-shell || true)"
    if [[ -n "$pid" ]]; then
        tr '\0' '\n' < "/proc/$pid/environ" \
            | grep -E '^(DISPLAY|WAYLAND_DISPLAY|XAUTHORITY|XDG_RUNTIME_DIR|DBUS_SESSION_BUS_ADDRESS|XDG_SESSION_TYPE|HOME|USER)=' \
            || true
    else
        echo 'gnome-shell not found'
    fi

    echo '=== commands ==='
    for cmd in python3 wps wpp et wpspdf xdotool wmctrl xclip xprop; do
        printf '%-10s ' "$cmd"
        command -v "$cmd" || echo MISSING
    done

    echo '=== WPS processes ==='
    pgrep -a -u "$(id -u)" 'wps|wpsoffice|wpp|et|wpspdf' || true
} > "$output" 2>&1
