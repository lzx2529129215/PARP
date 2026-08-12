#!/usr/bin/env python3
"""Generate two fixture-only, read-only QQ collection scenarios."""

import argparse
import json
from pathlib import Path
import random


QQ_CLASS = "^parp-phase210-qq$"


def marker(session, repeat, event, operation):
    return {
        "type": "trace_marker", "event_type": event,
        "app_key": "QQ", "operation_name": operation,
        "operation_id": "%s_r%02d_%s" % (session, repeat, operation.lower()),
        "metadata": {"source": "PHASE210_AUTHORIZED_QQ_ACCOUNT_NO_SEND",
                     "feature_eligible": False, "repeat_id": repeat},
    }


def scenario(session, seed):
    rng = random.Random(seed)
    scope = "phase210-%s" % session
    command = ("env XDG_CONFIG_HOME=${QQ_PROFILE}/config "
               "XDG_DATA_HOME=${QQ_PROFILE}/data "
               "XDG_CACHE_HOME=${QQ_PROFILE}/cache "
               "/opt/QQ/qq --user-data-dir=${QQ_PROFILE}/chromium")
    retag = (
        "i=0; wid=''; while [ -z \"$wid\" ]; do "
        "i=$((i+1)); [ $i -lt 60 ] || exit 72; "
        "pid=$(pgrep -f -- '--user-data-dir=${QQ_PROFILE}/chromium' | head -1 || true); "
        "[ -n \"$pid\" ] && wid=$(xdotool search --onlyvisible --pid \"$pid\" 2>/dev/null | tail -1 || true); "
        "[ -n \"$wid\" ] || sleep 1; done; "
        "xdotool set_window --class parp-phase210-qq --classname parp-phase210-qq "
        "--name 'PARP Phase2.10 QQ Test (Read-only)' \"$wid\"; "
        "wmctrl -i -r \"$wid\" -t 0; "
        "xdotool windowmap \"$wid\"; "
        "xdotool windowsize \"$wid\" 900 700; "
        "xdotool windowmove \"$wid\" 120 80; "
        "xdotool windowraise \"$wid\"; "
        "xdotool windowactivate --sync \"$wid\""
    )
    actions = [
        {"type": "launch", "name": "qq_phase210", "scope_name": scope,
         "app_key": "QQ", "command": command, "label": "QQ_ISOLATED_LAUNCH"},
        {"type": "shell", "command": retag, "label": "QQ_RETAG_UNIQUE_TEST_WINDOW"},
        {"type": "wait_window", "name": "qq_phase210", "class": QQ_CLASS,
         "timeout": 60, "label": "QQ_TEST_WINDOW_READY"},
        {"type": "shell", "command": "i=0; while [ ! -e '${COLLECTOR_READY}' ]; do i=$((i+1)); [ $i -lt 180 ] || exit 70; sleep 1; done",
         "label": "WAIT_FOR_ROOT_COLLECTOR"},
    ]
    operations = ("FOREGROUND_REENTRY", "WINDOW_SCROLL", "FIXTURE_SEARCH_CLEAR",
                  "BACKGROUND_COOLING", "WINDOW_RESIZE")
    for repeat in range(1, 31):
        operation = operations[(repeat - 1) % len(operations)]
        actions.append(marker(session, repeat, "OP_START", operation))
        if operation == "FOREGROUND_REENTRY":
            actions.extend([
                {"type": "focus", "class": QQ_CLASS, "name": "qq_phase210"},
                {"type": "wait", "seconds": rng.randint(18, 32)},
            ])
        elif operation == "WINDOW_SCROLL":
            actions.extend([
                {"type": "focus", "class": QQ_CLASS, "name": "qq_phase210"},
                {"type": "scroll", "class": QQ_CLASS, "name": "qq_phase210",
                 "direction": "down", "amount": 4, "x_ratio": 0.20, "y_ratio": 0.45},
                {"type": "wait", "seconds": rng.randint(12, 20)},
                {"type": "scroll", "class": QQ_CLASS, "name": "qq_phase210",
                 "direction": "up", "amount": 3, "x_ratio": 0.20, "y_ratio": 0.45},
            ])
        elif operation == "FIXTURE_SEARCH_CLEAR":
            actions.extend([
                {"type": "focus", "class": QQ_CLASS, "name": "qq_phase210"},
                {"type": "click_window", "class": QQ_CLASS, "name": "qq_phase210",
                 "x_ratio": 0.15, "y_ratio": 0.055},
                {"type": "type", "text": "PARP_PHASE210_FIXTURE_%02d" % repeat,
                 "delay_ms": 25},
                {"type": "wait", "seconds": rng.randint(10, 18)},
                {"type": "key", "key": "ctrl+a"},
                {"type": "key", "key": "BackSpace"},
            ])
        elif operation == "BACKGROUND_COOLING":
            actions.extend([
                {"type": "window_state", "class": QQ_CLASS, "name": "qq_phase210", "state": "minimize"},
                {"type": "wait", "seconds": rng.randint(25, 45)},
                {"type": "window_state", "class": QQ_CLASS, "name": "qq_phase210", "state": "restore"},
            ])
        else:
            actions.extend([
                {"type": "window_state", "class": QQ_CLASS, "name": "qq_phase210", "state": "maximize"},
                {"type": "wait", "seconds": rng.randint(12, 20)},
                {"type": "window_state", "class": QQ_CLASS, "name": "qq_phase210", "state": "restore"},
            ])
        actions.extend([
            {"type": "wait", "seconds": rng.randint(35, 60), "label": "FIXED_SEED_DWELL"},
            marker(session, repeat, "OP_DONE", operation),
        ])
    actions.extend([
        {"type": "shell", "command": "touch '${AUTOMATION_DONE}'; i=0; while [ ! -e '${COLLECTOR_DONE}' ]; do i=$((i+1)); [ $i -lt 180 ] || exit 71; sleep 1; done",
         "label": "WAIT_FOR_COLLECTOR_FINALIZE"},
        {"type": "close", "name": "qq_phase210", "wait_after_window_close": 2,
         "force_after_seconds": 2, "label": "CLOSE_ONLY_TRACKED_QQ_SCOPE"},
    ])
    for action in actions:
        if action.get("class") == QQ_CLASS:
            action["strict_window_match"] = True
            action["include_hidden"] = True
            action["pid_cmdline_contains"] = "${QQ_PROFILE}/chromium"
            action["retag_class"] = "parp-phase210-qq"
            if action.get("type") == "focus":
                action.update({
                    "ensure_visible_geometry": True,
                    "window_x": 120, "window_y": 80,
                    "window_width": 900, "window_height": 700,
                })
    return {
        "schema_version": 1, "scenario_id": "phase210_%s" % session,
        "seed": seed, "privacy_mode": "AUTHORIZED_QQ_ACCOUNT_READ_ONLY_NO_SEND",
        "forbidden": ["send_message", "upload_real_file", "real_home_path", "real_contact"],
        "actions": actions,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    for session, seed in (("qq_train_01", 21011), ("qq_validation_01", 21029)):
        path = args.output / (session + ".json")
        path.write_text(json.dumps(scenario(session, seed), indent=2, sort_keys=True) + "\n")
        print(path)


if __name__ == "__main__":
    main()
