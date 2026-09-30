#!/usr/bin/env bash
set -euo pipefail

project="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
name=""
run_id="$(date +%Y%m%d-%H%M%S)"
while (($#)); do
    case "$1" in
        --name) name="${2:?missing --name value}"; shift 2 ;;
        --run-id) run_id="${2:?missing --run-id value}"; shift 2 ;;
        --) shift; break ;;
        *) echo "unknown argument before --: $1" >&2; exit 2 ;;
    esac
done
if [[ ! "$name" =~ ^[A-Za-z0-9_.-]+$ ]] || [[ ! "$run_id" =~ ^[A-Za-z0-9_.-]+$ ]]; then
    echo "invalid --name or --run-id" >&2
    exit 2
fi
if (($# == 0)); then
    echo "usage: $0 --name NAME [--run-id ID] -- COMMAND [ARG ...]" >&2
    exit 2
fi

target_name="${name}-${run_id}"
setup_output="$(bash "$project/scripts/setup_app_cgroup.sh" --name "$target_name")"
cgroup="$(printf '%s\n' "$setup_output" | sed -n 's/^cgroup=//p')"
test -n "$cgroup"
log_dir="${APP_WSS_LOG_DIR:-$project/runs/logs}"
mkdir -p "$log_dir"
stdout_log="$log_dir/app-wss-${target_name}.stdout.log"
stderr_log="$log_dir/app-wss-${target_name}.stderr.log"
(
    shell_pid="$BASHPID"
    sudo -n tee "$cgroup/cgroup.procs" >/dev/null <<<"$shell_pid"
    exec "$@"
) >"$stdout_log" 2>"$stderr_log" &
launcher_pid=$!

for _ in {1..100}; do
    if [[ -r "/proc/$launcher_pid/cgroup" ]] &&
       grep -Fq "0::${cgroup#/sys/fs/cgroup}" "/proc/$launcher_pid/cgroup"; then
        printf 'launcher_pid=%s\ncgroup=%s\nstdout_log=%s\nstderr_log=%s\n' \
            "$launcher_pid" "$cgroup" "$stdout_log" "$stderr_log"
        exit 0
    fi
    if ! kill -0 "$launcher_pid" 2>/dev/null; then
        echo "application exited before target cgroup was verified" >&2
        cat "$stderr_log" >&2 || true
        exit 1
    fi
    sleep 0.05
done
echo "application did not enter target cgroup: pid=$launcher_pid cgroup=$cgroup" >&2
exit 1
