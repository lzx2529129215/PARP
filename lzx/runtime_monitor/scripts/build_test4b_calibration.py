#!/usr/bin/env python3
"""Build a foreground-only, 4 MiB-at-a-time Test4B-1 calibration scenario."""
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
CHUNK = 4 * MIB
SCOPE = {"FIREFOX": "automation-firefox.scope", "THUNDERBIRD": "automation-thunderbird.scope", "TELEGRAM": "automation-telegram.scope"}


def phase_plan(phase: str) -> dict[str, list[str]]:
    if phase == "A_FILE":
        return {"FIREFOX": ["FILE_COLD"] * 12 + ["FILE_HOT"]}
    if phase == "B_ANON":
        return {"FIREFOX": ["ANON_COLD"] * 8 + ["ANON_HOT"]}
    if phase == "C_MIXED":
        one = ["FILE_COLD"] * 6 + ["FILE_HOT"] + ["ANON_COLD"] * 2 + ["ANON_HOT"]
        return {app: list(one) for app in ("FIREFOX", "THUNDERBIRD", "TELEGRAM")}
    if phase == "C_BASELINE":
        return {app: [] for app in ("FIREFOX", "THUNDERBIRD", "TELEGRAM")}
    if phase == "C_BASELINE_STABLE":
        return {app: [] for app in ("FIREFOX", "THUNDERBIRD", "TELEGRAM")}
    if phase == "C_MIXED_20":
        # 8 MiB cold file + 4 MiB hot file + 4 MiB cold anon + 4 MiB hot
        # anon.  This is the first lower multi-App point after C_MIXED (40
        # MiB/App) reached the PSI safety gate before Telegram's first block.
        one = ["FILE_COLD"] * 2 + ["FILE_HOT", "ANON_COLD", "ANON_HOT"]
        return {app: list(one) for app in ("FIREFOX", "THUNDERBIRD", "TELEGRAM")}
    raise ValueError(phase)


def app_command(app: str) -> str:
    if app == "FIREFOX":
        binary = ROOT / "tools/firefox/firefox/firefox"
        return f"mkdir -p {shlex.quote(str(Path.home() / 'firefox_profiles/test4b-calibration'))} && env -u WAYLAND_DISPLAY MOZ_ENABLE_WAYLAND=0 GDK_BACKEND=x11 {shlex.quote(str(binary))} --new-instance --no-remote -profile {shlex.quote(str(Path.home() / 'firefox_profiles/test4b-calibration'))} --new-window about:blank"
    if app == "THUNDERBIRD":
        return "thunderbird --new-instance"
    return "telegram-desktop"


