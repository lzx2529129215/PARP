#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="${1:-$(cd -- "$SCRIPT_DIR/.." && pwd)}"

session_pid="$(pgrep -u "$(id -u)" -n gnome-shell || true)"
if [[ -n "$session_pid" ]]; then
    while IFS= read -r -d '' item; do
        case "$item" in
            DISPLAY=*|XAUTHORITY=*|XDG_RUNTIME_DIR=*|DBUS_SESSION_BUS_ADDRESS=*)
                export "$item"
                ;;
        esac
    done < "/proc/$session_pid/environ"
fi

for candidate in \
    "$(command -v pycharm-community 2>/dev/null || true)" \
    "$(command -v pycharm 2>/dev/null || true)" \
    /snap/pycharm-community/current/bin/pycharm.sh \
    /snap/pycharm-professional/current/bin/pycharm.sh; do
    if [[ -n "$candidate" && -x "$candidate" ]]; then
        exec "$candidate" "$PROJECT_ROOT"
    fi
done

echo "没有找到 PyCharm。请先安装：sudo snap install pycharm-community --classic" >&2
exit 1
