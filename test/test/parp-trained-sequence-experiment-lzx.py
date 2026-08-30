#!/usr/bin/env python3
"""Run LSAPP-trained eight-application PARP reclaim experiments.  lzx-note"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import importlib.util
import json
import os
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


TEST_DIR = Path(__file__).resolve().parent
TEST_ROOT = TEST_DIR.parent
REPO_ROOT = TEST_ROOT.parent
CONFIG_DEFAULT = TEST_DIR / "parp-trained-sequence-config-lzx.json"
EVIDENCE = TEST_DIR / "parp-trained-sequence-evidence-lzx.py"
FIXTURE = TEST_DIR / "memory-fixture-lzx.py"
AUTOMATION = TEST_ROOT / "automation/app_automation.py"
PRESSURE = TEST_ROOT / "automation/oom_threshold_pressure_lzx.py"
MIB = 1024 * 1024


def load_acceptance() -> Any:
    """Reuse the established app, cgroup and policy adapters. lzx-note"""
    path = TEST_DIR / "parp-acceptance-lzx.py"
    spec = importlib.util.spec_from_file_location("parp_acceptance_lzx", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


ACCEPT = load_acceptance()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def load_config(path: Path) -> dict[str, Any]:
    value = read_json(path)
    required = {
        "slice", "apps", "hot_apps", "cold_apps", "trained_history",
        "trained_history_vocab", "expected_next_vocab", "pressure_mib",
        "reclaim_target_mib", "initial_memory_max_mib",
    }
    missing = sorted(required - value.keys())
    if missing:
        raise ValueError("trained-sequence config missing: " + ",".join(missing))
    if set(value["hot_apps"]) & set(value["cold_apps"]):
        raise ValueError("hot and cold application sets overlap")
    if set(value["apps"]) != set(value["hot_apps"]) | set(value["cold_apps"]):
        raise ValueError("apps must equal hot_apps union cold_apps")
    if len(value["trained_history"]) != 5 or len(value["trained_history_vocab"]) != 5:
        raise ValueError("v3 checkpoint contract requires exactly five history entries")
    return value


def helper_action(arguments: list[str], label: str) -> dict[str, Any]:
    command = [sys.executable, str(EVIDENCE), *arguments]
    return {"type": "shell", "command": shlex.join(command), "label": label}


def fixture_socket(run_dir: Path, app: str) -> Path:
    token = hashlib.sha256(str(run_dir).encode("utf-8")).hexdigest()[:10]
    slug = app.lower().replace("_", "-")  # lzx-note
    return Path(f"/run/user/{os.getuid()}/parp-trained-{token}-{slug}.sock")


def fixture_action(socket_path: Path, command: str, label: str, timeout: int = 180) -> dict[str, Any]:
    return {
        "type": "shell",
        "command": shlex.join(
            [
                sys.executable, str(TEST_DIR / "parp-acceptance-lzx.py"),
                "fixture-command", "--socket", str(socket_path),
                "--command", command, "--timeout", str(timeout), "--wait", "30",
            ]
        ),
        "label": label,
    }


def app_switch_actions(spec: Any, dwell: float, socket_path: Path, index: int) -> list[dict[str, Any]]:
    return [
        {
            "type": "switch", "name": spec.name, "app_key": spec.key,
            "class": spec.window_class, "title": spec.window_title,
            "label": f"TRAINED_{index:02d}_SWITCH_{spec.key}",
        },
        {
            "type": "verify_foreground", "name": spec.name, "app_key": spec.key,
            "class": spec.window_class, "title": spec.window_title,
            "label": f"TRAINED_{index:02d}_VERIFY_{spec.key}",
        },
        fixture_action(socket_path, "TOUCH_HOT", f"TRAINED_{index:02d}_HOT_{spec.key}"),
        {"type": "wait", "seconds": dwell, "label": f"TRAINED_{index:02d}_DWELL_{spec.key}"},
    ]


def generate_scenario(
    config: dict[str, Any], scenario_name: str, run_dir: Path,
    cgroup_path: Path, seed: int, require_myfs: bool,
) -> dict[str, Any]:
    ACCEPT.write_local_app_fixtures(run_dir)
    specs = ACCEPT.app_specs(run_dir)
    apps = list(config["apps"])
    missing_specs = [app for app in apps if app not in specs]
    if missing_specs:
        raise ValueError("missing app specs: " + ",".join(missing_specs))
    ballast = run_dir / "ballast"
    ballast.mkdir(parents=True, exist_ok=True)
    sockets = {app: fixture_socket(run_dir, app) for app in apps}
    actions: list[dict[str, Any]] = [
        {
            "type": "trace_marker", "event_type": "TRAINED_SCENARIO_START",
            "status": "running", "label": "TRAINED_SCENARIO_START",
            "metadata": {"scenario": scenario_name, "seed": seed},
        }
    ]
    launch_wait: dict[str, list[dict[str, Any]]] = {
        app: ACCEPT.app_launch_actions(specs[app]) for app in apps
    }
    for app in apps:
        actions.append(launch_wait[app][0])
    for app in apps:
        actions.append(launch_wait[app][1])
    if "AUDACITY" in apps:
        # The isolated login-free Audacity profile opens a non-normal welcome
        # dialog.  Runtime Monitor correctly excludes dialogs from foreground
        # history, so close it before the cold-app initialization switch. lzx-note
        audacity = specs["AUDACITY"]
        actions.extend([
            {
                "type": "switch", "name": audacity.name, "app_key": audacity.key,
                "class": audacity.window_class, "title": audacity.window_title,
                "label": "AUDACITY_DISMISS_WELCOME_FOCUS",
            },
            {
                "type": "key", "name": audacity.name, "app_key": audacity.key,
                "key": "Escape", "optional": True,
                "label": "AUDACITY_DISMISS_WELCOME_ESCAPE",
            },
            {"type": "wait", "seconds": 1.0, "label": "AUDACITY_MAIN_WINDOW_SETTLE"},
        ])
    actions.append({
        "type": "wait", "seconds": float(config["startup_settle_seconds"]),
        "label": "TRAINED_APPLICATION_STARTUP_SETTLE",
    })

    file_bytes = int(config["file_mib_per_app"]) * MIB
    hot_bytes = int(config["hot_mib_per_app"]) * MIB
    for app in apps:
        command = [
            sys.executable, str(FIXTURE), "--app", app,
            "--socket", str(sockets[app]),
            "--file", str(ballast / f"{app.lower().replace('_', '-')}.sparse"),  # lzx-note
            "--log", str(ballast / f"{app.lower().replace('_', '-')}-fixture.csv"),  # lzx-note
            "--file-bytes", str(file_bytes), "--anon-bytes", "0",
            "--hot-bytes", str(hot_bytes),
        ]
        actions.append({
            "type": "launch", "name": f"fixture-{app.lower().replace('_', '-')}",  # lzx-note
            "scope_name": f"fixture-{app.lower().replace('_', '-')}", "app_key": app,  # lzx-note
            "command": shlex.join(command), "label": f"FIXTURE_LAUNCH_{app}",
        })
    for app in apps:
        actions.append(fixture_action(sockets[app], "STATUS", f"FIXTURE_READY_{app}"))
    for app in apps:
        actions.append(fixture_action(sockets[app], "PREPARE", f"FIXTURE_PREPARE_{app}", timeout=600))
    # Exercise every cold GUI once before the measured history.  Besides
    # matching the user-facing "used once, then abandoned" scenario, this
    # gives event-only desktop collectors a causal APP_OPEN/APP_SWITCH event.
    # The following five trained switches fully overwrite this warm-up. lzx-note
    for index, app in enumerate(config["cold_apps"], start=1):
        spec = specs[app]
        actions.extend([
            {
                "type": "switch", "name": spec.name, "app_key": spec.key,
                "class": spec.window_class, "title": spec.window_title,
                "label": f"COLD_INITIAL_{index:02d}_SWITCH_{app}",
            },
            {
                "type": "verify_foreground", "name": spec.name, "app_key": spec.key,
                "class": spec.window_class, "title": spec.window_title,
                "label": f"COLD_INITIAL_{index:02d}_VERIFY_{app}",
            },
            {"type": "wait", "seconds": 0.5, "label": f"COLD_INITIAL_{index:02d}_DWELL_{app}"},
        ])
    actions.append({"type": "wait", "seconds": 2.0, "label": "FIXTURE_ALIAS_DISCOVERY"})

    marker = run_dir / "prediction-window-start.json"
    actions.append(helper_action(["mark", "--output", str(marker)], "PREDICTION_WINDOW_MARK"))
    for index, app in enumerate(config["trained_history"], start=1):
        actions.extend(
            app_switch_actions(
                specs[app], float(config["history_dwell_seconds"]), sockets[app], index
            )
        )
    actions.append({
        "type": "wait", "seconds": float(config["prediction_settle_seconds"]),
        "label": "TRAINED_PREDICTION_SETTLE",
    })

    gate = config["prediction_gate"]
    gate_args = [
        "prediction-gate", "--after-mark", str(marker),
        "--output", str(run_dir / "prediction-gate.json"),
        "--history", "|".join(config["trained_history_vocab"]),
        "--opened", "|".join(
            ACCEPT.LSAPP_NAME_BY_APP_KEY[app] for app in apps
        ),
        "--current", str(config["current_vocab"]),
        "--current-key", str(config["current_app"]),
        "--expected-next", "|".join(config["expected_next_vocab"]),
        "--cold", "|".join(
            ACCEPT.LSAPP_NAME_BY_APP_KEY[app] for app in config["cold_apps"]
        ),
        "--minimum-hot-probability", str(gate["minimum_hot_probability"]),
        "--maximum-cold-probability", str(gate["maximum_cold_probability"]),
        "--minimum-bindings", str(gate["minimum_bindings"]),
        "--timeout", str(gate["timeout_seconds"]),
    ]
    gate_args.append("--require-myfs" if require_myfs else "--no-require-myfs")
    actions.append(helper_action(gate_args, "TRAINED_PREDICTION_GATE"))

    app_text = "|".join(apps)
    hot_text = "|".join(config["hot_apps"])
    cold_text = "|".join(config["cold_apps"])
    before_path = run_dir / "snapshot-before-pressure.json"
    pressure_path = run_dir / "snapshot-under-pressure.json"
    actions.append(helper_action([
        "snapshot", "--cgroup", str(cgroup_path), "--ballast", str(ballast),
        "--apps", app_text, "--label", "before_pressure", "--output", str(before_path),
    ], "TRAINED_SNAPSHOT_BEFORE"))
    global_pressure = str(config.get("pressure_mode", "")).startswith("global_")  # lzx-note
    if global_pressure:
        actions.append(helper_action([
            "global-pressure-plan",
            "--target-memfree-mib", str(config["global_target_memfree_mib"]),
            "--reclaim-probe-mib", str(config["global_reclaim_probe_mib"]),
            "--max-allocate-mib", str(config["global_max_allocate_mib"]),
            "--output", str(run_dir / "pressure-boundary.json"),
        ], "TRAINED_SET_GLOBAL_PRESSURE_BOUNDARY"))
    else:
        actions.append(helper_action([
            "set-boundary", "--slice", str(config["slice"]), "--cgroup", str(cgroup_path),
            "--pressure-mib", str(config["pressure_mib"]),
            "--reclaim-mib", str(config["reclaim_target_mib"]),
            "--output", str(run_dir / "pressure-boundary.json"),
        ], "TRAINED_SET_PRESSURE_BOUNDARY"))
    actions.append({
        "type": "trace_marker", "event_type": "TRAINED_PRESSURE_START",
        "status": "running", "label": "TRAINED_PRESSURE_START",
    })
    pressure_state = run_dir / "pressure-state.json"
    pressure_command = [sys.executable, str(PRESSURE)]
    if global_pressure:
        pressure_command.extend([
            "--target-memfree-mib", str(config["global_target_memfree_mib"]),
            "--max-allocate-mib", str(config["global_max_allocate_mib"]),
        ])
    else:
        pressure_command.extend(["--target-mib", str(config["pressure_mib"])])
    pressure_command.extend([
        "--chunk-mib", str(config["pressure_chunk_mib"]),
        "--reclaim-probe-mib", str(config["global_reclaim_probe_mib"] if global_pressure else 0),
        "--ramp-interval", str(config["pressure_ramp_interval_seconds"]),
        "--hold-seconds", str(config["pressure_hold_seconds"]),
        "--oom-score-adj", "1000", "--seed", str(seed), "--state", str(pressure_state),
    ])
    actions.append({
        "type": "launch", "name": "trained-pressure", "scope_name": "trained-pressure",
        "command": shlex.join(pressure_command), "label": "TRAINED_PRESSURE_LAUNCH",
    })
    actions.append({
        "type": "wait_json", "path": str(pressure_state), "field": "status",
        "equals": "HOLDING", "timeout": float(config["pressure_ramp_timeout_seconds"]),
        "poll_seconds": 0.2, "label": "TRAINED_PRESSURE_HOLDING",
    })
    actions.append(helper_action([
        "capture-json", "--input", str(pressure_state),
        "--output", str(run_dir / "pressure-holding-state.json"),
    ], "TRAINED_PRESSURE_HOLDING_EVIDENCE"))  # lzx-note
    actions.append({"type": "wait", "seconds": 1.0, "label": "TRAINED_RECLAIM_SETTLE"})
    actions.append(helper_action([
        "snapshot", "--cgroup", str(cgroup_path), "--ballast", str(ballast),
        "--apps", app_text, "--label", "under_pressure", "--output", str(pressure_path),
    ], "TRAINED_SNAPSHOT_PRESSURE"))
    actions.append(helper_action([
        "compare", "--before", str(before_path), "--after", str(pressure_path),
        "--hot", hot_text, "--cold", cold_text,
        "--output", str(run_dir / "reclaim-source.json"),
    ], "TRAINED_RECLAIM_SOURCE"))

    if scenario_name == "future_reuse":
        target = "FIREFOX"
        spec = specs[target]
        actions.extend([
            {
                "type": "trace_marker", "event_type": "TRAINED_FUTURE_REUSE_START",
                "status": "running", "app_key": target,
                "label": "TRAINED_FUTURE_REUSE_START",
            },
            {
                "type": "switch", "name": spec.name, "app_key": target,
                "class": spec.window_class, "title": spec.window_title,
                "label": "TRAINED_FUTURE_REUSE_SWITCH_FIREFOX",
            },
            {
                "type": "verify_foreground", "name": spec.name, "app_key": target,
                "class": spec.window_class, "title": spec.window_title,
                "label": "TRAINED_FUTURE_REUSE_VERIFY_FIREFOX",
            },
            fixture_action(
                sockets[target],
                f"TOUCH_SAMPLE {int(config['future_reuse_offset_mib']) * MIB} "
                f"{int(config['future_reuse_mib']) * MIB}",
                "TRAINED_FUTURE_REUSE_TOUCH_FIREFOX", timeout=600,
            ),
            helper_action([
                "snapshot", "--cgroup", str(cgroup_path), "--ballast", str(ballast),
                "--apps", app_text, "--label", "after_future_reuse",
                "--output", str(run_dir / "snapshot-after-reuse.json"),
            ], "TRAINED_SNAPSHOT_AFTER_REUSE"),
            {
                "type": "trace_marker", "event_type": "TRAINED_FUTURE_REUSE_DONE",
                "status": "success", "app_key": target,
                "label": "TRAINED_FUTURE_REUSE_DONE",
            },
        ])

    actions.extend([
        {
            "type": "trace_marker", "event_type": "TRAINED_PRESSURE_DONE",
            "status": "success", "label": "TRAINED_PRESSURE_DONE",
        },
        {"type": "close", "name": "trained-pressure", "label": "TRAINED_PRESSURE_STOP"},
    ])
    for app in reversed(apps):
        actions.append(fixture_action(sockets[app], "STOP", f"FIXTURE_STOP_{app}"))
    for app in reversed(apps):
        actions.append(ACCEPT.app_close_action(specs[app]))
    actions.append({
        "type": "trace_marker", "event_type": "TRAINED_SCENARIO_DONE",
        "status": "success", "label": "TRAINED_SCENARIO_DONE",
    })
    return {
        "description": f"LSAPP-trained {scenario_name} reclaim experiment. lzx-note",
        "validation_mode": True,
        "metadata": {
            "schema_version": 1, "scenario": scenario_name, "seed": seed,
            "apps": apps, "hot_apps": config["hot_apps"], "cold_apps": config["cold_apps"],
            "trained_history": config["trained_history"],
            "file_mib_per_app": config["file_mib_per_app"],
            "pressure_mib": config["pressure_mib"],
            "reclaim_target_mib": config["reclaim_target_mib"],
        },
        "actions": actions,
    }


def setup_config(config: dict[str, Any]) -> dict[str, Any]:
    total = int(ACCEPT.meminfo()["MemTotal"])
    maximum = int(total * float(config.get("experiment_memory_max_ratio", 0.95)))  # lzx-note
    return {
        "slice": config["slice"],
        "safety": {
            "memory_high_ratio": 0.90,
            "memory_max_ratio": 0.95,
        },
        "peak": {
            "oom_threshold": {
                "enabled": True,
                "memory_high": "infinity",
                "memory_max_bytes": maximum,
                "memory_swap_max_bytes": 0,
            }
        },
    }


def gui_environment() -> dict[str, str]:
    keys = (
        "DISPLAY", "XAUTHORITY", "DBUS_SESSION_BUS_ADDRESS", "XDG_RUNTIME_DIR",
        "XDG_SESSION_TYPE", "WAYLAND_DISPLAY",
    )
    values = dict(os.environ)
    show = subprocess.run(
        ["systemctl", "--user", "show-environment"],
        text=True, capture_output=True, check=False, timeout=5,
    )
    for line in show.stdout.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            if key in keys and value:
                values[key] = value
    values["GDK_BACKEND"] = "x11"
    values["MOZ_ENABLE_WAYLAND"] = "0"
    values["WAYLAND_DISPLAY"] = ""
    return values


def preflight(config: dict[str, Any], policy: str) -> dict[str, Any]:
    env = gui_environment()
    specs = ACCEPT.app_specs(Path("/tmp/parp-trained-preflight"))
    apps = list(config["apps"])
    executables = {
        app: bool(ACCEPT.command_exists(specs[app].executable) or Path(specs[app].executable).is_file())
        for app in apps
    }
    service = subprocess.run(
        ["systemctl", "--user", "is-active", "parp-runtime-monitor.service"],
        text=True, capture_output=True, check=False, timeout=5,
    ).stdout.strip()
    checks = {
        "sudo_noninteractive": subprocess.run(
            ["sudo", "-n", "true"], check=False,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        ).returncode == 0,
        "runtime_service_active": service == "active",
        "display_available": bool(env.get("DISPLAY") and env.get("XAUTHORITY")),
        "automation_exists": AUTOMATION.is_file(),
        "fixture_exists": FIXTURE.is_file(),
        "pressure_exists": PRESSURE.is_file(),
        "evidence_exists": EVIDENCE.is_file(),
        "all_apps_installed": all(executables.values()),
    }
    if policy == "native_kernel":
        # The pinned upstream tree is installed with a distinguishing
        # LOCALVERSION; Native deliberately has neither myfs nor PARP sysctls.
        # Requiring those PARP-only interfaces made a real Native run
        # impossible even though scenario generation already disables the
        # myfs gate for this policy.  lzx-note
        checks["native_kernel_running"] = os.uname().release in {
            "6.17.13", "6.17.13-native-6.17.13",
        }
    else:
        checks["myfs_available"] = Path("/dev/myfs").exists()
        checks["bin_switch_available"] = Path(
            "/proc/sys/vm/parp_reclaim_bin_enabled"
        ).is_file()
        if policy in {"bin_cold_lstm", "bin_workload_lstm"}:
            checks["cold_pressure_switch_available"] = Path(
                "/proc/sys/vm/parp_reclaim_cold_aggressive_enabled"
            ).is_file()  # lzx-note
        if policy == "bin_workload_lstm":
            checks["workload_reclaim_switch_available"] = Path(
                "/proc/sys/vm/parp_reclaim_workload_enabled"
            ).is_file()  # lzx-note
        checks["parp_kernel_running"] = "parp-lzx" in os.uname().release
    return {
        "status": "READY" if all(checks.values()) else "BLOCKED",
        "checks": checks,
        "executables": executables,
        "kernel_release": os.uname().release,
        "service_state": service,
        "gui": {key: env.get(key, "") for key in ("DISPLAY", "XAUTHORITY", "XDG_SESSION_TYPE")},
    }


def parse_fixture_latency(path: Path) -> dict[str, Any]:
    rows: list[dict[str, str]] = []
    try:
        with path.open(encoding="utf-8", newline="") as stream:
            rows = list(csv.DictReader(stream))
    except (FileNotFoundError, OSError):
        pass
    touches = [row for row in rows if row.get("command") == "TOUCH_SAMPLE" and row.get("status") == "OK"]
    return {
        "count": len(touches),
        "latency_us": int(touches[-1]["latency_us"]) if touches else None,
        "touched_bytes": int(touches[-1]["touched_bytes"]) if touches else None,
    }


def policy_stat_delta(before: str, after: str) -> dict[str, int]:
    def parse(value: str) -> dict[str, int]:
        result: dict[str, int] = {}
        for line in value.splitlines():
            fields = line.split()
            if len(fields) == 2:
                try:
                    result[fields[0]] = int(fields[1])
                except ValueError:
                    continue
        return result
    first, last = parse(before), parse(after)
    return {key: last.get(key, 0) - first.get(key, 0) for key in sorted(set(first) | set(last))}


def run_one(
    config: dict[str, Any], scenario_name: str, policy: str, seed: int,
    run_dir: Path,
) -> dict[str, Any]:
    run_dir.mkdir(parents=True, exist_ok=False)
    pf = preflight(config, policy)
    write_json(run_dir / "preflight.json", pf)
    if pf["status"] != "READY":
        return {"status": "BLOCKED", "preflight": pf, "run_dir": str(run_dir)}
    policy_variant = {"parp_off": "native", "bin_lstm": "bin_apply"}.get(policy)
    original_policy: dict[str, Any] | None = None
    cgroup_path: Path | None = None
    automation: subprocess.Popen[Any] | None = None
    abort_reason = ""
    automation_rc = 1
    trace_instance = f"parp-accept-trained-{os.getpid()}-{seed}"
    trace_stream: subprocess.Popen[Any] | None = None
    trace_output: Any = None
    trace_error: Any = None
    policy_before: dict[str, Any] = {}
    policy_after: dict[str, Any] = {}
    monitor_rows: list[dict[str, Any]] = []
    try:
        if policy_variant:
            original_policy = ACCEPT.apply_global_policy(policy_variant)
        cgroup_path = ACCEPT.setup_slice(
            setup_config(config), policy_variant or "native"
        )
        policy_before = ACCEPT.policy_state(cgroup_path)
        write_json(run_dir / "policy-before.json", policy_before)
        scenario = generate_scenario(
            config, scenario_name, run_dir, cgroup_path, seed,
            require_myfs=policy != "native_kernel",
        )
        scenario_path = run_dir / "scenario.json"
        write_json(scenario_path, scenario)
        setup = ACCEPT.run([
            "sudo", "-n", "bash", str(ACCEPT.TRACE_HELPER), "setup", trace_instance,
            str(int(config["safety"]["trace_buffer_kb_per_cpu"])),
        ], timeout=30)
        if setup.returncode != 0:
            raise RuntimeError(setup.stderr.strip() or "trace setup failed")
        trace_output = (run_dir / "trace.txt").open("w", encoding="utf-8")
        trace_error = (run_dir / "trace-error.txt").open("w", encoding="utf-8")
        trace_stream = subprocess.Popen(
            ["sudo", "-n", "bash", str(ACCEPT.TRACE_HELPER), "stream", trace_instance],
            stdout=trace_output, stderr=trace_error, text=True,
        )
        enabled = ACCEPT.run([
            "sudo", "-n", "bash", str(ACCEPT.TRACE_HELPER), "enable", trace_instance,
        ], timeout=15)
        if enabled.returncode != 0:
            raise RuntimeError(enabled.stderr.strip() or "trace enable failed")
        env = gui_environment()
        command = [
            sys.executable, str(AUTOMATION), str(scenario_path),
            "--display", env["DISPLAY"], "--xauthority", env["XAUTHORITY"],
            "--trace-output", str(run_dir / "automation-trace.csv"),
            "--session-id", run_dir.name, "--scenario-id", f"trained_{scenario_name}",
            "--test-slice", str(config["slice"]),
            "--screenshot-output-dir", str(run_dir / "screenshots"),
        ]
        log_stream = (run_dir / "automation.log").open("w", encoding="utf-8")
        automation = subprocess.Popen(
            command, stdout=log_stream, stderr=subprocess.STDOUT, text=True,
            env=env, start_new_session=True,
        )
        root_oom = ACCEPT.vmstat().get("oom_kill", 0)
        low_count = 0
        psi_count = 0
        started = time.monotonic()
        safety = config["safety"]
        while automation.poll() is None:
            snap = ACCEPT.snapshot(cgroup_path)
            monitor_rows.append(snap)
            low = snap["memavailable"] < int(safety["min_memavailable_mib"]) * MIB
            low_count = low_count + 1 if low else 0
            high_psi = float(snap["psi"].get("full_avg10", 0)) > float(safety["psi_full_avg10_abort"])
            psi_count = psi_count + 1 if high_psi and low else 0
            if snap["vmstat"].get("oom_kill", 0) > root_oom:
                abort_reason = "HOST_OOM_KILL_INCREMENT"
            elif low_count >= int(safety["abort_consecutive_samples"]):
                abort_reason = "MEMAVAILABLE_HARD_FLOOR"
            elif psi_count >= int(safety["abort_consecutive_samples"]):
                abort_reason = "PSI_FULL_HARD_LIMIT"
            elif time.monotonic() - started > float(safety["max_round_seconds"]):
                abort_reason = "ROUND_TIMEOUT"
            if abort_reason:
                os.killpg(automation.pid, signal.SIGTERM)
                break
            time.sleep(float(safety["sample_interval_seconds"]))
        try:
            automation_rc = automation.wait(timeout=45)
        except subprocess.TimeoutExpired:
            os.killpg(automation.pid, signal.SIGKILL)
            automation_rc = automation.wait(timeout=10)
        log_stream.close()
        policy_after = ACCEPT.policy_state(cgroup_path)
        write_json(run_dir / "policy-after.json", policy_after)
    except Exception as exc:
        abort_reason = abort_reason or f"HARNESS_ERROR:{type(exc).__name__}:{exc}"
    finally:
        if automation is not None and automation.poll() is None:
            try:
                os.killpg(automation.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        try:
            ACCEPT.run(["sudo", "-n", "bash", str(ACCEPT.TRACE_HELPER), "disable", trace_instance], timeout=15)
            ACCEPT.run(["sudo", "-n", "bash", str(ACCEPT.TRACE_HELPER), "stop-stream", trace_instance], timeout=15)
            if trace_stream is not None:
                try:
                    trace_stream.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    trace_stream.kill()
            ACCEPT.trace_stats(trace_instance, run_dir / "trace-stats.txt")
            ACCEPT.run(["sudo", "-n", "bash", str(ACCEPT.TRACE_HELPER), "cleanup", trace_instance], timeout=30)
        finally:
            if trace_output is not None:
                trace_output.close()
            if trace_error is not None:
                trace_error.close()
            ACCEPT.cleanup_slice(setup_config(config))
            if original_policy is not None:
                ACCEPT.restore_global_policy(original_policy)
    write_json(run_dir / "monitor.json", monitor_rows)

    reasons: list[str] = []
    gate: dict[str, Any] = {}
    source: dict[str, Any] = {}
    pressure: dict[str, Any] = {}
    for path, label in (
        (run_dir / "prediction-gate.json", "prediction gate"),
        (run_dir / "reclaim-source.json", "reclaim source"),
        (run_dir / "pressure-holding-state.json", "pressure state"),  # lzx-note
    ):
        try:
            value = read_json(path)
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            reasons.append(f"{label} missing")
            value = {}
        if label == "prediction gate":
            gate = value
            if not value.get("valid"):
                reasons.extend(str(item) for item in value.get("reasons", ["prediction gate invalid"]))
        elif label == "reclaim source":
            source = value
            if not value.get("valid"):
                reasons.append("no measurable file-residency reclaim")
        else:
            pressure = value
            if value.get("status") != "HOLDING":
                reasons.append(f"pressure status={value.get('status')}")
    if automation_rc != 0:
        reasons.append(f"automation returned {automation_rc}")
    if abort_reason:
        reasons.append(abort_reason)
    future_reuse = parse_fixture_latency(run_dir / "ballast/firefox-fixture.csv")
    if scenario_name == "future_reuse" and future_reuse["count"] != 1:
        reasons.append(f"future reuse samples={future_reuse['count']}, expected 1")
    bin_delta = policy_stat_delta(
        str(policy_before.get("reclaim_bin_stats", "")),
        str(policy_after.get("reclaim_bin_stats", "")),
    )
    result = {
        "status": "VALID" if not reasons else "INVALID",
        "valid": not reasons,
        "invalid_reasons": list(dict.fromkeys(reasons)),
        "scenario": scenario_name,
        "policy": policy,
        "seed": seed,
        "kernel_release": os.uname().release,
        "run_dir": str(run_dir),
        "automation_rc": automation_rc,
        "prediction_gate": gate,
        "pressure": pressure,
        "reclaim_source": source.get("source_distribution", {}),
        "parent_deltas": source.get("parent_deltas", {}),
        "per_app": source.get("apps", {}),
        "future_reuse": future_reuse,
        "reclaim_bin_delta": bin_delta,
        "monitor": {
            "samples": len(monitor_rows),
            "min_memavailable_bytes": min((row["memavailable"] for row in monitor_rows), default=None),
            "max_psi_full_avg10": max((float(row["psi"].get("full_avg10", 0)) for row in monitor_rows), default=None),
        },
    }
    write_json(run_dir / "run-result.json", result)
    return result


def command_run(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    scenarios = list(config["scenarios"]) if args.scenario == "all" else [args.scenario]
    root = Path(config["output_root"])
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    session = root / f"{args.policy}-{stamp}-{os.uname().release}"
    session.mkdir(parents=True, exist_ok=False)
    write_json(session / "config.json", config)
    results: list[dict[str, Any]] = []
    for round_index in range(1, args.rounds + 1):
        for scenario_name in scenarios:
            seed = args.seed + round_index - 1
            run_dir = session / f"round-{round_index:02d}-{scenario_name}"
            print(
                f"run policy={args.policy} scenario={scenario_name} round={round_index}/{args.rounds} seed={seed}",
                flush=True,
            )
            result = run_one(config, scenario_name, args.policy, seed, run_dir)
            results.append(result)
            print(f"status={result['status']} output={run_dir}", flush=True)
            if not result.get("valid") and not args.keep_going:
                break
        if results and not results[-1].get("valid") and not args.keep_going:
            break
    summary = {
        "schema_version": 1,
        "status": "COMPLETE" if all(item.get("valid") for item in results) else "INCOMPLETE",
        "policy": args.policy,
        "kernel_release": os.uname().release,
        "rounds_requested": args.rounds,
        "scenarios_requested": scenarios,
        "runs": results,
    }
    write_json(session / "summary.json", summary)
    print(session)
    return 0 if summary["status"] == "COMPLETE" else 1


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser()
    sub = root.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("--config", type=Path, default=CONFIG_DEFAULT)
    run.add_argument(
        "--policy", choices=("parp_off", "bin_lstm", "native_kernel"), required=True
    )
    run.add_argument(
        "--scenario", choices=("all", "cold_retire", "future_reuse", "source_distribution"),
        default="all",
    )
    run.add_argument("--rounds", type=int, default=1)
    run.add_argument("--seed", type=int, default=20260828)
    run.add_argument("--keep-going", action="store_true")
    run.set_defaults(func=command_run)
    return root


def main() -> int:
    args = parser().parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
