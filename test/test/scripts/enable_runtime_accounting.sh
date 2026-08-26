#!/usr/bin/env bash
set -euo pipefail

uid="$(id -u)"
current_rel="$(awk -F: '$1 == "0" {print $3}' /proc/self/cgroup)"
current_path="/sys/fs/cgroup${current_rel}"

if [[ -z "$current_rel" || ! -d "$current_path" ]]; then
  echo "Current cgroup v2 path cannot be resolved" >&2
  exit 2
fi
if ! sudo -n true; then
  echo "sudo credentials are not cached; run sudo -v first" >&2
  exit 3
fi

# Runtime-only properties: these disappear after reboot and do not create
# persistent systemd drop-ins.
sudo -n systemctl set-property --runtime user.slice \
  MemoryAccounting=yes CPUAccounting=yes IOAccounting=yes
sudo -n systemctl set-property --runtime "user-${uid}.slice" \
  MemoryAccounting=yes CPUAccounting=yes IOAccounting=yes
sudo -n systemctl set-property --runtime "user@${uid}.service" \
  MemoryAccounting=yes CPUAccounting=yes IOAccounting=yes
systemctl --user set-property --runtime app.slice \
  MemoryAccounting=yes CPUAccounting=yes IOAccounting=yes
systemctl --user set-property --runtime session.slice \
  MemoryAccounting=yes CPUAccounting=yes IOAccounting=yes

missing=0
for file in memory.stat cpu.stat io.stat; do
  if [[ -r "$current_path/$file" ]]; then
    echo "OK current cgroup endpoint: $current_path/$file"
  else
    echo "FAIL current cgroup endpoint: $current_path/$file" >&2
    missing=1
  fi
done
exit "$missing"
