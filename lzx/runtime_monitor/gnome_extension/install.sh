#!/usr/bin/env bash
set -euo pipefail

UUID="runtime-app-monitor@huawei.local"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DST_DIR="${HOME}/.local/share/gnome-shell/extensions/${UUID}"

mkdir -p "${DST_DIR}"
cp "${SRC_DIR}/metadata.json" "${SRC_DIR}/extension.js" "${DST_DIR}/"

echo "installed: ${DST_DIR}"
echo ""
echo "Next steps:"
echo "  1) Log out of the GNOME Wayland desktop and log back in."
echo "  2) Check that GNOME sees the extension:"
echo "       gnome-extensions list | grep ${UUID}"
echo "  3) Enable it:"
echo "       gnome-extensions enable ${UUID}"
echo ""
echo "If step 3 says the extension does not exist, GNOME Shell has not refreshed"
echo "the user extension directory yet, or this script was run as a different user."
