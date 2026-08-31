#!/usr/bin/env bash
set -euo pipefail

source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/layout.sh"
unit="$PARP_RUNTIME_ROOT/systemd/parp-runtime-monitor.service"
process_unit="$PARP_RUNTIME_ROOT/systemd/parp-process-events@.service"
process_helper="$PARP_RUNTIME_ROOT/helpers/proc_connector_helper.py"
file_unit="$PARP_RUNTIME_ROOT/systemd/parp-file-events@.service"
file_helper="$PARP_RUNTIME_ROOT/helpers/ebpf_file_event_helper.py"
file_bpf="$PARP_RUNTIME_ROOT/ebpf/file_events.bpf.c"
udev_rule="$PARP_RUNTIME_ROOT/udev/70-parp-myfs.rules"
service_uid="$(id -u)"

if ! python3 -c 'from bcc import BPF' >/dev/null 2>&1; then
  echo "python3-bpfcc is required for precise eBPF file events" >&2
  exit 1
fi

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
sudo -n install -D -m 0755 "$file_helper" /usr/local/libexec/parp-ebpf-file-events
sudo -n install -m 0644 "$file_bpf" /usr/local/libexec/parp-file-events.bpf.c
sudo -n install -m 0644 "$file_unit" /etc/systemd/system/parp-file-events@.service

# BCC 需要当前自定义内核同时暴露 build 与 source。该内核的 build 链接已由
# 安装流程提供，但 source 链接可能缺失；只在能唯一找到本项目对应源码时补齐。
kernel_release="$(uname -r)"
kernel_module_dir="/lib/modules/$kernel_release"
kernel_build="$(readlink -f "$kernel_module_dir/build" 2>/dev/null || true)"
kernel_source="$(readlink -f "$kernel_build/source" 2>/dev/null || true)"
if [[ ! -e "$kernel_module_dir/source" && -f "$kernel_source/include/linux/kconfig.h" ]]; then
  sudo -n ln -s "$kernel_source" "$kernel_module_dir/source"
fi
sudo -n systemctl daemon-reload
sudo -n systemctl enable "parp-process-events@${service_uid}.service"
sudo -n systemctl enable "parp-file-events@${service_uid}.service"
sudo -n systemctl restart "parp-process-events@${service_uid}.service"
sudo -n systemctl restart "parp-file-events@${service_uid}.service"
bash "$PARP_RUNTIME_ROOT/gnome_extension/install.sh" # lzx-note
systemctl --user link --force "$unit"
systemctl --user daemon-reload
systemctl --user enable parp-runtime-monitor.service
systemctl --user restart parp-runtime-monitor.service # lzx-note: apply updated daemon arguments now.
systemctl --user --no-pager --full status parp-runtime-monitor.service
