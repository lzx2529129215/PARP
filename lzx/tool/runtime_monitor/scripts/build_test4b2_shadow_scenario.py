#!/usr/bin/env python3
"""Build Test4B-2's preallocated 3 x 40 MiB SHADOW scenario.

Each four-MiB region chunk has its own no-window ballast process.  This is
intentional: allocating one sidecar at a time makes the construction rate
auditable and enforces the experiment's 4 MiB safety boundary without adding a
new allocation primitive to the C helper.
"""
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
REGIONS = (("FILE_COLD_ALLOC", "file_cold_bytes", 6), ("FILE_HOT_ALLOC", "file_hot_bytes", 1),
           ("ANON_COLD_ALLOC", "anon_cold_bytes", 2), ("ANON_HOT_ALLOC", "anon_hot_bytes", 1))


def app_command(app: str) -> str:
    firefox = ROOT / "tools/firefox/firefox/firefox"
    if app == "FIREFOX":
        return (
            'mkdir -p "${FIREFOX_PROFILE}" && env -u WAYLAND_DISPLAY '
            'MOZ_ENABLE_WAYLAND=0 GDK_BACKEND=x11 "${FIREFOX_BIN}" '
            '--new-instance --no-remote -profile "${FIREFOX_PROFILE}" --new-window about:blank'
        )
    if app == "THUNDERBIRD":
        return "thunderbird --new-instance"
    if app == "TELEGRAM":
        return "telegram-desktop"
    raise ValueError(app)


def switch_action(app: str, label: str) -> dict[str, Any]:
    values = {
        "FIREFOX": ("firefox", "firefox|Firefox", "Mozilla Firefox|about:blank"),
        "THUNDERBIRD": ("thunderbird", "thunderbird|Thunderbird", "Thunderbird"),
        "TELEGRAM": ("telegram", "telegramdesktop|TelegramDesktop|telegram-desktop", "Telegram"),
    }
    name, klass, title = values[app]
    return {"type": "switch", "name": name, "app_key": app, "class": klass, "title": title,
            "label": label, "optional": True}


