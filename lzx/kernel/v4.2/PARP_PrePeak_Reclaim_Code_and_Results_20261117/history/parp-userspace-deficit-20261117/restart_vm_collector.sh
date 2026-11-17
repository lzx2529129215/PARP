#!/usr/bin/env bash
# Restart only the scoped collector in a second local Ubuntu terminal.
set -eu
if [ "$(id -u)" != 1000 ]; then
    echo 'Run as the ordinary desktop user qi, not sudo.' >&2
    exit 1
fi
experiment_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$experiment_dir"
while true; do
    python3 -B prepare_guest.py
    sudo python3 -u -B /home/qi/parp-userspace-totalfault-20261116/root_guard.py --minutes 120
    echo 'Collector restored its original settings. Waiting for the next cohort request.'
    while [ ! -f results/collector-restart.request ] && [ ! -f results/collector-session.stop ]; do
        sleep 1
    done
    if [ -f results/collector-session.stop ]; then
        echo 'All cohorts finished; collector launcher stopped.'
        break
    fi
    python3 -c 'from pathlib import Path; Path("results/collector-restart.request").unlink()'
done
