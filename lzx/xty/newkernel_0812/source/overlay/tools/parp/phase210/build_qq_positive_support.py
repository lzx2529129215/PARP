#!/usr/bin/env python3
"""Build the bounded, no-send Phase2.10 QQ positive-support pilot."""

import argparse
import json
from pathlib import Path


QQ_CLASS = "^parp-phase210-qq$"
SESSION = "qq_positive_support_pilot"


def marker(repeat, event):
    return {
        "type": "trace_marker",
        "event_type": event,
        "app_key": "QQ",
        "operation_name": "REPEATED_SAFE_UI_REVISIT",
        "operation_id": "%s_r%02d" % (SESSION, repeat),
        "metadata": {
            "source": "PHASE210_POSITIVE_SUPPORT_PILOT_NO_SEND",
            "feature_eligible": False,
            "repeat_id": repeat,
        },
    }


def strict(action):
    if action.get("class") == QQ_CLASS:
        action.update({
            "strict_window_match": True,
            "include_hidden": True,
            "pid_cmdline_contains": "${QQ_PROFILE}/chromium",
            "retag_class": "parp-phase210-qq",
        })
        if action.get("type") == "focus":
            action.update({
                "ensure_visible_geometry": True,
                "window_x": 120,
                "window_y": 80,
                "window_width": 900,
                "window_height": 700,
            })
    return action


def scenario():
    command = (
        "env XDG_CONFIG_HOME=${QQ_PROFILE}/config "
        "XDG_DATA_HOME=${QQ_PROFILE}/data "
        "XDG_CACHE_HOME=${QQ_PROFILE}/cache "
        "/opt/QQ/qq --user-data-dir=${QQ_PROFILE}/chromium"
    )
    retag = (
        "i=0; wid=''; while [ -z \"$wid\" ]; do "
        "i=$((i+1)); [ $i -lt 60 ] || exit 72; "
        "pid=$(pgrep -f -- '--user-data-dir=${QQ_PROFILE}/chromium' | head -1 || true); "
        "[ -n \"$pid\" ] && wid=$(xdotool search --onlyvisible --pid \"$pid\" 2>/dev/null | tail -1 || true); "
        "[ -n \"$wid\" ] || sleep 1; done; "
        "xdotool set_window --class parp-phase210-qq --classname parp-phase210-qq "
        "--name 'PARP Phase2.10 QQ Positive-Support Pilot' \"$wid\"; "
        "wmctrl -i -r \"$wid\" -t 0; xdotool windowmap \"$wid\"; "
        "xdotool windowsize \"$wid\" 900 700; xdotool windowmove \"$wid\" 120 80; "
        "xdotool windowraise \"$wid\"; xdotool windowactivate --sync \"$wid\""
    )
    actions = [
        {"type": "launch", "name": "qq_phase210", "scope_name": "phase210-%s" % SESSION,
         "app_key": "QQ", "command": command, "label": "QQ_ISOLATED_PILOT_LAUNCH"},
        {"type": "shell", "command": retag, "label": "QQ_RETAG_UNIQUE_PILOT_WINDOW"},
        {"type": "wait_window", "name": "qq_phase210", "class": QQ_CLASS,
         "timeout": 60, "label": "QQ_PILOT_WINDOW_READY"},
        {"type": "shell",
         "command": "i=0; while [ ! -e '${COLLECTOR_READY}' ]; do i=$((i+1)); [ $i -lt 180 ] || exit 70; sleep 1; done",
         "label": "WAIT_FOR_ROOT_COLLECTOR"},
    ]
    for repeat in range(1, 19):
        actions.extend([
            marker(repeat, "OP_START"),
            {"type": "focus", "class": QQ_CLASS, "name": "qq_phase210"},
            {"type": "wait", "seconds": 4},
            {"type": "scroll", "class": QQ_CLASS, "name": "qq_phase210",
             "direction": "down", "amount": 5, "x_ratio": 0.20, "y_ratio": 0.45},
            {"type": "wait", "seconds": 6},
            {"type": "scroll", "class": QQ_CLASS, "name": "qq_phase210",
             "direction": "up", "amount": 5, "x_ratio": 0.20, "y_ratio": 0.45},
            {"type": "click_window", "class": QQ_CLASS, "name": "qq_phase210",
             "x_ratio": 0.15, "y_ratio": 0.055},
            {"type": "type", "text": "PARP_PHASE210_PILOT", "delay_ms": 20},
            {"type": "wait", "seconds": 4},
            {"type": "key", "key": "ctrl+a"},
            {"type": "key", "key": "BackSpace"},
            {"type": "window_state", "class": QQ_CLASS, "name": "qq_phase210", "state": "minimize"},
            {"type": "wait", "seconds": 12},
            {"type": "window_state", "class": QQ_CLASS, "name": "qq_phase210", "state": "restore"},
            {"type": "wait", "seconds": 8},
            marker(repeat, "OP_DONE"),
        ])
    actions.extend([
        {"type": "shell",
         "command": "touch '${AUTOMATION_DONE}'; i=0; while [ ! -e '${COLLECTOR_DONE}' ]; do i=$((i+1)); [ $i -lt 180 ] || exit 71; sleep 1; done",
         "label": "WAIT_FOR_COLLECTOR_FINALIZE"},
        {"type": "close", "name": "qq_phase210", "wait_after_window_close": 2,
         "force_after_seconds": 2, "label": "CLOSE_ONLY_TRACKED_QQ_SCOPE"},
    ])
    return {
        "schema_version": 1,
        "scenario_id": "phase210_%s" % SESSION,
        "seed": 21071,
        "privacy_mode": "AUTHORIZED_QQ_ACCOUNT_READ_ONLY_NO_SEND",
        "pilot": "POSITIVE_SUPPORT_ONLY",
        "forbidden": ["send_message", "upload_real_file", "real_home_path", "real_contact"],
        "actions": [strict(action) for action in actions],
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    path = args.output / (SESSION + ".json")
    path.write_text(json.dumps(scenario(), indent=2, sort_keys=True) + "\n")
    print(path)


if __name__ == "__main__":
    main()
