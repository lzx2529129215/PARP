#!/usr/bin/env python3
"""Build a repeatable Test4 scenario from one continuous v3 split chain.

No unsupported application is substituted.  The generated sequence keeps the
dataset's foreground order, first-launch versus re-focus semantics, and each
current-App dwell bucket.  Bootstrap launches merely reconstruct the source
opened-App set and are explicitly excluded from scored timing.
"""
from __future__ import annotations

import argparse
import csv
import json
from datetime import datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
SUPPORTED = {
    "Firefox": "FIREFOX", "LibreOffice": "LIBREOFFICE", "VLC": "VLC",
    "GIMP": "GIMP", "Audacity": "AUDACITY", "Thunderbird": "THUNDERBIRD",
    "Telegram": "TELEGRAM", "Evince": "EVINCE", "Files": "FILES",
    "Calculator": "CALCULATOR",
}


def load_templates(path: Path) -> tuple[dict[tuple[str, str], dict[str, Any]], dict[str, Any]]:
    source = json.loads(path.read_text(encoding="utf-8"))
    templates: dict[tuple[str, str], dict[str, Any]] = {}

    def walk(items: list[dict[str, Any]]) -> None:
        for item in items:
            if item.get("type") == "repeat":
                walk(item.get("actions", []))
                continue
            app = item.get("app_key")
            if app and item.get("type") in {"launch", "shell", "switch", "close"}:
                templates.setdefault((str(app), str(item["type"])), item)

    walk(source["actions"])
    return templates, source.get("variables", {})


def source_dwell_seconds(row: dict[str, str], next_row: dict[str, str]) -> int:
    """Keep the dwell measured by adjacent foreground-transition timestamps.

    The v3 ``history_durations_s`` column describes history construction, not
    necessarily the current row's outgoing dwell.  The two adjacent split
    timestamps are therefore the authoritative sequence timing contract.
    """
    observed = int((
        datetime.strptime(next_row["timestamp"], "%Y-%m-%d %H:%M:%S")
        - datetime.strptime(row["timestamp"], "%Y-%m-%d %H:%M:%S")
    ).total_seconds())
    if observed <= 0:
        raise SystemExit(f"NON_POSITIVE_DWELL at {row['timestamp']}: {observed}")
    return observed


def action_from_template(
    templates: dict[tuple[str, str], dict[str, Any]], app: str, action_type: str, label: str,
) -> dict[str, Any]:
    key = SUPPORTED[app]
    template = templates.get((key, action_type))
    if template is None and action_type == "launch":
        template = templates.get((key, "shell"))
    if template is None:
        raise SystemExit(f"UNSUPPORTED_APP_NO_{action_type.upper()}_TEMPLATE: {app}")
    action = dict(template)
    action["label"] = label
    return action


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--split", default="val", choices=["val", "test"])
    parser.add_argument("--session-id", default="108:4")
    parser.add_argument("--start-index", type=int, default=60)
    parser.add_argument("--length", type=int, default=9)
    parser.add_argument("--output", default=str(ROOT / "configs/automation/test4_validation_sequence_108_4_60.json"))
    parser.add_argument("--coverage-output", default="")
    args = parser.parse_args()

    dataset = ROOT / "operation_predictor/data/test1/processed/app_lstm_duration_switch" / f"{args.split}.csv"
    with dataset.open(newline="", encoding="utf-8") as stream:
        session_rows = [
            row for row in csv.DictReader(stream)
            if row["session_id"] == args.session_id
            and row["trigger_type"] == "foreground_transition"
            and row["has_next_switch"] == "1"
        ]
    session_rows.sort(key=lambda row: row["timestamp"])
    chain = session_rows[args.start_index:args.start_index + args.length]
    if len(chain) != args.length:
        raise SystemExit("requested chain extends past the available validation session")
    if any(chain[index]["next_app"] != chain[index + 1]["current_app"] for index in range(len(chain) - 1)):
        raise SystemExit("requested rows are not a continuous foreground-transition chain")
    if args.start_index + args.length >= len(session_rows):
        raise SystemExit("requested chain has no following foreground transition to measure its final dwell")
    continuation = session_rows[args.start_index + args.length]
    if chain[-1]["next_app"] != continuation["current_app"]:
        raise SystemExit("requested chain's final label does not continue into the following foreground transition")

    source_apps = {app for row in chain for app in (row["current_app"], row["next_app"])}
    initial_opened = {app for app in chain[0]["opened_apps"].split("|") if app}
    all_apps = source_apps | initial_opened
    unsupported = sorted(all_apps - set(SUPPORTED))
    if unsupported:
        raise SystemExit("UNSUPPORTED_APP: " + ",".join(unsupported))
    templates, variables = load_templates(ROOT / "configs/automation/scenario_test1_app_switch.json")

    actions: list[dict[str, Any]] = []
    started: set[str] = set()
    for app in sorted(initial_opened):
        actions.append(action_from_template(templates, app, "launch", f"TEST4_BOOTSTRAP_OPEN_{SUPPORTED[app]}"))
        started.add(app)
    actions.append({"type": "wait", "seconds": 3, "label": "TEST4_BOOTSTRAP_NOT_SCORED"})

    current = chain[0]["current_app"]
    if current not in started:
        actions.append(action_from_template(templates, current, "launch", "TEST4_VAL_00_FIRST_LAUNCH_" + SUPPORTED[current]))
        started.add(current)
    actions.append(action_from_template(templates, current, "switch", "TEST4_VAL_00_FOREGROUND_" + SUPPORTED[current]))

    dwell_seconds: list[int] = []
    for index, row in enumerate(chain):
        if row["current_app"] != current:
            raise SystemExit("internal sequence continuity check failed")
        dwell = source_dwell_seconds(row, chain[index + 1] if index + 1 < len(chain) else continuation)
        dwell_seconds.append(dwell)
        actions.append({"type": "wait", "seconds": dwell, "label": f"TEST4_VAL_{index:02d}_DWELL_{dwell}S"})
        target = row["next_app"]
        if target not in started:
            actions.append(action_from_template(templates, target, "launch", f"TEST4_VAL_{index:02d}_FIRST_LAUNCH_{SUPPORTED[target]}"))
            started.add(target)
        else:
            actions.append(action_from_template(templates, target, "switch", f"TEST4_VAL_{index:02d}_FOREGROUND_{SUPPORTED[target]}"))
        current = target

    for app in sorted(started, reverse=True):
        template = templates.get((SUPPORTED[app], "close"))
        if template:
            actions.append(dict(template))

    sequence = [row["current_app"] for row in chain] + [chain[-1]["next_app"]]
    output = {
        "description": "Test4 continuous v3 split-derived foreground sequence; bootstrap is unscored.",
        "validation_mode": args.split == "val",
        "variables": variables,
        "actions": actions,
    }
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    coverage = {
        "split": args.split,
        "dataset": str(dataset),
        "session_id": args.session_id,
        "start_index": args.start_index,
        "transition_count": len(chain),
        "apps": sorted(all_apps),
        "unsupported_apps": unsupported,
        "sequence_order": sequence,
        "source_dwell_s": dwell_seconds,
        "dwell_source": "adjacent_foreground_transition_timestamps",
        "initial_opened_apps": sorted(initial_opened),
        "first_launch_apps": sorted(source_apps - initial_opened),
        "status": "PASS",
    }
    coverage_path = Path(args.coverage_output) if args.coverage_output else output_path.with_suffix(".coverage.json")
    coverage_path.write_text(json.dumps(coverage, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(coverage, ensure_ascii=False))


if __name__ == "__main__":
    main()
