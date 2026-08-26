#!/usr/bin/env bash
set -euo pipefail

source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/layout.sh"
unit="$PARP_RUNTIME_ROOT/systemd/parp-runtime-monitor.service"

# lzx-note: A lingering user manager starts the observe-only service at boot.
sudo -n loginctl enable-linger "$(id -un)"
systemctl --user link --force "$unit"
systemctl --user daemon-reload
systemctl --user enable --now parp-runtime-monitor.service
systemctl --user --no-pager --full status parp-runtime-monitor.service
