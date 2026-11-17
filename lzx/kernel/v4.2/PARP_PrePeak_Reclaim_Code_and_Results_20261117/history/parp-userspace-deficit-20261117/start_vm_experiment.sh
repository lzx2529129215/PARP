#!/usr/bin/env bash
# Run in the Ubuntu VM as qi. The sudo prompt stays in this local terminal.
set -eu
if [ "$(id -u)" != 1000 ]; then
    echo 'Please start this script as the ordinary desktop user qi, not sudo.' >&2
    exit 1
fi
experiment_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$experiment_dir"
mkdir -p results
python3 -B run_total.py --scope per_app --check
python3 -B prepare_guest.py
channel_log="$experiment_dir/results/guest-channel-$(date +%Y%m%d-%H%M%S).log"
python3 -u -B ../krita-vm-acceptance/guest_session.py --minutes 120 >"$channel_log" 2>&1 &
channel_pid=$!
sleep 1
if ! kill -0 "$channel_pid" 2>/dev/null; then
    cat "$channel_log"
    exit 1
fi
echo "Ordinary-user channel started; log: $channel_log"
echo 'The next sudo command starts only the local scoped compression/collector helper.'
sudo python3 -u -B "$HOME/$(basename "$experiment_dir")/root_guard.py" --minutes 120
echo 'Collector helper ended. The ordinary-user channel remains available for final verification.'
echo 'Leave this terminal open until the experiment and cleanup have finished.'
wait "$channel_pid"
