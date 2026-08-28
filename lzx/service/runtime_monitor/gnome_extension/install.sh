#!/usr/bin/env bash
set -euo pipefail

UUID="runtime-app-monitor@huawei.local"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DST_DIR="${HOME}/.local/share/gnome-shell/extensions/${UUID}"

install -d -m 0755 "${DST_DIR}"
install -m 0644 "${SRC_DIR}/metadata.json" "${SRC_DIR}/extension.js" "${DST_DIR}/"

echo "installed: ${DST_DIR}"
echo ""
if gnome-extensions enable "${UUID}" 2>/dev/null; then
  echo "enabled: ${UUID}"
else
  echo "The current GNOME Shell has not reloaded the new extension yet."
  echo "It will be enabled after the next logout/login or reboot."
  enabled_extensions="$(gsettings get org.gnome.shell enabled-extensions)"
  if [[ "$enabled_extensions" != *"'$UUID'"* ]]; then
    if [[ "$enabled_extensions" == "@as []" || "$enabled_extensions" == "[]" ]]; then
      enabled_extensions="['$UUID']"
    else
      enabled_extensions="${enabled_extensions%]}"
      enabled_extensions="$enabled_extensions, '$UUID']"
    fi
    gsettings set org.gnome.shell enabled-extensions "$enabled_extensions"
    echo "scheduled for next GNOME login: ${UUID}" # lzx-note
  fi
fi

echo "Verification:"
echo "  1) Check that GNOME sees the extension:"
echo "       gnome-extensions list | grep ${UUID}"
echo "  2) Enable it if needed:"
echo "       gnome-extensions enable ${UUID}"
