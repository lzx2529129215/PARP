#!/usr/bin/env bash
set -euo pipefail

name=""
root="${APP_WSS_CGROUP_ROOT:-/sys/fs/cgroup/app-wss}"
while (($#)); do
    case "$1" in
        --name) name="${2:?missing --name value}"; shift 2 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done
if [[ ! "$name" =~ ^[A-Za-z0-9_.-]+$ ]]; then
    echo "--name must contain only letters, digits, dot, underscore or dash" >&2
    exit 2
fi
if [[ "$(stat -fc %T /sys/fs/cgroup)" != "cgroup2fs" ]]; then
    echo "cgroup v2 is required" >&2
    exit 1
fi
case "$root" in
    /sys/fs/cgroup/*) ;;
    *) echo "invalid cgroup root: $root" >&2; exit 2 ;;
esac

sudo -n mkdir -p "$root"
printf '%s\n' '+memory +pids' | sudo -n tee "$root/cgroup.subtree_control" >/dev/null
target="$root/$name"
sudo -n mkdir "$target"
sudo -n chown "$(id -u):$(id -g)" "$root" "$target" "$target/cgroup.procs"

printf 'cgroup=%s\n' "$target"
printf 'controllers=%s\n' "$(cat "$root/cgroup.subtree_control")"
