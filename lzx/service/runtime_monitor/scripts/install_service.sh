#!/usr/bin/env bash
set -euo pipefail

source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/layout.sh"
unit="$PARP_RUNTIME_ROOT/systemd/parp-runtime-monitor.service"
process_unit="$PARP_RUNTIME_ROOT/systemd/parp-process-events@.service"
process_helper="$PARP_RUNTIME_ROOT/helpers/proc_connector_helper.py"
udev_rule="$PARP_RUNTIME_ROOT/udev/70-parp-myfs.rules"
service_uid="$(id -u)"

# lzx-note: A lingering user manager starts the observe-only service at boot.
sudo -n loginctl enable-linger "$(id -un)"
sudo -n install -m 0644 "$udev_rule" /etc/udev/rules.d/70-parp-myfs.rules
sudo -n udevadm control --reload-rules
# lzx-note: Do not require a misc subsystem match: an early built-in myfs can
# initially appear as /sys/devices/myfs while keeping the same unique sysname.
sudo -n udevadm trigger --action=add --sysname-match=myfs || true
# The main monitor remains an unprivileged user service.  Install a narrowly
# sandboxed root helper for the one operation that requires CAP_NET_ADMIN:
# subscribing to the kernel's system-wide process connector.
sudo -n install -D -m 0755 "$process_helper" /usr/local/libexec/parp-proc-connector
sudo -n install -m 0644 "$process_unit" /etc/systemd/system/parp-process-events@.service
sudo -n systemctl daemon-reload
sudo -n systemctl enable "parp-process-events@${service_uid}.service"
sudo -n systemctl restart "parp-process-events@${service_uid}.service"
bash "$PARP_RUNTIME_ROOT/gnome_extension/install.sh" # lzx-note
systemctl --user link --force "$unit"
systemctl --user daemon-reload
systemctl --user enable parp-runtime-monitor.service
systemctl --user restart parp-runtime-monitor.service # lzx-note: apply updated daemon arguments now.
systemctl --user --no-pager --full status parp-runtime-monitor.service
