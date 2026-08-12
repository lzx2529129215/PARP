#!/usr/bin/env python3
"""Prepare deterministic, repeated Phase2.8 collection scenarios.

This module never starts an application and never requires privilege.  It
derives runnable scenarios from the already validated Phase2.7B automation
schema, adds repeat identifiers and causal baseline/recovery periods, and
writes a machine-readable collection manifest.
"""

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import random


WPS_GROUPS = {
    "OPEN_CLOSE": ("CLOSE_DOCUMENT", "REOPEN"),
    "VIEW_IDLE": ("IDLE_VIEW",),
    "NAVIGATION_FORWARD": ("SCROLL_DOWN",),
    "NAVIGATION_BACKWARD": ("SCROLL_UP",),
    "EDIT_SAVE": ("EDIT", "SAVE"),
    "SEARCH": ("SEARCH",),
    "FOREGROUND_BACKGROUND": ("MINIMIZE", "RESTORE"),
}
FILES_GROUPS = {
    "DIRECTORY_VIEW": ("BROWSE_LIST",),
    "NAVIGATION_PAIR": ("ENTER_DIRECTORY", "RETURN_DIRECTORY"),
    "SEARCH": ("SEARCH",),
    "FOREGROUND_BACKGROUND": ("MINIMIZE", "RESTORE"),
}
EXPECTED_COARSE = {
    "WPS": {
        "OPEN_CLOSE", "VIEW_IDLE", "NAVIGATION_FORWARD", "NAVIGATION_BACKWARD",
        "EDIT", "SAVE_WRITE", "SEARCH", "FOREGROUND_BACKGROUND",
    },
    "FILES": {
        "DIRECTORY_VIEW", "NAVIGATION_FORWARD", "NAVIGATION_BACKWARD", "SEARCH",
        "FOREGROUND_BACKGROUND",
    },
}
RAW_TO_COARSE = {
    "BROWSE_LIST": "DIRECTORY_VIEW",
    "CLOSE_DOCUMENT": "OPEN_CLOSE",
    "EDIT": "EDIT",
    "ENTER_DIRECTORY": "NAVIGATION_FORWARD",
    "IDLE_VIEW": "VIEW_IDLE",
    "MINIMIZE": "FOREGROUND_BACKGROUND",
    "OPEN": "OPEN_CLOSE",
    "REOPEN": "OPEN_CLOSE",
    "RESTORE": "FOREGROUND_BACKGROUND",
    "RETURN_DIRECTORY": "NAVIGATION_BACKWARD",
    "SAVE": "SAVE_WRITE",
    "SCROLL_DOWN": "NAVIGATION_FORWARD",
    "SCROLL_UP": "NAVIGATION_BACKWARD",
    "SEARCH": "SEARCH",
}


def atomic_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.write("\n")
    os.replace(temporary, path)


def operation_blocks(actions):
    """Return the first complete automation block for every raw operation."""
    blocks = {}
    index = 0
    while index < len(actions):
        start = actions[index]
        if start.get("type") != "trace_marker" or start.get("event_type") != "OP_START":
            index += 1
            continue
        operation = start["operation_name"]
        operation_id = start["operation_id"]
        end = index + 1
        while end < len(actions):
            candidate = actions[end]
            if (candidate.get("type") == "trace_marker" and
                    candidate.get("event_type") == "OP_DONE" and
                    candidate.get("operation_id") == operation_id):
                break
            end += 1
        if end == len(actions):
            raise ValueError("unterminated operation block: %s" % operation)
        blocks.setdefault(operation, copy.deepcopy(actions[index:end + 1]))
        index = end + 1
    return blocks


def instantiate(block, session_number, repeat_number, ordinal):
    raw_name = block[0]["operation_name"]
    repeat_id = "s%02d-%s-r%02d-%02d" % (
        session_number, raw_name.lower(), repeat_number, ordinal)
    operation_id = "${SESSION_ID}_" + repeat_id
    output = [{
        "type": "wait", "seconds": 20, "name": block[0].get("app_key", "app").lower(),
        "label": "PHASE28_STABLE_BASELINE_" + repeat_id,
    }]
    for action in copy.deepcopy(block):
        if action.get("type") == "trace_marker":
            action["operation_id"] = operation_id
            metadata = action.setdefault("metadata", {})
            metadata.update({
                "source": "SAFE_X11_AUTOMATION_OFFLINE_LABEL_ONLY",
                "repeat_id": repeat_id,
                "phase28_session_ordinal": session_number,
                "feature_eligible": False,
            })
        output.append(action)
    # A guaranteed twenty-second in-operation dwell avoids relying on GUI
    # execution-time estimates when validating the operation duration.
    output.insert(-1, {
        "type": "wait", "seconds": 20, "name": block[0].get("app_key", "app").lower(),
        "label": "PHASE28_OPERATION_DWELL_" + repeat_id,
    })
    output.append({
        "type": "wait", "seconds": 20, "name": block[0].get("app_key", "app").lower(),
        "label": "PHASE28_RECOVERY_" + repeat_id,
    })
    return output, repeat_id, raw_name


