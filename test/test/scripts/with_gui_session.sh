#!/usr/bin/env bash
set -euo pipefail

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
env_file="${MEMSCHED_GUI_ENV_FILE:-$root_dir/.env.vm.local}"

if [[ ! -r "$env_file" ]]; then
  echo "GUI session environment not found: $env_file" >&2
  echo "Open a terminal inside the VM desktop and run scripts/capture_gui_session_env.sh first." >&2
  exit 2
fi

set -a
# shellcheck disable=SC1090
source "$env_file"
set +a

if [[ -d "$root_dir/.venv/bin" ]]; then
  export PATH="$root_dir/.venv/bin:$PATH"
fi

if [[ -z "${DISPLAY:-}" || -z "${XDG_RUNTIME_DIR:-}" || -z "${DBUS_SESSION_BUS_ADDRESS:-}" ]]; then
  echo "The GUI environment file is incomplete; capture it again from the VM desktop." >&2
  exit 3
fi

if ! wmctrl -m >/dev/null 2>&1; then
  echo "Cannot access the captured X11 display. The desktop session may have ended or changed." >&2
  exit 4
fi

if [[ $# -eq 0 ]]; then
  exec bash
fi
exec "$@"
