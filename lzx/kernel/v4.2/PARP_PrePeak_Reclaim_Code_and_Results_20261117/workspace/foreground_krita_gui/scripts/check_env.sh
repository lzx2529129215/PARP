#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "$0")/.."
exec "${KRITA_GUI_PYTHON:-python3}" -m oomlab_krita_gui.cli check-env "$@"