def window_action(app: str) -> dict[str, Any]:
    values = {
        "FIREFOX": ("firefox|Firefox", "Mozilla Firefox|about:blank"),
        "THUNDERBIRD": ("thunderbird|Thunderbird", "Thunderbird"),
        "TELEGRAM": ("telegramdesktop|TelegramDesktop|telegram-desktop", "Telegram"),
    }
    cls, title = values[app]
    return {"type": "switch", "name": app.lower(), "app_key": app, "class": cls, "title": title, "optional": True, "label": f"TEST4B1_FOREGROUND_{app}"}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--session-dir", type=Path, required=True)
    parser.add_argument("--phase", choices=["A_FILE", "B_ANON", "C_BASELINE", "C_BASELINE_STABLE", "C_MIXED", "C_MIXED_20"], required=True)
    parser.add_argument("--ballast-bin", type=Path, default=ROOT / "runtime_monitor/tools/parp_memory_ballast")
    args = parser.parse_args()
    if not args.ballast_bin.is_file():
        raise SystemExit(f"missing ballast binary: {args.ballast_bin}")
    session = args.session_dir.resolve(); session.mkdir(parents=True, exist_ok=True)
    configs = session / "configs"; configs.mkdir(exist_ok=True)
    calibration = session / "calibration"; calibration.mkdir(exist_ok=True)
    raw_dir = calibration / "raw_ballast"; raw_dir.mkdir(exist_ok=True)
    files = session / "ballast_files" / session.name; files.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(str(session).encode()).hexdigest()[:16]
    socket_dir = Path(f"/run/user/{os.getuid()}/t4b-cal-{digest}"); socket_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    plan = phase_plan(args.phase)
    prelaunch_all = args.phase.startswith("C_")
    workers: list[dict[str, Any]] = []
    for app, stages in plan.items():
        counts: dict[str, int] = {}
        for stage in stages:
            index = counts.get(stage, 0); counts[stage] = index + 1
            name = f"{app.lower()}-{stage.lower()}-{index:02d}"
            sizes = {"anon_cold_bytes": 0, "anon_hot_bytes": 0, "file_cold_bytes": 0, "file_hot_bytes": 0}
            sizes[{"ANON_COLD": "anon_cold_bytes", "ANON_HOT": "anon_hot_bytes", "FILE_COLD": "file_cold_bytes", "FILE_HOT": "file_hot_bytes"}[stage]] = CHUNK
            workers.append({"worker_id": name, "app_key": app, "stage": f"{stage}_ALLOC", "scope_name": SCOPE[app],
                            "socket_path": str(socket_dir / f"{name}.sock"), "log_path": str(raw_dir / f"{name}.csv"),
                            "file_path": str(files / f"{name}.bin"), **sizes})
    config = {"phase": args.phase, "slice": "test4b-experiment.slice", "chunk_bytes": CHUNK,
              "configured_max_bytes": len(workers) * CHUNK, "system_reserve_bytes": 512 * MIB,
              "cgroup_reserve_bytes": 64 * MIB, "pause_seconds": .75, "psi_full_abort_avg10": .20,
              "app_keys": list(plan), "launch_all_apps_before_calibration": prelaunch_all,
              "startup_stabilization_seconds": 15 if args.phase in {"C_BASELINE_STABLE", "C_MIXED", "C_MIXED_20"} else 0,
              "startup_stable_required_samples": 3,
              "workers": workers, "socket_dir": str(socket_dir)}
    config_path = calibration / "calibration_config.json"; config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    # The cgroup helper only consumes max_global_bytes; retain the full worker
    # manifest separately for the schedule controller.
    # This file is deliberately minimal because the calibration scheduler, not
    # Runtime Monitor's Test4B ballast coordinator, owns allocation.  Keep the
    # worker paths here too so the existing strictly-scoped cleanup helper can
    # remove every generated file/socket after the temporary cgroup is stopped.
    ballast_cleanup = {
        "slice": "test4b-experiment.slice",
        "max_global_bytes": len(workers) * CHUNK,
        "apps": {},
        "cleanup_paths": [
            {"file_path": worker["file_path"], "socket_path": worker["socket_path"]}
            for worker in workers
        ],
    }
    (calibration / "ballast_config.json").write_text(
        json.dumps(ballast_cleanup, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    scope = json.loads((ROOT / "configs/runtime/test1_runtime_app_scope.json").read_text(encoding="utf-8")); scope["slice"] = "test4b-experiment.slice"
    scope_path = configs / "test4b_calibration_scope.json"; scope_path.write_text(json.dumps(scope, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    actions: list[dict[str, Any]] = []
    active: list[str] = []
    for app in plan:
        worker_cmds: list[str] = []
        for worker in [item for item in workers if item["app_key"] == app]:
            worker_cmds.append(" ".join([shlex.quote(str(args.ballast_bin)), "--app-key", app, "--socket", shlex.quote(worker["socket_path"]), "--log", shlex.quote(worker["log_path"]), "--file", shlex.quote(worker["file_path"]), "--anon-cold", str(worker["anon_cold_bytes"]), "--anon-hot", str(worker["anon_hot_bytes"]), "--file-cold", str(worker["file_cold_bytes"]), "--file-hot", str(worker["file_hot_bytes"]), "--hot-interval-ms", "750", "&"]))
        actions += [
            {"type": "shell", "name": app.lower(), "app_key": app, "command": " ".join(worker_cmds) + " exec sh -c " + shlex.quote(app_command(app)), "label": f"TEST4B1_LAUNCH_{app}"},
            {"type": "wait", "seconds": 5, "label": f"TEST4B1_WAIT_{app}"}, window_action(app),
            {"type": "wait", "seconds": 1, "label": f"TEST4B1_FOREGROUND_SETTLE_{app}"},
        ]
        if not prelaunch_all:
            actions.append({"type": "shell", "command": f"python3 {shlex.quote(str(ROOT / 'runtime_monitor/scripts/run_test4b_calibration_schedule.py'))} --session-dir {shlex.quote(str(session))} --app {app}", "label": f"TEST4B1_CALIBRATE_{app}"})
        active.append(app)
    if prelaunch_all:
        for app in plan:
            actions += [window_action(app), {"type": "wait", "seconds": 1, "label": f"TEST4B1_CALIBRATION_FOREGROUND_{app}"},
                        {"type": "shell", "command": f"python3 {shlex.quote(str(ROOT / 'runtime_monitor/scripts/run_test4b_calibration_schedule.py'))} --session-dir {shlex.quote(str(session))} --app {app}", "label": f"TEST4B1_CALIBRATE_{app}"}]
    for app in reversed(active):
        actions.append({"type": "close", "name": app.lower(), "app_key": app, "optional": True, "force_after_seconds": 2, "label": f"TEST4B1_CLOSE_{app}"})
    scenario = {"description": f"Test4B-1 {args.phase}: foreground-only 4MiB ballast calibration; no memory.reclaim.", "validation_mode": False, "actions": actions}
    scenario_path = configs / "test4b_calibration_scenario.json"; scenario_path.write_text(json.dumps(scenario, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"phase": args.phase, "workers": len(workers), "scenario": str(scenario_path), "scope": str(scope_path), "config": str(config_path)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