def ballast_command(binary: Path, app: str, worker: dict[str, Any]) -> str:
    parts = [shlex.quote(str(binary)), "--app-key", app, "--socket", shlex.quote(worker["socket_path"]),
             "--log", shlex.quote(worker["log_path"]), "--file", shlex.quote(worker["file_path"]),
             "--anon-cold", str(worker["anon_cold_bytes"]), "--anon-hot", str(worker["anon_hot_bytes"]),
             "--file-cold", str(worker["file_cold_bytes"]), "--file-hot", str(worker["file_hot_bytes"]),
             "--hot-interval-ms", "1000"]
    return " ".join(parts)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--session-dir", type=Path, required=True)
    parser.add_argument("--base-scenario", type=Path, default=ROOT / "configs/automation/test4_validation_sequence_108_4_60.json")
    parser.add_argument("--base-scope", type=Path, default=ROOT / "configs/runtime/test1_runtime_app_scope.json")
    parser.add_argument("--ballast-bin", type=Path, default=ROOT / "runtime_monitor/tools/parp_memory_ballast")
    parser.add_argument("--chunk-bytes", type=int, default=4 * MIB)
    parser.add_argument("--pause-seconds", type=float, default=.75)
    parser.add_argument("--system-reserve-bytes", type=int, default=512 * MIB)
    parser.add_argument("--cgroup-reserve-bytes", type=int, default=64 * MIB)
    parser.add_argument("--psi-full-abort-avg10", type=float, default=.20)
    parser.add_argument("--startup-ready-timeout-s", type=float, default=180.0,
                        help="Maximum no-allocation wait for the unchanged PSI gate.")
    parser.add_argument("--startup-psi-full-start-avg10", type=float, default=.05,
                        help="Stricter steady-state target before construction; runtime abort remains 0.20.")
    args = parser.parse_args()
    if not args.ballast_bin.is_file():
        raise SystemExit(f"ballast binary missing: {args.ballast_bin}")
    if args.chunk_bytes != 4 * MIB:
        raise SystemExit("Test4B-2 fixes the construction chunk at 4 MiB")

    session = args.session_dir.resolve(); session.mkdir(parents=True, exist_ok=True)
    ballast_dir = session / "ballast"; ballast_dir.mkdir(exist_ok=True)
    (ballast_dir / "raw").mkdir(exist_ok=True)
    files_dir = session / "ballast_files" / session.name; files_dir.mkdir(parents=True, exist_ok=True)
    token = hashlib.sha256(str(session).encode()).hexdigest()[:16]
    socket_dir = Path(f"/run/user/{os.getuid()}/t4b-{token}"); socket_dir.mkdir(mode=0o700, parents=True, exist_ok=True)

    config: dict[str, Any] = {
        "slice": "test4b-experiment.slice", "allocation_mode": "external_preallocated",
        "max_global_bytes": len(APPS) * 40 * MIB, "chunk_bytes": args.chunk_bytes,
        "pause_seconds": args.pause_seconds, "system_reserve_bytes": args.system_reserve_bytes,
        "cgroup_reserve_bytes": args.cgroup_reserve_bytes, "psi_full_abort_avg10": args.psi_full_abort_avg10,
        "startup_stabilization_seconds": 15.0, "startup_stable_required_samples": 3,
        "startup_ready_timeout_seconds": args.startup_ready_timeout_s,
        "startup_psi_full_start_avg10": args.startup_psi_full_start_avg10,
        "apps": {}, "cleanup_paths": [],
    }
    all_workers: list[dict[str, Any]] = []
    for app in APPS:
        workers: list[dict[str, Any]] = []
        for stage, field, count in REGIONS:
            for index in range(count):
                worker_id = f"{stage.lower()}_{index:02d}"
                worker = {
                    "worker_id": worker_id, "stage": stage,
                    "socket_path": str(socket_dir / f"{app.lower()}-{worker_id}.sock"),
                    "log_path": str(ballast_dir / "raw" / f"{app.lower()}-{worker_id}.csv"),
                    "file_path": str(files_dir / f"{app.lower()}-{worker_id}.bin"),
                    "anon_cold_bytes": 0, "anon_hot_bytes": 0,
                    "file_cold_bytes": 0, "file_hot_bytes": 0,
                }
                worker[field] = args.chunk_bytes
                workers.append(worker); all_workers.append({"app_key": app, **worker})
                config["cleanup_paths"].append({"socket_path": worker["socket_path"], "file_path": worker["file_path"]})
        config["apps"][app] = {"workers": workers}
    if sum(sum(w[key] for w in config["apps"]["FIREFOX"]["workers"]) for key in ("anon_cold_bytes", "anon_hot_bytes", "file_cold_bytes", "file_hot_bytes")) != 40 * MIB:
        raise SystemExit("internal Test4B-2 ballast-size assertion failed")
    ballast_config = ballast_dir / "ballast_config.json"
    ballast_config.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (ballast_dir / "workers.json").write_text(json.dumps({"workers": all_workers}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    scope = json.loads(args.base_scope.read_text(encoding="utf-8")); scope["slice"] = "test4b-experiment.slice"
    scope_path = session / "configs/test4b2_runtime_app_scope.json"; scope_path.parent.mkdir(exist_ok=True)
    scope_path.write_text(json.dumps(scope, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    workers_by_app = {app: config["apps"][app]["workers"] for app in APPS}
    launch_actions: list[dict[str, Any]] = []
    for app in APPS:
        sidecars = " ".join(f"{ballast_command(args.ballast_bin, app, worker)} &" for worker in workers_by_app[app])
        launch_actions.append({"type": "shell", "name": app.lower(), "app_key": app,
                               "command": f"{sidecars} exec sh -c {shlex.quote(app_command(app))}",
                               "label": f"TEST4B2_BOOTSTRAP_OPEN_{app}", "optional": True})

    preallocator = ROOT / "runtime_monitor/scripts/run_test4b2_preallocate.py"
    activation = session / "test4b2_controller_active"
    preamble: list[dict[str, Any]] = launch_actions + [
        {"type": "shell", "command": f"python3 {shlex.quote(str(preallocator))} --session-dir {shlex.quote(str(session))} --phase stabilize", "label": "TEST4B2_STARTUP_STABILIZE"},
        # Window-map timing is deliberately not assumed: Thunderbird can
        # become foreground after Telegram's launch.  Establish the required
        # foreground app as an explicit X11 event before its guarded chunks.
        switch_action("TELEGRAM", "TEST4B2_PREALLOCATE_FOREGROUND_TELEGRAM"),
        {"type": "shell", "command": f"python3 {shlex.quote(str(preallocator))} --session-dir {shlex.quote(str(session))} --phase allocate --app TELEGRAM", "label": "TEST4B2_ALLOCATE_TELEGRAM"},
        switch_action("FIREFOX", "TEST4B2_PREALLOCATE_FOREGROUND_FIREFOX"),
        {"type": "shell", "command": f"python3 {shlex.quote(str(preallocator))} --session-dir {shlex.quote(str(session))} --phase allocate --app FIREFOX", "label": "TEST4B2_ALLOCATE_FIREFOX"},
        switch_action("THUNDERBIRD", "TEST4B2_PREALLOCATE_FOREGROUND_THUNDERBIRD"),
        {"type": "shell", "command": f"python3 {shlex.quote(str(preallocator))} --session-dir {shlex.quote(str(session))} --phase allocate --app THUNDERBIRD", "label": "TEST4B2_ALLOCATE_THUNDERBIRD"},
        {"type": "shell", "command": f"touch {shlex.quote(str(activation))}", "label": "TEST4B2_CONTROLLER_ACTIVATED"},
        {"type": "wait", "seconds": .25, "label": "TEST4_BOOTSTRAP_NOT_SCORED"},
    ]
    base = json.loads(args.base_scenario.read_text(encoding="utf-8"))
    validation = list(base.get("actions", []))[3:]
    # Telegram is already deliberately launched during the unscored setup.
    for action in validation:
        if action.get("label") == "TEST4_VAL_00_FIRST_LAUNCH_TELEGRAM":
            action.clear(); action.update(switch_action("TELEGRAM", "TEST4_VAL_00_FIRST_LAUNCH_TELEGRAM"))
    # The validation-derived alternating sequence only reaches the selected
    # 0.08 low-probability region for the long-idle Thunderbird immediately
    # after its final transition.  The source scenario closes Thunderbird at
    # that same boundary.  Keep validation untouched and add one explicitly
    # unscored transition before close, so the controller can evaluate the
    # already-open, cold, background target while it still exists.
    first_close = next((index for index, action in enumerate(validation) if action.get("type") == "close"), len(validation))
    candidate_maturation = [
        {"type": "trace_marker", "event_type": "TEST4B2_CANDIDATE_MATURATION_START", "app_key": "THUNDERBIRD", "label": "TEST4B2_POST_VALIDATION_UNSCORED"},
        switch_action("FIREFOX", "TEST4B2_POST_VALIDATION_CANDIDATE_MATURATION"),
        {"type": "wait", "seconds": 3, "label": "TEST4B2_POST_VALIDATION_CANDIDATE_SETTLE"},
        {"type": "trace_marker", "event_type": "TEST4B2_CANDIDATE_MATURATION_END", "app_key": "THUNDERBIRD", "label": "TEST4B2_POST_VALIDATION_UNSCORED"},
    ]
    scenario = {
        **base,
        "description": "Test4B-2: three apps are opened first, then their 4 MiB ballast chunks are constructed only while foreground; the original split validation begins after TEST4_BOOTSTRAP_NOT_SCORED.",
        # Keep Test4's shared, pre-warmed Firefox profile.  A fresh profile
        # per run measures first-start migration/indexing pressure instead of
        # app-switch behavior and made the system PSI gate non-repeatable.
        "variables": {**dict(base.get("variables", {})), "FIREFOX_BIN": str(ROOT / "tools/firefox/firefox/firefox")},
        "actions": preamble + validation[:first_close] + candidate_maturation + validation[first_close:],
    }
    scenario_path = session / "configs/test4b2_validation_sequence.json"
    scenario_path.write_text(json.dumps(scenario, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (ballast_dir / "ballast_manifest.json").write_text(json.dumps({
        "session_id": session.name, "apps": list(APPS), "chunks_per_app": 10,
        "chunk_bytes": args.chunk_bytes, "layout_bytes_per_app": {"file_cold": 24*MIB, "file_hot": 4*MIB, "anon_cold": 8*MIB, "anon_hot": 4*MIB},
        "global_ballast_bytes": 120*MIB, "activation_file": str(activation), "scenario": str(scenario_path),
        "contract": "No memory.reclaim command exists in Test4B-2; controller mode is SHADOW only. The original scored validation/dwell segment is unchanged; the one post-validation candidate-maturation switch is explicitly unscored.",
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"scenario": str(scenario_path), "scope": str(scope_path), "ballast_config": str(ballast_config), "activation_file": str(activation), "bytes_per_app": 40*MIB, "global_bytes": 120*MIB}, ensure_ascii=False))


if __name__ == "__main__":
    main()
