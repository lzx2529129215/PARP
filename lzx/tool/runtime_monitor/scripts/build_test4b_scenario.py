#!/usr/bin/env python3
"""Derive a Test4B scenario without changing its scored validation sequence."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
MIB = 1024 * 1024
APPS = ("FIREFOX", "THUNDERBIRD", "TELEGRAM")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--session-dir", type=Path, required=True)
    parser.add_argument("--base-scenario", type=Path, default=ROOT / "configs/automation/test4_validation_sequence_108_4_60.json")
    parser.add_argument("--base-scope", type=Path, default=ROOT / "configs/runtime/test1_runtime_app_scope.json")
    parser.add_argument("--ballast-bin", type=Path, default=ROOT / "runtime_monitor/tools/parp_memory_ballast")
    parser.add_argument("--anon-cold-bytes", type=int, default=32 * MIB)
    parser.add_argument("--anon-hot-bytes", type=int, default=8 * MIB)
    parser.add_argument("--file-cold-bytes", type=int, default=48 * MIB)
    parser.add_argument("--file-hot-bytes", type=int, default=8 * MIB)
    parser.add_argument("--hot-interval-ms", type=int, default=1000)
    args = parser.parse_args()
    if not args.ballast_bin.is_file():
        raise SystemExit(f"ballast binary missing: {args.ballast_bin}")
    if min(args.anon_cold_bytes, args.anon_hot_bytes, args.file_cold_bytes, args.file_hot_bytes) <= 0:
        raise SystemExit("all Test4B ballast regions must be positive")

    session = args.session_dir.resolve()
    session.mkdir(parents=True, exist_ok=True)
    ballast_dir = session / "ballast"; ballast_dir.mkdir(exist_ok=True)
    files_dir = session / "ballast_files" / session.name; files_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(str(session).encode()).hexdigest()[:16]
    socket_dir = Path(f"/run/user/{os.getuid()}/t4b-{digest}")
    socket_dir.mkdir(mode=0o700, parents=True, exist_ok=True)

    total = args.anon_cold_bytes + args.anon_hot_bytes + args.file_cold_bytes + args.file_hot_bytes
    config: dict[str, Any] = {"slice": "test4b-experiment.slice", "max_global_bytes": total * len(APPS), "apps": {}}
    for app in APPS:
        config["apps"][app] = {
            "socket_path": str(socket_dir / f"{app.lower()}.sock"),
            "log_path": str(ballast_dir / f"raw_events_{app.lower()}.csv"),
            "file_path": str(files_dir / f"{app.lower()}.bin"),
            "anon_cold_bytes": args.anon_cold_bytes, "anon_hot_bytes": args.anon_hot_bytes,
            "file_cold_bytes": args.file_cold_bytes, "file_hot_bytes": args.file_hot_bytes,
            "hot_interval_ms": args.hot_interval_ms,
        }
    ballast_config = ballast_dir / "ballast_config.json"
    ballast_config.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    scope = json.loads(args.base_scope.read_text(encoding="utf-8"))
    scope["slice"] = "test4b-experiment.slice"
    scope_path = session / "configs" / "test4b_runtime_app_scope.json"; scope_path.parent.mkdir(exist_ok=True)
    scope_path.write_text(json.dumps(scope, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    scenario = json.loads(args.base_scenario.read_text(encoding="utf-8"))
    # The generated scenario lives under outputs/, so the automation runner's
    # PROJECT_ROOT expansion would otherwise point at that output directory.
    # Resolve Firefox from the checked-in project before moving the scenario.
    bundled_firefox = ROOT / "tools" / "firefox" / "firefox" / "firefox"
    if bundled_firefox.is_file():
        scenario.setdefault("variables", {})["FIREFOX_BIN"] = str(bundled_firefox)
    altered: list[str] = []
    for action in scenario.get("actions", []):
        app = str(action.get("app_key", ""))
        if app not in APPS or action.get("type") not in {"launch", "shell"}:
            continue
        original = str(action.get("command", ""))
        if not original:
            continue
        item = config["apps"][app]
        ballast = " ".join([
            shlex.quote(str(args.ballast_bin)), "--app-key", app,
            "--socket", shlex.quote(item["socket_path"]), "--log", shlex.quote(item["log_path"]),
            "--file", shlex.quote(item["file_path"]), "--anon-cold", str(args.anon_cold_bytes),
            "--anon-hot", str(args.anon_hot_bytes), "--file-cold", str(args.file_cold_bytes),
            "--file-hot", str(args.file_hot_bytes), "--hot-interval-ms", str(args.hot_interval_ms),
        ])
        original_type = action.get("type", "")
        action["type"] = "shell"
        action["command"] = f"{ballast} & exec sh -c {shlex.quote(original)}"
        action["metadata"] = {"test4b_ballast_wrapped": True, "original_type": original_type}
        altered.append(app)

    # The validation source ends with Thunderbird still alive but never
    # returns to it.  This is a clearly marked, *unscored* post-validation
    # probe so a reclaimed Thunderbird can run hot/cold verification.
    actions = list(scenario.get("actions", []))
    first_close = next((index for index, action in enumerate(actions) if action.get("type") == "close"), len(actions))
    probe = [
        {"type": "trace_marker", "event_type": "TEST4B_POST_VALIDATION_PROBE_START", "app_key": "THUNDERBIRD", "label": "TEST4B_POST_VALIDATION_UNSCORED"},
        {"type": "switch", "name": "thunderbird", "app_key": "THUNDERBIRD", "class": "thunderbird|Thunderbird", "title": "Thunderbird", "optional": True, "label": "TEST4B_POST_VALIDATION_REACCESS_THUNDERBIRD"},
        {"type": "wait", "seconds": 2, "label": "TEST4B_POST_VALIDATION_REACCESS_SETTLE"},
        {"type": "trace_marker", "event_type": "TEST4B_POST_VALIDATION_PROBE_END", "app_key": "THUNDERBIRD", "label": "TEST4B_POST_VALIDATION_UNSCORED"},
    ]
    scenario["actions"] = actions[:first_close] + probe + actions[first_close:]
    scenario["description"] = str(scenario.get("description", "")) + " Test4B wraps launches only; scored validation actions/dwells are unchanged; post-validation reaccess probe is unscored."
    scenario_path = session / "configs" / "test4b_validation_sequence.json"
    scenario_path.write_text(json.dumps(scenario, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (ballast_dir / "ballast_files_manifest.json").write_text(json.dumps({
        "session_id": session.name, "filesystem": "disk", "files_dir": str(files_dir), "socket_dir": str(socket_dir),
        "apps": list(APPS), "bytes_per_app": total, "altered_launches": altered,
        "validation_contract": "base validation actions and dwells preserved; only launch command wrapping plus an explicitly unscored post-validation probe",
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"scenario": str(scenario_path), "scope": str(scope_path), "ballast_config": str(ballast_config), "bytes_per_app": total}, ensure_ascii=False))


if __name__ == "__main__":
    main()