def build_scenario(template, app, session_number, repeats, groups):
    actions = template["actions"]
    blocks = operation_blocks(actions)
    required = {item for group in groups.values() for item in group}
    missing = sorted(required - set(blocks))
    if missing:
        raise ValueError("automation schema lacks operations: %s" % ", ".join(missing))

    scenario_actions = copy.deepcopy(actions[:3])
    manifest_rows = []
    if app == "WPS":
        # WPS starts on the recent-documents screen.  Opening the per-session
        # fixture is setup, not an online feature.  It is still labelled and
        # receives a unique repeat_id for auditability.
        setup, repeat_id, raw_name = instantiate(blocks["OPEN"], session_number, 0, 0)
        scenario_actions.extend(setup)
        manifest_rows.append((repeat_id, raw_name, RAW_TO_COARSE[raw_name], 0, "SETUP"))

    for repeat_number in range(1, repeats + 1):
        order = list(groups)
        # Fixed seed makes the schedule reproducible while changing both the
        # time position and order across session/repeat combinations.
        random.Random(280000 + session_number * 100 + repeat_number).shuffle(order)
        for group_name in order:
            for ordinal, operation in enumerate(groups[group_name], start=1):
                materialized, repeat_id, raw_name = instantiate(
                    blocks[operation], session_number, repeat_number, ordinal)
                scenario_actions.extend(materialized)
                manifest_rows.append((repeat_id, raw_name, RAW_TO_COARSE[raw_name],
                                      repeat_number, group_name))

    scenario_actions.extend(copy.deepcopy(actions[-2:]))
    return {"validation_mode": True, "actions": scenario_actions}, manifest_rows


def prepare(source_dir, output_dir):
    source_dir = source_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    templates = {
        "WPS": json.load((source_dir / "phase27b_wps_real.json").open(encoding="utf-8")),
        "FILES": json.load((source_dir / "phase27b_files_real.json").open(encoding="utf-8")),
    }
    sessions = []
    all_rows = []
    definitions = [
        ("WPS", 1, 5, WPS_GROUPS, "small", "word_0040_fixture.docx"),
        ("WPS", 2, 5, WPS_GROUPS, "medium", "word_200m.docx"),
        ("WPS", 3, 5, WPS_GROUPS, "large", "word_200m.docx+padded-copy"),
        ("FILES", 1, 3, FILES_GROUPS, "fixture-tree-a", "files_root"),
        ("FILES", 2, 3, FILES_GROUPS, "fixture-tree-b", "files_root"),
    ]
    for app, ordinal, repeats, groups, scale, document in definitions:
        scenario, rows = build_scenario(templates[app], app, ordinal, repeats, groups)
        filename = "phase28_%s_repeated_%02d.json" % (app.lower(), ordinal)
        atomic_json(output_dir / filename, scenario)
        counts = {}
        for repeat_id, raw_name, coarse, repeat, group in rows:
            all_rows.append({
                "session_id": "%s_%02d" % (app.lower(), ordinal),
                "app": app, "repeat_id": repeat_id, "raw_operation": raw_name,
                "coarse_operation": coarse, "repeat_number": repeat,
                "dependency_group": group, "online_feature_eligible": False,
            })
            if repeat:
                counts[coarse] = counts.get(coarse, 0) + 1
        missing = sorted(EXPECTED_COARSE[app] - set(counts))
        minimum = 5 if app == "WPS" else 3
        if missing or any(counts[name] < minimum for name in EXPECTED_COARSE[app]):
            raise AssertionError("incomplete repeated-operation coverage: %s" % (missing, counts))
        sessions.append({
            "session_id": "%s_%02d" % (app.lower(), ordinal), "app": app,
            "scenario": filename, "document_scale": scale, "fixture": document,
            "required_repeats_per_coarse_operation": minimum,
            "coarse_operation_counts": counts,
        })

    manifest = {
        "schema_version": 1,
        "source": "RUNTIME_PHASE28_REAL_FRESH_PLANNED",
        "feature_boundary": "foreground_app_id plus kernel/cgroup state only",
        "automation_role": "OFFLINE_LABEL_ONLY",
        "baseline_seconds": 20,
        "minimum_operation_seconds": 20,
        "recovery_seconds": 20,
        "sessions": sessions,
        "operations": all_rows,
        "qq_status": "QQ_COLLECTION_AUTH_GATED_NOT_REQUIRED",
        "kernel_write": False,
        "apply": False,
        "prefetch": False,
        "anon_pageout": False,
    }
    canonical = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    manifest["manifest_sha256"] = hashlib.sha256(canonical).hexdigest()
    atomic_json(output_dir / "phase28_collection_manifest.json", manifest)
    return manifest


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    manifest = prepare(args.source_dir, args.output_dir)
    print(json.dumps({
        "sessions": len(manifest["sessions"]),
        "operation_instances": len(manifest["operations"]),
        "manifest_sha256": manifest["manifest_sha256"],
    }, sort_keys=True))


if __name__ == "__main__":
    main()
