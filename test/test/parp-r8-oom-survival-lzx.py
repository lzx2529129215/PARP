#!/usr/bin/env python3
"""Isolated implementation of the R8 multi-application OOM survival test.

This module is deliberately imported by ``parp-real-pc-experiment-lzx.py``.
Keeping the R8 paths here prevents the R1--R7 no-OOM/reclaim contracts from
silently acquiring a browser allocator or a memcg-OOM success condition.
"""

from __future__ import annotations

import copy
import dataclasses
import hashlib
import json
import math
import os
import re
import shlex
import signal
import shutil
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


MIB = 1024 * 1024
SCENARIO = "r8_multi_app_oom_survival"
HEAVY_APPS = frozenset({"FIREFOX", "THUNDERBIRD", "GIMP", "LIBREOFFICE", "AUDACITY"})
MEDIUM_APPS = frozenset({"VLC", "EVINCE", "IMAGE_VIEWER", "RHYTHMBOX", "SHOTWELL", "FILES"})
LIGHT_APPS = frozenset({"CALCULATOR", "CALENDAR", "SYSTEM_MONITOR", "SOLITAIRE"})
ALL_R8_APPS = HEAVY_APPS | MEDIUM_APPS | LIGHT_APPS

TRAINED: Any = None
ACCEPT: Any = None
RUNNER: Path | None = None
TEST_DIR = Path(__file__).resolve().parent
TEST_ROOT = TEST_DIR.parent
AUTOMATION = TEST_ROOT / "automation" / "app_automation.py"
ASSET_BUILDER = TEST_ROOT / "automation" / "create_real_pc_assets_lzx.py"
EVIDENCE = TEST_DIR / "parp-trained-sequence-evidence-lzx.py"


def bind(trained: Any, accept: Any, runner: Path) -> None:
    """Inject adapters owned by the existing real-PC runner."""
    global TRAINED, ACCEPT, RUNNER
    TRAINED, ACCEPT, RUNNER = trained, accept, runner


def _need_bound() -> None:
    if TRAINED is None or ACCEPT is None or RUNNER is None:
        raise RuntimeError("R8 adapter was not bound by the real-PC runner")


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def read_int(path: Path) -> int | None:
    try:
        return int(path.read_text(encoding="ascii").strip())
    except (FileNotFoundError, PermissionError, OSError, ValueError):
        return None


def read_kv(path: Path) -> dict[str, int]:
    result: dict[str, int] = {}
    try:
        for line in path.read_text(encoding="ascii").splitlines():
            key, value = line.split(maxsplit=1)
            result[key] = int(value)
    except (FileNotFoundError, PermissionError, OSError, ValueError):
        pass
    return result


def canonical_sha256(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def memtotal_mib() -> int:
    _need_bound()
    return int(ACCEPT.meminfo()["MemTotal"]) // MIB


def memory_limit_from_p95(p95_bytes: int) -> int:
    """R8's frozen MemoryMax formula, rounded upward to 128 MiB."""
    raw = max(int(p95_bytes) + 1024 * MIB, math.ceil(int(p95_bytes) * 1.10))
    unit = 128 * MIB
    return ((raw + unit - 1) // unit) * unit


def memory_limit_cap_bytes(total_mib: int) -> int:
    cap_mib = min(10240, total_mib - 4096)
    return max(0, (cap_mib // 128) * 128) * MIB


def _tiers(config: dict[str, Any]) -> dict[str, int]:
    settings = config["r8_oom"]
    tiers = settings.get("working_set_minimum_mib", {})
    return {
        "heavy": int(tiers.get("heavy", 128)),
        "medium": int(tiers.get("medium", 32)),
        "light": int(tiers.get("light", 16)),
    }


def app_minimum_mib(config: dict[str, Any], app: str) -> int:
    tiers = _tiers(config)
    if app in HEAVY_APPS:
        return tiers["heavy"]
    if app in MEDIUM_APPS:
        return tiers["medium"]
    if app in LIGHT_APPS:
        return tiers["light"]
    raise ValueError(f"R8 has no working-set tier for {app}")


def frozen_config_contract(config: dict[str, Any]) -> dict[str, Any]:
    copy_value = copy.deepcopy(config)
    copy_value.get("r8_oom", {}).get("calibration", {}).pop("frozen_config_sha256", None)
    return copy_value


def validate_config(config: dict[str, Any], *, require_frozen: bool = False) -> None:
    required = {
        "output_root", "asset_root", "slice", "apps", "hot_apps", "cold_apps",
        "trained_history", "trained_history_vocab", "expected_next_vocab", "scenarios",
        "safety", "pressure_mode", "r8_oom", "prediction_gate",
    }
    missing = sorted(required - set(config))
    if missing:
        raise ValueError("R8 config missing: " + ",".join(missing))
    if list(config["scenarios"]) != [SCENARIO]:
        raise ValueError("R8 config must contain only r8_multi_app_oom_survival")
    apps = list(config["apps"])
    if len(apps) != 15 or set(apps) != ALL_R8_APPS:
        raise ValueError("R8 requires the exact 15 LSAPP GUI applications")
    if set(config["hot_apps"]) | set(config["cold_apps"]) != set(apps):
        raise ValueError("R8 hot/cold sets must cover every application")
    if set(config["hot_apps"]) & set(config["cold_apps"]):
        raise ValueError("R8 hot/cold application sets overlap")
    if len(config["trained_history"]) != 5 or len(config["trained_history_vocab"]) != 5:
        raise ValueError("R8 requires the five-event LSTM history contract")
    if config.get("pressure_mode") != "firefox_arraybuffer_oom_burst":
        raise ValueError("R8 pressure must be Firefox ArrayBuffer only")
    r8 = config["r8_oom"]
    if r8.get("aggressor_app") != "FIREFOX":
        raise ValueError("R8 Firefox must be the only aggressor")
    victims = list(r8.get("victim_apps", []))
    if set(victims) != set(apps) - {"FIREFOX"} or len(victims) != 14:
        raise ValueError("R8 must declare exactly the other 14 applications as victims")
    if int(r8.get("pressure_chunk_mib", 0)) != 64:
        raise ValueError("R8 Firefox pressure chunk must be exactly 64 MiB")
    if int(r8.get("memory_swap_max_mib", 0)) != 1024:
        raise ValueError("R8 MemorySwapMax must be 1024 MiB")
    if str(r8.get("memory_high", "")) != "infinity":
        raise ValueError("R8 MemoryHigh must remain infinity")
    if int(r8.get("victim_oom_score_adj", -1)) != 500 or int(r8.get("aggressor_oom_score_adj", -1)) != 0:
        raise ValueError("R8 OOM score contract is Firefox=0 and victims=500")
    if not bool(r8.get("memory_oom_group", False)):
        raise ValueError("R8 requires MemoryOOMGroup=yes per application scope")
    tiers = _tiers(config)
    if tiers["heavy"] < 128 or tiers["medium"] < 32 or tiers["light"] < 16:
        raise ValueError("R8 working-set thresholds may not be lowered")
    if int(r8.get("minimum_total_working_set_mib", 0)) < 1536:
        raise ValueError("R8 aggregate working-set threshold must be at least 1536 MiB")
    calibration = r8.get("calibration", {})
    if int(calibration.get("baseline_rounds", 0)) != 3 or int(calibration.get("candidate_rounds", 0)) != 5:
        raise ValueError("R8 calibration requires three baseline and five candidate rounds")
    if int(calibration.get("burst_start_mib", 0)) != 512 or int(calibration.get("burst_step_mib", 0)) != 128:
        raise ValueError("R8 burst search must start at 512 MiB and step by 128 MiB")
    if int(calibration.get("minimum_in_range_rounds", 0)) < 4:
        raise ValueError("R8 calibration requires at least four in-range OOM rounds")
    if require_frozen:
        if not calibration.get("frozen"):
            raise ValueError("formal R8 runs require a frozen native calibration config")
        if int(r8.get("memory_max_mib", 0)) <= 0 or int(r8.get("burst_mib", 0)) <= 0:
            raise ValueError("frozen R8 config lacks MemoryMax or Firefox burst")
        if int(r8["burst_mib"]) % 64:
            raise ValueError("frozen R8 Firefox burst must be divisible by 64 MiB")
        configured_hash = str(calibration.get("frozen_config_sha256", ""))
        if not configured_hash or configured_hash != canonical_sha256(frozen_config_contract(config)):
            raise ValueError("frozen R8 calibration config hash mismatch")


def _asset_copy(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    target.unlink(missing_ok=True)
    try:
        os.link(source, target)
    except OSError:
        shutil.copy2(source, target)


def prepare_assets(config: dict[str, Any], run_dir: Path) -> dict[str, Any]:
    _need_bound()
    root = Path(config["asset_root"])
    subprocess.run(
        [sys.executable, str(ASSET_BUILDER), "--profile", "r8", "--output", str(root)],
        check=True, timeout=900,
    )
    ACCEPT.write_local_app_fixtures(run_dir)
    # Epiphany private instances require distinct profiles.  R8 keeps the
    # resident workset window and the allocator page in the same Firefox
    # application scope, but in separate browser instances, so pressure never
    # depends on unreliable address-bar automation under Wayland/Xwayland.
    (run_dir / "firefox-pressure-profile").mkdir(parents=True, exist_ok=True)
    fixture = run_dir / "fixtures"
    names = [
        "local-page.html", "oom-pressure.html", "writer-test.odt", "mail-test.eml",
        "audio-test.wav", "document-test.pdf", "rhythmdb-r8.xml",
        *(f"image-test-{index:02d}.png" for index in range(1, 9)),
    ]
    for name in names:
        # The ODT is intentionally copied: LibreOffice saves its per-round edit.
        if name == "writer-test.odt":
            shutil.copy2(root / name, fixture / name)
        else:
            _asset_copy(root / name, fixture / name)
    destination = fixture / "files-workload"
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(root / "files-workload", destination)
    return read_json(root / "manifest.json")


def _scope_path(cgroup: Path, app: str) -> Path | None:
    slug = app.lower().replace("_", "-")
    direct = cgroup / f"automation-{slug}.scope"
    if direct.is_dir():
        return direct
    try:
        matches = [path for path in cgroup.rglob(f"automation-{slug}.scope") if path.is_dir()]
    except (FileNotFoundError, PermissionError, OSError):
        # Expected group kills and final slice cleanup can remove a scope while
        # the out-of-slice watcher is taking its last attribution sample.
        return None
    return matches[0] if len(matches) == 1 else None


def _scope_row(path: Path | None) -> dict[str, Any]:
    if path is None or not path.exists():
        return {"valid": False, "reason": "scope missing", "pids": []}
    pids = []
    try:
        pids = [int(item) for item in (path / "cgroup.procs").read_text(encoding="ascii").split()]
    except (FileNotFoundError, PermissionError, OSError, ValueError):
        pass
    processes: list[dict[str, Any]] = []
    for pid in pids:
        proc = Path("/proc") / str(pid)

        def proc_text(name: str) -> str:
            # A scope can disappear between reading cgroup.procs and walking
            # /proc during expected OOM/cleanup.  Treat that as a dead process
            # instead of invalidating the harness itself.
            try:
                return (proc / name).read_text(encoding="utf-8", errors="replace")
            except (FileNotFoundError, PermissionError, OSError):
                return ""

        processes.append({
            "pid": pid,
            "comm": proc_text("comm").strip(),
            "oom_score_adj": read_int(proc / "oom_score_adj"),
            "cgroup": proc_text("cgroup"),
        })
    try:
        stat = path.stat()
    except OSError as exc:
        return {"valid": False, "reason": str(exc), "pids": pids}
    return {
        "valid": True, "path": str(path), "device": stat.st_dev, "inode": stat.st_ino,
        "memory_current": read_int(path / "memory.current"),
        "memory_peak": read_int(path / "memory.peak"),
        "memory_stat": read_kv(path / "memory.stat"),
        "memory_events": read_kv(path / "memory.events"),
        "memory_events_local": read_kv(path / "memory.events.local"),
        "memory_oom_group": read_int(path / "memory.oom.group"),
        "pids": pids, "processes": processes,
    }


def _window_ids(window_class: str) -> list[str]:
    if not shutil.which("xdotool"):
        return []
    result = subprocess.run(
        ["xdotool", "search", "--onlyvisible", "--class", window_class],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False,
    )
    return [line for line in result.stdout.splitlines() if line.isdigit()]


def snapshot(cgroup: Path, apps: list[str], specs: dict[str, Any], label: str) -> dict[str, Any]:
    rows: dict[str, Any] = {}
    for app in apps:
        row = _scope_row(_scope_path(cgroup, app))
        row["window_ids"] = _window_ids(specs[app].window_class)
        row["window_alive"] = bool(row["window_ids"])
        row["scope_alive"] = bool(row.get("pids"))
        rows[app] = row
    return {
        "schema_version": 1, "label": label, "timestamp_ns": time.time_ns(),
        "cgroup": _scope_row(cgroup), "apps": rows,
    }


def _r8_specs(run_dir: Path, pressure_mib: int, seed: int) -> dict[str, Any]:
    _need_bound()
    specs = ACCEPT.app_specs(run_dir)
    fixture = run_dir / "fixtures"
    pressure_uri = (
        (fixture / "oom-pressure.html").as_uri()
        + f"?mib={max(512, pressure_mib)}&seed={seed}"
    )
    browser_environment = [
        "env", "-u", "http_proxy", "-u", "https_proxy",
        "-u", "HTTP_PROXY", "-u", "HTTPS_PROXY",
    ]
    pressure_command = [
        *browser_environment, "epiphany-browser", "--private-instance",
        f"--profile={run_dir / 'firefox-pressure-profile'}",
        pressure_uri,
    ]
    workset_command = [
        *browser_environment, "epiphany-browser", "--private-instance",
        f"--profile={run_dir / 'firefox-profile'}",
        (fixture / "local-page.html").as_uri(),
    ]
    dual_browser_script = (
        f"{shlex.join(pressure_command)} & exec {shlex.join(workset_command)}"
    )
    specs["FIREFOX"] = dataclasses.replace(
        specs["FIREFOX"],
        command=shlex.join(["/bin/sh", "-c", dual_browser_script]),
    )
    files = fixture / "files-workload"
    file_manager = "nautilus" if ACCEPT.command_exists("nautilus") else "pcmanfm"
    files_command = (
        f"nautilus --new-window {shlex.quote(str(files))}"
        if file_manager == "nautilus" else f"pcmanfm --new-win {shlex.quote(str(files))}"
    )
    specs["FILES"] = dataclasses.replace(specs["FILES"], command=files_command)
    image_paths = " ".join(
        shlex.quote(str(fixture / f"image-test-{index:02d}.png"))
        for index in range(1, 9)
    )
    specs["IMAGE_VIEWER"] = dataclasses.replace(
        specs["IMAGE_VIEWER"],
        command=f"env GDK_BACKEND=x11 eog --new-instance {image_paths}",
    )
    specs["VLC"] = dataclasses.replace(
        specs["VLC"],
        command=(
            "vlc --no-one-instance --no-video-title-show --no-qt-privacy-ask "
            "--no-metadata-network-access --file-caching=60000 "
            f"{shlex.quote(str(fixture / 'audio-test.wav'))}"
        ),
    )
    specs["RHYTHMBOX"] = dataclasses.replace(
        specs["RHYTHMBOX"],
        command=(
            "env GDK_BACKEND=x11 rhythmbox --no-registration "
            f"--rhythmdb-file={shlex.quote(str(fixture / 'rhythmdb-r8.xml'))}"
        ),
    )
    return specs


def _score_wrapped(command: str, score: int) -> str:
    _need_bound()
    return shlex.join([
        sys.executable, str(RUNNER), "oom-score-exec", "--score", str(score), "--",
        *shlex.split(command),
    ])


def _switch(spec: Any, label: str) -> list[dict[str, Any]]:
    window_contract = {
        "name": spec.name,
        "app_key": spec.key,
        "class": spec.window_class,
        "title": spec.window_title,
    }
    if spec.key == "FIREFOX":
        # WebKit/Epiphany creates small transient helper windows.  They must
        # never satisfy the Firefox foreground contract used to build the
        # deterministic LSTM history.  R8 also runs its workset and pressure
        # pages as distinct private browser instances in one application
        # scope; filter by profile so title/class enumeration order cannot
        # direct workset input to the allocator page (or vice versa).
        window_contract.update({
            "minimum_foreground_width": 700,
            "minimum_foreground_height": 500,
            "dismiss_small_transient": True,
            "pid_cmdline_contains": (
                "firefox-pressure-profile"
                if "PARP R8" in spec.window_title else "/firefox-profile"
            ),
        })
    return [
        {"type": "switch", **window_contract, "label": f"{label}_SWITCH_{spec.key}"},
        {"type": "verify_foreground", **window_contract, "label": f"{label}_VERIFY_{spec.key}"},
    ]


def _prepare_steps(app: str, run_dir: Path) -> list[dict[str, Any]]:
    fixture = run_dir / "fixtures"
    steps: dict[str, list[dict[str, Any]]] = {
        "FIREFOX": [
            {"type": "key", "key": "Home"}, {"type": "key", "key": "Page_Down", "repeat": 36, "interval": 0.03},
            {"type": "key", "key": "Home"},
        ],
        "THUNDERBIRD": [
            {"type": "key", "key": "Home"}, {"type": "key", "key": "Page_Down", "repeat": 48, "interval": 0.03},
            {"type": "key", "key": "ctrl+f"}, {"type": "type", "text": "Thread message 720"},
            {"type": "key", "key": "Return"}, {"type": "key", "key": "Escape"},
        ],
        "GIMP": [
            *({"type": "open_file", "path": str(fixture / f"image-test-{index:02d}.png"), "wait_after": 2.0}
              for index in range(2, 7)),
            *(action for _ in range(6) for action in (
                {"type": "key", "key": "slash"}, {"type": "type", "text": "Invert"},
                {"type": "key", "key": "Return"}, {"type": "wait", "seconds": 0.5},
                {"type": "key", "key": "ctrl+Page_Up"},
            )),
        ],
        "LIBREOFFICE": [
            {"type": "key", "key": "ctrl+End"}, {"type": "key", "key": "Page_Up", "repeat": 48, "interval": 0.02},
            {"type": "key", "key": "ctrl+End"},
            {"type": "paste_text", "text": "\nPARP R8 fixed native working-set paragraph.", "repeat": 256},
            {"type": "key", "key": "ctrl+s"},
        ],
        "AUDACITY": [
            {"type": "key", "key": "Escape", "optional": True}, {"type": "key", "key": "ctrl+a"},
            {"type": "hotkey", "key": "ctrl+d"},
            *(
                {"type": "open_file", "shortcut": "ctrl+shift+i", "path": str(fixture / "audio-test.wav"), "wait_after": 2.0}
                for _ in range(3)
            ),
            {"type": "key", "key": "ctrl+a"}, {"type": "key", "key": "ctrl+f"},
            {"type": "key", "key": "plus", "repeat": 6},
        ],
        "VLC": [
            {"type": "key", "key": "alt+F10"},
            {"type": "key", "key": "space"}, {"type": "key", "key": "ctrl+Right", "repeat": 12, "interval": 0.08},
            {"type": "key", "key": "space"},
        ],
        "EVINCE": [
            {"type": "key", "key": "Home"}, {"type": "key", "key": "Page_Down", "repeat": 239, "interval": 0.01},
            {"type": "key", "key": "Home"},
        ],
        "IMAGE_VIEWER": [
            {"type": "key", "key": "Right", "repeat": 8, "interval": 0.15},
            {"type": "key", "key": "1"}, {"type": "key", "key": "plus", "repeat": 8},
            {"type": "key", "key": "minus", "repeat": 3},
        ],
        "SHOTWELL": [
            {"type": "key", "key": "plus", "repeat": 8}, {"type": "key", "key": "minus", "repeat": 3},
        ],
        "RHYTHMBOX": [
            {"type": "key", "key": "Home"}, {"type": "key", "key": "Page_Down", "repeat": 24, "interval": 0.03},
            {"type": "key", "key": "space"}, {"type": "wait", "seconds": 2.0}, {"type": "key", "key": "space"},
        ],
        "FILES": [
            {"type": "key", "key": "Home"}, {"type": "key", "key": "Page_Down", "repeat": 96, "interval": 0.02},
            {"type": "key", "key": "End"}, {"type": "key", "key": "Home"},
        ],
        "CALCULATOR": [
            {"type": "key", "key": "alt+F10"},
            {"type": "type", "text": "".join(f"{index}+{index}=" for index in range(1, 257)), "delay_ms": 2},
            {"type": "key", "key": "Return"},
        ],
        "CALENDAR": [{"type": "key", "key": "Right", "repeat": 36, "interval": 0.04}],
        "SYSTEM_MONITOR": [{"type": "key", "key": "ctrl+2"}, {"type": "wait", "seconds": 5.0}],
        "SOLITAIRE": [{"type": "key", "key": "F2", "repeat": 8, "interval": 0.15}],
    }
    return steps[app]


def _runner_action(arguments: list[str], label: str) -> dict[str, Any]:
    _need_bound()
    return {"type": "shell", "command": shlex.join([sys.executable, str(RUNNER), *arguments]), "label": label}


def _evidence_action(arguments: list[str], label: str) -> dict[str, Any]:
    return {"type": "shell", "command": shlex.join([sys.executable, str(EVIDENCE), *arguments]), "label": label}


def generate_scenario(
    config: dict[str, Any], run_dir: Path, cgroup: Path, seed: int, policy: str,
    *, burst_mib: int, baseline_only: bool,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build the R8-only GUI scenario and its static action-plan contract."""
    _need_bound()
    apps = list(config["apps"])
    r8 = config["r8_oom"]
    specs = _r8_specs(run_dir, burst_mib, seed)
    actions: list[dict[str, Any]] = [{
        "type": "trace_marker", "event_type": "R8_START", "status": "running",
        "label": "R8_START", "metadata": {"scenario": SCENARIO, "seed": seed},
    }]
    for app in apps:
        launch, wait = ACCEPT.app_launch_actions(specs[app])
        launch = dict(launch)
        score = int(r8["aggressor_oom_score_adj"] if app == "FIREFOX" else r8["victim_oom_score_adj"])
        launch["command"] = _score_wrapped(str(launch["command"]), score)
        launch["scope_properties"] = {"MemoryOOMGroup": "yes"}
        launch["label"] = f"R8_LAUNCH_{app}"
        wait = dict(wait)
        wait["label"] = f"R8_WAIT_{app}"
        actions.extend((launch, wait))
    actions.append({"type": "wait", "seconds": float(r8["startup_settle_seconds"]), "label": "R8_STARTUP_SETTLE"})
    for index, app in enumerate(apps, start=1):
        label = f"R8_WORKSET_{index:02d}"
        actions.extend(_switch(specs[app], label))
        for action_index, template in enumerate(_prepare_steps(app, run_dir), start=1):
            action = dict(template)
            action.update({
                "name": specs[app].name, "app_key": app, "class": specs[app].window_class,
                "title": specs[app].window_title, "label": f"{label}_ACTION_{action_index:02d}_{app}",
                "metadata": {"working_set_origin": "application_ui", "phase": "r8_prepare", "app": app},
            })
            actions.append(action)
    actions.append({"type": "wait", "seconds": float(r8["post_workset_settle_seconds"]), "label": "R8_WORKSET_SETTLE"})

    # Publish exactly the same LSTM history on both kernels after all native
    # content is resident.  The R8 action-plan deliberately locks this order.
    marker = run_dir / "r8-prediction-mark.json"
    actions.append(_evidence_action(["mark", "--output", str(marker)], "R8_PREDICTION_MARK"))
    for index, app in enumerate(config["trained_history"], start=1):
        actions.extend(_switch(specs[app], f"R8_TRAINED_{index:02d}"))
        actions.append({"type": "wait", "seconds": float(config["history_dwell_seconds"]), "label": f"R8_TRAINED_{index:02d}_DWELL"})
    actions.append(_runner_action([
        "r8-enforce-oom-scores", "--config", str(run_dir / "r8-config.json"),
        "--cgroup", str(cgroup), "--apps", "|".join(apps),
        "--output", str(run_dir / "r8-oom-score-gate.json"),
    ], "R8_OOM_SCORE_GATE"))
    gate = config["prediction_gate"]
    gate_args = [
        "prediction-gate", "--after-mark", str(marker), "--output", str(run_dir / "prediction-gate.json"),
        "--history", "|".join(config["trained_history_vocab"]),
        "--opened", "|".join(ACCEPT.LSAPP_NAME_BY_APP_KEY[app] for app in apps),
        "--current", str(config["current_vocab"]), "--current-key", str(config["current_app"]),
        "--expected-next", "|".join(config["expected_next_vocab"]),
        "--cold", "|".join(ACCEPT.LSAPP_NAME_BY_APP_KEY[app] for app in config["cold_apps"]),
        "--minimum-hot-probability", str(gate["minimum_hot_probability"]),
        "--maximum-cold-probability", str(gate["maximum_cold_probability"]),
        "--minimum-bindings", str(gate["minimum_bindings"]), "--minimum-myfs-abi", "2",
        "--timeout", str(gate["timeout_seconds"]),
        "--require-myfs" if policy == "bin_lstm" else "--no-require-myfs",
    ]
    actions.append(_evidence_action(gate_args, "R8_PREDICTION_GATE"))
    before = run_dir / "r8-before-pressure.json"
    actions.append(_runner_action([
        "r8-workset-gate", "--config", str(run_dir / "r8-config.json"), "--cgroup", str(cgroup),
        "--apps", "|".join(apps), "--label", "before_pressure", "--output", str(before),
        "--gate-output", str(run_dir / "r8-workset-gate.json"),
    ], "R8_WORKSET_GATE"))
    actions.append(ACCEPT.trace_action(f"parp-accept-r8-{os.getpid()}-{seed}", "enable-reclaim", "R8_TRACE_ENABLE"))

    if baseline_only:
        actions.append(_runner_action([
            "r8-pressure-record", "--requested-mib", "0", "--committed-mib", "0",
            "--output", str(run_dir / "r8-pressure.json"),
        ], "R8_PRESSURE_BASELINE_RECORD"))
    else:
        firefox = specs["FIREFOX"]
        pressure_firefox = dataclasses.replace(firefox, window_title="PARP R8")
        actions.extend(_switch(pressure_firefox, "R8_PRESSURE"))
        actions.extend([
            {"type": "wait_window_title", "name": firefox.name, "app_key": "FIREFOX", "class": firefox.window_class,
             "title": "PARP R8", "pid_cmdline_contains": "firefox-pressure-profile",
             "expected_title": f"PARP R8 READY 0/{burst_mib} MiB",
             "timeout": float(r8["pressure_navigation_timeout_seconds"]), "poll_seconds": 0.02,
             "label": "R8_PRESSURE_READY_FIREFOX"},
            {"type": "trace_marker", "event_type": "R8_PRESSURE_START", "status": "running", "label": "R8_PRESSURE_START"},
        ])
        for chunk_index in range(1, burst_mib // 64 + 1):
            completed = chunk_index * 64
            prefix = f"R8_PRESSURE_CHUNK_{chunk_index:03d}"
            actions.extend([
                {"type": "click_window", "name": firefox.name, "app_key": "FIREFOX", "class": firefox.window_class,
                 "title": "PARP R8", "pid_cmdline_contains": "firefox-pressure-profile",
                 "x_ratio": 0.5, "y_ratio": 0.5, "label": f"{prefix}_REQUEST_FIREFOX"},
                {"type": "wait_window_title", "name": firefox.name, "app_key": "FIREFOX", "class": firefox.window_class,
                 "title": "PARP R8", "pid_cmdline_contains": "firefox-pressure-profile",
                 "expected_title": f"PARP R8 ALLOCATED {completed}/{burst_mib} MiB",
                 "timeout": float(r8["pressure_chunk_timeout_seconds"]), "poll_seconds": 0.01,
                 "label": f"{prefix}_READY_FIREFOX"},
            ])
        actions.append(_runner_action([
            "r8-pressure-record", "--requested-mib", str(burst_mib), "--committed-mib", str(burst_mib),
            "--output", str(run_dir / "r8-pressure.json"),
        ], "R8_PRESSURE_COMPLETE_RECORD"))
        actions.append({"type": "wait", "seconds": float(r8["pressure_hold_seconds"]), "label": "R8_PRESSURE_HOLD"})
    after = run_dir / "r8-after-pressure.json"
    actions.append(_runner_action([
        "r8-snapshot", "--cgroup", str(cgroup), "--apps", "|".join(apps),
        "--label", "after_pressure", "--output", str(after),
    ], "R8_SNAPSHOT_AFTER_PRESSURE"))
    actions.append({"type": "trace_marker", "event_type": "R8_COMPLETE", "status": "success", "label": "R8_COMPLETE"})
    scenario = {"name": SCENARIO, "seed": seed, "actions": actions, "keep_alive_after_s": 0}
    static = {
        "schema_version": 1, "scenario": SCENARIO, "seed": seed, "apps": apps,
        "asset_sha256": {key: row.get("sha256") for key, row in sorted(read_json(run_dir / "asset-manifest.json")["assets"].items())},
        "workset_actions": [
            {"label": str(action.get("label", "")), "type": str(action.get("type", "")),
             "key": action.get("key"), "repeat": action.get("repeat"), "interval": action.get("interval"),
             "seconds": action.get("seconds"), "timeout": action.get("timeout"),
             "text_bytes": len(str(action.get("text", "")).encode("utf-8"))}
            for action in actions if str(action.get("label", "")).startswith(("R8_WORKSET_", "R8_TRAINED_"))
        ],
        "memory_max_mib": int(r8.get("memory_max_mib", 0)), "memory_swap_max_mib": int(r8["memory_swap_max_mib"]),
        "memory_oom_group": bool(r8["memory_oom_group"]), "aggressor_score": int(r8["aggressor_oom_score_adj"]),
        "victim_score": int(r8["victim_oom_score_adj"]), "pressure_chunk_mib": 64, "pressure_burst_mib": burst_mib,
        "firefox_pressure_window_mode": "same_scope_dual_private_instance",
        "pressure_chunk_order": list(range(1, burst_mib // 64 + 1)),
        "waits": {key: r8[key] for key in sorted(r8) if key.endswith("seconds")},
        "frozen_calibration_sha256": str(r8.get("calibration", {}).get("frozen_config_sha256", "")),
    }
    return scenario, {**static, "sha256": canonical_sha256(static)}


def command_oom_score_exec(args: Any) -> int:
    command = list(args.command)
    if command and command[0] == "--":
        command.pop(0)
    if not command:
        raise ValueError("oom-score-exec requires a command after --")
    Path("/proc/self/oom_score_adj").write_text(f"{int(args.score)}\n", encoding="ascii")
    os.execvp(command[0], command)
    return 127


def _snapshot_for_cli(cgroup: Path, apps: list[str], label: str) -> dict[str, Any]:
    _need_bound()
    # The fixture directory is not needed to query existing X11 window classes.
    specs = ACCEPT.app_specs(Path("/tmp/parp-r8-snapshot"))
    return snapshot(cgroup, apps, specs, label)


def command_snapshot(args: Any) -> int:
    payload = _snapshot_for_cli(args.cgroup, args.apps.split("|"), args.label)
    write_json(args.output, payload)
    print(args.output)
    return 0


def command_workset_gate(args: Any) -> int:
    config = read_json(args.config)
    validate_config(config)
    apps = args.apps.split("|")
    payload = _snapshot_for_cli(args.cgroup, apps, args.label)
    rows = payload["apps"]
    reasons: list[str] = []
    total = 0
    for app in apps:
        row = rows[app]
        current = int(row.get("memory_current") or 0)
        total += current
        if not row.get("valid"):
            reasons.append(f"{app}: scope missing")
            continue
        if current < app_minimum_mib(config, app) * MIB:
            reasons.append(f"{app}: memory.current below tier threshold")
        if int(row.get("memory_oom_group") or 0) != 1:
            reasons.append(f"{app}: memory.oom.group is not 1")
        expected_score = int(config["r8_oom"]["aggressor_oom_score_adj"] if app == "FIREFOX" else config["r8_oom"]["victim_oom_score_adj"])
        processes = list(row.get("processes", []))
        if not processes:
            reasons.append(f"{app}: no PID in scope")
        for process in processes:
            if process.get("oom_score_adj") != expected_score:
                reasons.append(f"{app}: PID {process.get('pid')} OOM score mismatch")
    if total < int(config["r8_oom"]["minimum_total_working_set_mib"]) * MIB:
        reasons.append("aggregate application working set below 1536 MiB gate")
    gate = {
        "schema_version": 1, "valid": not reasons, "reasons": reasons,
        "total_memory_current_bytes": total,
        "minimum_total_bytes": int(config["r8_oom"]["minimum_total_working_set_mib"]) * MIB,
        "tiers_mib": _tiers(config), "apps": rows,
    }
    write_json(args.output, payload)
    write_json(args.gate_output, gate)
    print(args.gate_output)
    return 0 if gate["valid"] else 9


def command_enforce_oom_scores(args: Any) -> int:
    """Re-apply and verify the frozen per-app OOM score immediately pre-pressure."""
    config = read_json(args.config)
    validate_config(config)
    apps = args.apps.split("|")
    reasons: list[str] = []
    rows: dict[str, Any] = {}
    for app in apps:
        scope = _scope_path(args.cgroup, app)
        initial = _scope_row(scope)
        expected = int(
            config["r8_oom"]["aggressor_oom_score_adj"]
            if app == "FIREFOX" else config["r8_oom"]["victim_oom_score_adj"]
        )
        writes: list[dict[str, Any]] = []
        for pid in initial.get("pids", []):
            error = ""
            try:
                (Path("/proc") / str(pid) / "oom_score_adj").write_text(
                    f"{expected}\n", encoding="ascii",
                )
            except (FileNotFoundError, PermissionError, OSError) as exc:
                error = f"{type(exc).__name__}:{exc}"
            writes.append({"pid": pid, "expected": expected, "error": error})
        observed = _scope_row(scope)
        if not observed.get("valid") or not observed.get("processes"):
            reasons.append(f"{app}: no live scope/PID during OOM score gate")
        for process in observed.get("processes", []):
            if process.get("oom_score_adj") != expected:
                reasons.append(
                    f"{app}: PID {process.get('pid')} OOM score "
                    f"{process.get('oom_score_adj')} != {expected}"
                )
            relative_scope = str(scope).removeprefix("/sys/fs/cgroup") if scope is not None else ""
            if relative_scope and relative_scope not in str(process.get("cgroup", "")):
                reasons.append(f"{app}: PID {process.get('pid')} cgroup mismatch")
        rows[app] = {
            "expected_oom_score_adj": expected,
            "writes": writes,
            "observed": observed,
        }
    payload = {"schema_version": 1, "valid": not reasons, "reasons": reasons, "apps": rows}
    write_json(args.output, payload)
    print(args.output)
    return 0 if payload["valid"] else 11


def command_pressure_record(args: Any) -> int:
    requested = int(args.requested_mib) * MIB
    committed = int(args.committed_mib) * MIB
    payload = {
        "schema_version": 1, "pressure_requested_bytes": requested,
        "pressure_committed_bytes": committed, "pressure_complete": requested == committed,
        "pressure_chunk_bytes": 64 * MIB,
    }
    write_json(args.output, payload)
    print(args.output)
    return 0 if payload["pressure_complete"] else 10


def _pid_map(snapshot_payload: dict[str, Any]) -> dict[int, dict[str, Any]]:
    result: dict[int, dict[str, Any]] = {}
    for app, row in snapshot_payload.get("apps", {}).items():
        for process in row.get("processes", []):
            pid = int(process.get("pid", 0) or 0)
            if pid:
                result[pid] = {"app": app, **process}
    return result


def _trace_victims(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return rows
    for line in lines:
        if "mark_victim" not in line:
            continue
        pid = re.search(r"\bpid=(\d+)", line)
        comm = re.search(r"\bcomm=([^\s]+)", line)
        score = re.search(r"\boom_score_adj=(-?\d+)", line)
        if pid:
            rows.append({
                "pid": int(pid.group(1)), "comm": comm.group(1) if comm else "",
                "oom_score_adj": int(score.group(1)) if score else None, "trace": line,
            })
    return rows


def _counter_delta(before: dict[str, Any], after: dict[str, Any], key: str) -> int:
    return max(0, int(after.get(key, 0) or 0) - int(before.get(key, 0) or 0))


def _counter_text(text: str) -> dict[str, int]:
    values: dict[str, int] = {}
    for key, value in re.findall(r"([A-Za-z_][A-Za-z0-9_]*)[=: ]+(-?\d+)", text or ""):
        values[key] = int(value)
    return values


def _trace_lost(path: Path) -> bool:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return True
    for line in text.splitlines():
        lowered = line.lower()
        if "overrun" in lowered or "dropped" in lowered or "lost" in lowered:
            values = [int(value) for value in re.findall(r"\b(\d+)\b", line)]
            if any(values):
                return True
    return False


def evaluate_result(
    config: dict[str, Any], policy: str, run_dir: Path, before_vmstat: int,
    after_vmstat: int, policy_before: dict[str, Any], policy_after: dict[str, Any],
    automation_rc: int, abort_reason: str, pid_history: dict[int, dict[str, Any]],
    *, baseline_only: bool,
) -> dict[str, Any]:
    """Attribute OOM marks to pre-pressure application scopes and validate R8."""
    reasons: list[str] = []
    try:
        before = read_json(run_dir / "r8-before-pressure.json")
        after = read_json(run_dir / "r8-after-pressure.json")
        gate = read_json(run_dir / "r8-workset-gate.json")
        pressure = read_json(run_dir / "r8-pressure.json")
    except (FileNotFoundError, OSError, json.JSONDecodeError) as exc:
        early_reasons = [f"R8 artifact missing: {exc}"]
        if automation_rc != 0:
            early_reasons.append(f"automation returned {automation_rc}")
        if abort_reason:
            early_reasons.append(abort_reason)
        return {"status": "INVALID", "valid": False, "invalid_reasons": early_reasons}
    if not gate.get("valid"):
        reasons.extend(gate.get("reasons", ["R8 working-set gate invalid"]))
    try:
        prediction_gate = read_json(run_dir / "prediction-gate.json")
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        prediction_gate = {}
    if not prediction_gate.get("valid"):
        reasons.extend(prediction_gate.get("reasons", ["prediction gate invalid"]))
    if automation_rc != 0:
        reasons.append(f"automation returned {automation_rc}")
    if abort_reason:
        reasons.append(abort_reason)
    trace_stats = run_dir / "trace-stats.txt"
    if _trace_lost(trace_stats):
        reasons.append("trace lost or trace stats unavailable")
    trace_victims = _trace_victims(run_dir / "trace.txt")
    all_pids = dict(pid_history)
    all_pids.update(_pid_map(before))
    victims: set[str] = set()
    unknown_marks: list[dict[str, Any]] = []
    aggressor_marks: list[dict[str, Any]] = []
    score_mismatches: list[dict[str, Any]] = []
    r8 = config["r8_oom"]
    for mark in trace_victims:
        owner = all_pids.get(int(mark["pid"]))
        if owner is None:
            unknown_marks.append(mark)
            continue
        app = str(owner["app"])
        expected = int(r8["aggressor_oom_score_adj"] if app == "FIREFOX" else r8["victim_oom_score_adj"])
        if mark.get("oom_score_adj") != expected:
            score_mismatches.append(mark)
        if app == "FIREFOX":
            aggressor_marks.append(mark)
        else:
            victims.add(app)
    if unknown_marks:
        reasons.append("host or unknown OOM mark")
    if aggressor_marks:
        reasons.append("Firefox aggressor was selected as an OOM victim")
    if score_mismatches:
        reasons.append("OOM trace score does not match scope contract")
    parent_before = before.get("cgroup", {}).get("memory_events", {})
    parent_after = after.get("cgroup", {}).get("memory_events", {})
    event_delta = _counter_delta(parent_before, parent_after, "oom")
    kill_delta = _counter_delta(parent_before, parent_after, "oom_kill")
    group_delta = _counter_delta(parent_before, parent_after, "oom_group_kill")
    global_kill_delta = max(0, int(after_vmstat) - int(before_vmstat))
    host_or_unknown = bool(unknown_marks or global_kill_delta > kill_delta)
    if host_or_unknown:
        reasons.append("OOM occurred outside the experiment application scopes")
    app_rows: dict[str, Any] = {}
    untraced_disappearances: list[str] = []
    for app in config["apps"]:
        before_row = before.get("apps", {}).get(app, {})
        after_row = after.get("apps", {}).get(app, {})
        survived = bool(after_row.get("scope_alive") or after_row.get("window_alive"))
        disappeared = bool(before_row.get("scope_alive") and not survived)
        if disappeared and app not in victims:
            untraced_disappearances.append(app)
        app_rows[app] = {
            "before": before_row, "after": after_row, "survived": survived,
            "oom_victim": app in victims, "disappeared": disappeared,
        }
    if untraced_disappearances:
        reasons.append("application disappeared without an OOM trace")
    aggressor_survived = bool(app_rows["FIREFOX"]["survived"])
    if not aggressor_survived:
        reasons.append("Firefox aggressor did not survive")
    if not baseline_only:
        requested = int(r8["burst_mib"]) * MIB
        if int(pressure.get("pressure_requested_bytes", -1)) != requested:
            reasons.append("pressure request does not equal frozen Firefox burst")
        if not pressure.get("pressure_complete") or int(pressure.get("pressure_committed_bytes", -1)) != requested:
            reasons.append("Firefox pressure was not fully committed")
        if victims and group_delta < len(victims):
            reasons.append("oom_group_kill cross-check is lower than distinct victim apps")
        if (event_delta or kill_delta) and not trace_victims:
            reasons.append("memcg OOM event has no mark_victim trace")
    bin_delta = {
        key: after - _counter_text(str(policy_before.get("reclaim_bin_stats", ""))).get(key, 0)
        for key, after in _counter_text(str(policy_after.get("reclaim_bin_stats", ""))).items()
    }
    if policy == "bin_lstm":
        required_policy = {
            "reclaim_bin_enabled": "1", "reclaim_cold_enabled": "0", "reclaim_workload_enabled": "0",
            "effective_tier_mode": "0", "tier2_enabled": "0",
        }
        for key, expected in required_policy.items():
            if str(policy_after.get(key, "")) != expected:
                reasons.append(f"bin_lstm policy setting invalid: {key}")
        if not baseline_only and int(bin_delta.get("policy_hits", 0)) <= 0:
            reasons.append("reclaim-bin policy_hits did not increase")
        if not baseline_only and int(bin_delta.get("subtree_selected", 0)) <= 0:
            reasons.append("reclaim-bin selected no cgroup subtree")
    result = {
        "status": "VALID" if not reasons else "INVALID", "valid": not reasons,
        "invalid_reasons": list(dict.fromkeys(reasons)), "scenario": SCENARIO, "policy": policy,
        "pressure_requested_bytes": int(pressure.get("pressure_requested_bytes", 0)),
        "pressure_committed_bytes": int(pressure.get("pressure_committed_bytes", 0)),
        "pressure_complete": bool(pressure.get("pressure_complete")),
        "distinct_oom_victim_apps": len(victims), "victim_apps": sorted(victims),
        "surviving_apps": sorted(app for app, row in app_rows.items() if row["survived"]),
        "aggressor_survived": aggressor_survived, "oom_group_kill_delta": group_delta,
        "oom_kill_delta": kill_delta, "oom_event_delta": event_delta,
        "global_oom_kill_delta": global_kill_delta, "host_or_unknown_oom": host_or_unknown,
        "unknown_oom_marks": unknown_marks, "aggressor_oom_marks": aggressor_marks,
        "trace_victims": trace_victims, "applications": app_rows,
        "prediction_gate": prediction_gate, "workset_gate": gate, "reclaim_bin_delta": bin_delta,
        "policy_before": policy_before, "policy_after": policy_after,
        "baseline_only": baseline_only,
    }
    write_json(run_dir / "r8-oom-result.json", result)
    return result


def _setup(config: dict[str, Any], *, memory_max_mib: int) -> dict[str, Any]:
    return {
        "slice": config["slice"], "safety": {"memory_high_ratio": 0.90, "memory_max_ratio": 0.95},
        "peak": {"oom_threshold": {
            "enabled": True, "memory_high": "infinity", "memory_max_bytes": memory_max_mib * MIB,
            "memory_swap_max_bytes": int(config["r8_oom"]["memory_swap_max_mib"]) * MIB,
        }},
    }


def _record_pid_history(cgroup: Path, apps: list[str], history: dict[int, dict[str, Any]]) -> None:
    for app in apps:
        row = _scope_row(_scope_path(cgroup, app))
        for process in row.get("processes", []):
            pid = int(process.get("pid", 0) or 0)
            if pid:
                history[pid] = {"app": app, **process}


def run_one(
    config: dict[str, Any], policy: str, seed: int, run_dir: Path,
    expected_plan: dict[str, Any] | None = None, *, baseline_only: bool = False,
    burst_override_mib: int | None = None, allow_unfrozen: bool = False,
) -> dict[str, Any]:
    _need_bound()
    run_dir = run_dir.resolve()
    validate_config(config, require_frozen=not allow_unfrozen)
    if policy not in {"native_kernel", "bin_lstm"}:
        raise ValueError("R8 first phase supports native_kernel and bin_lstm only")
    run_dir.mkdir(parents=True, exist_ok=False)
    asset_manifest = prepare_assets(config, run_dir)
    write_json(run_dir / "asset-manifest.json", asset_manifest)
    # The in-scenario gate reads this immutable per-round copy, never a path
    # supplied by the caller that could change during a paired run.
    write_json(run_dir / "r8-config.json", config)
    preflight = TRAINED.preflight(config, policy)
    preflight["checks"]["r8_pressure_asset"] = (run_dir / "fixtures" / "oom-pressure.html").is_file()
    preflight["status"] = "READY" if all(preflight["checks"].values()) else "BLOCKED"
    write_json(run_dir / "preflight.json", preflight)
    if preflight["status"] != "READY":
        return {"status": "BLOCKED", "valid": False, "preflight": preflight, "run_dir": str(run_dir)}
    r8 = config["r8_oom"]
    burst_mib = 0 if baseline_only else int(burst_override_mib if burst_override_mib is not None else r8["burst_mib"])
    if burst_mib and burst_mib % 64:
        raise ValueError("R8 burst must be divisible by the 64 MiB browser chunk")
    cap_mib = memory_limit_cap_bytes(memtotal_mib()) // MIB
    configured_max = int(r8.get("memory_max_mib", 0))
    memory_max_mib = configured_max if configured_max > 0 else cap_mib
    if memory_max_mib <= 0 or memory_max_mib * MIB > memory_limit_cap_bytes(memtotal_mib()):
        return {"status": "BLOCKED", "valid": False, "invalid_reasons": ["R8 MemoryMax exceeds host calibration cap"], "run_dir": str(run_dir)}
    setup = _setup(config, memory_max_mib=memory_max_mib)
    variant = "bin_apply" if policy == "bin_lstm" else "native"
    original_policy: dict[str, Any] | None = None
    cgroup: Path | None = None
    automation: subprocess.Popen[Any] | None = None
    trace_stream: subprocess.Popen[Any] | None = None
    trace_output: Any = None
    trace_error: Any = None
    automation_rc = 1
    abort_reason = ""
    policy_before: dict[str, Any] = {}
    policy_after: dict[str, Any] = {}
    before_vmstat = 0
    after_vmstat = 0
    monitor: list[dict[str, Any]] = []
    pid_history: dict[int, dict[str, Any]] = {}
    trace_instance = f"parp-accept-r8-{os.getpid()}-{seed}"
    action_plan: dict[str, Any] = {}
    try:
        if policy == "bin_lstm":
            original_policy = ACCEPT.apply_global_policy("bin_apply")
        cgroup = ACCEPT.setup_slice(setup, variant)
        policy_before = ACCEPT.policy_state(cgroup)
        write_json(run_dir / "policy-before.json", policy_before)
        scenario, action_plan = generate_scenario(
            config, run_dir, cgroup, seed, policy, burst_mib=burst_mib, baseline_only=baseline_only,
        )
        write_json(run_dir / "scenario.json", scenario)
        write_json(run_dir / "action-plan.json", action_plan)
        if expected_plan is not None and action_plan.get("sha256") != expected_plan.get("sha256"):
            return {
                "status": "BLOCKED", "valid": False, "scenario": SCENARIO, "policy": policy, "seed": seed,
                "run_dir": str(run_dir), "expected_action_plan_sha256": expected_plan.get("sha256"),
                "observed_action_plan_sha256": action_plan.get("sha256"),
                "invalid_reasons": ["action-plan hash differs from Native replay"],
            }
        trace_setup = ACCEPT.run([
            "sudo", "-n", "bash", str(ACCEPT.TRACE_HELPER), "setup", trace_instance,
            str(int(config["safety"]["trace_buffer_kb_per_cpu"])),
        ], timeout=30)
        if trace_setup.returncode != 0:
            raise RuntimeError(trace_setup.stderr.strip() or "trace setup failed")
        trace_output = (run_dir / "trace.txt").open("w", encoding="utf-8")
        trace_error = (run_dir / "trace-error.txt").open("w", encoding="utf-8")
        trace_stream = subprocess.Popen(
            ["sudo", "-n", "bash", str(ACCEPT.TRACE_HELPER), "stream", trace_instance],
            stdout=trace_output, stderr=trace_error, text=True,
        )
        env = TRAINED.gui_environment()
        command = [
            sys.executable, str(AUTOMATION), str(run_dir / "scenario.json"),
            "--display", env["DISPLAY"], "--xauthority", env["XAUTHORITY"],
            "--trace-output", str(run_dir / "automation-trace.csv"), "--session-id", run_dir.name,
            "--scenario-id", "real_r8_multi_app_oom_survival", "--test-slice", str(config["slice"]),
            "--screenshot-output-dir", str(run_dir / "screenshots"),
        ]
        log = (run_dir / "automation.log").open("w", encoding="utf-8")
        automation = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, text=True, env=env, start_new_session=True)
        before_vmstat = int(ACCEPT.vmstat().get("oom_kill", 0))
        safety = config["safety"]
        low_count = psi_count = 0
        started = time.monotonic()
        while automation.poll() is None:
            sample = ACCEPT.snapshot(cgroup)
            monitor.append(sample)
            _record_pid_history(cgroup, list(config["apps"]), pid_history)
            low = sample["memavailable"] < int(safety["min_memavailable_mib"]) * MIB
            low_count = low_count + 1 if low else 0
            high_psi = float(sample["psi"].get("full_avg10", 0)) > float(safety["psi_full_avg10_abort"])
            psi_count = psi_count + 1 if low and high_psi else 0
            if low_count >= int(safety["abort_consecutive_samples"]):
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
            automation_rc = automation.wait(timeout=60)
        except subprocess.TimeoutExpired:
            os.killpg(automation.pid, signal.SIGKILL)
            automation_rc = automation.wait(timeout=15)
        log.close()
        _record_pid_history(cgroup, list(config["apps"]), pid_history)
        after_vmstat = int(ACCEPT.vmstat().get("oom_kill", 0))
        policy_after = ACCEPT.policy_state(cgroup)
        write_json(run_dir / "policy-after.json", policy_after)
    except Exception as exc:
        abort_reason = abort_reason or f"HARNESS_ERROR:{type(exc).__name__}:{exc}"
    finally:
        if automation is not None and automation.poll() is None:
            try:
                os.killpg(automation.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        ACCEPT.run(["sudo", "-n", "bash", str(ACCEPT.TRACE_HELPER), "disable", trace_instance], timeout=15)
        ACCEPT.run(["sudo", "-n", "bash", str(ACCEPT.TRACE_HELPER), "stop-stream", trace_instance], timeout=15)
        if trace_stream is not None:
            try:
                trace_stream.wait(timeout=10)
            except subprocess.TimeoutExpired:
                trace_stream.kill()
        ACCEPT.trace_stats(trace_instance, run_dir / "trace-stats.txt")
        ACCEPT.run(["sudo", "-n", "bash", str(ACCEPT.TRACE_HELPER), "cleanup", trace_instance], timeout=30)
        if trace_output is not None:
            trace_output.close()
        if trace_error is not None:
            trace_error.close()
        ACCEPT.cleanup_slice(setup)
        if original_policy is not None:
            ACCEPT.restore_global_policy(original_policy)
    write_json(run_dir / "monitor.json", monitor)
    result = evaluate_result(
        config, policy, run_dir, before_vmstat, after_vmstat, policy_before, policy_after,
        automation_rc, abort_reason, pid_history, baseline_only=baseline_only,
    )
    result.update({
        "seed": seed, "run_dir": str(run_dir), "action_plan_sha256": action_plan.get("sha256"),
        "replay_expected_sha256": expected_plan.get("sha256") if expected_plan else None,
        "replayed_from_baseline": expected_plan is not None,
        "monitor": {"samples": len(monitor), "min_memavailable_bytes": min((row["memavailable"] for row in monitor), default=None)},
    })
    write_json(run_dir / "run-result.json", result)
    return result


def command_run(args: Any) -> int:
    config = read_json(args.config)
    validate_config(config, require_frozen=True)
    if args.scenario not in {"all", SCENARIO}:
        raise ValueError("this configuration only supports R8")
    root = Path(config["output_root"])
    stamp = time.strftime("%Y%m%d_%H%M%S")
    session = root / f"r8-{args.policy}-{stamp}-{os.uname().release}"
    session.mkdir(parents=True, exist_ok=False)
    write_json(session / "config.json", config)
    native_runs: dict[int, dict[str, Any]] = {}
    if args.replay_from is not None:
        native_summary = read_json(args.replay_from / "summary.json")
        for row in native_summary.get("runs", []):
            if row.get("scenario") == SCENARIO and row.get("valid"):
                native_runs[int(row["seed"])] = row
    results: list[dict[str, Any]] = []
    for index in range(1, int(args.rounds) + 1):
        seed = int(args.seed) + index - 1
        expected: dict[str, Any] | None = None
        if args.replay_from is not None:
            native = native_runs.get(seed)
            if native is None:
                result = {
                    "status": "BLOCKED", "valid": False, "scenario": SCENARIO, "policy": args.policy,
                    "seed": seed, "invalid_reasons": ["no valid Native round with this seed for replay"],
                }
                results.append(result)
                break
            try:
                expected = read_json(Path(native["run_dir"]) / "action-plan.json")
            except (FileNotFoundError, OSError, json.JSONDecodeError) as exc:
                result = {
                    "status": "BLOCKED", "valid": False, "scenario": SCENARIO, "policy": args.policy,
                    "seed": seed, "invalid_reasons": [f"Native action plan unavailable: {exc}"],
                }
                results.append(result)
                break
        run_dir = session / f"round-{index:02d}-{SCENARIO}"
        print(f"R8 policy={args.policy} round={index}/{args.rounds} seed={seed}", flush=True)
        result = run_one(config, args.policy, seed, run_dir, expected_plan=expected)
        results.append(result)
        print(f"status={result['status']} output={run_dir}", flush=True)
        if not result.get("valid") and not args.keep_going:
            break
    summary = {
        "schema_version": 1,
        "status": "COMPLETE" if len(results) == int(args.rounds) and all(row.get("valid") for row in results) else "INCOMPLETE",
        "scenario": SCENARIO, "policy": args.policy, "rounds_requested": int(args.rounds),
        "runs": results, "config_sha256": canonical_sha256(frozen_config_contract(config)),
        "replay_from": str(args.replay_from) if args.replay_from else None,
    }
    write_json(session / "summary.json", summary)
    print(session)
    return 0 if summary["status"] == "COMPLETE" else 1


def _nearest_rank_p95(values: list[int]) -> int:
    if not values:
        raise ValueError("P95 needs at least one sample")
    ordered = sorted(values)
    return ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)]


def command_calibrate(args: Any) -> int:
    config = read_json(args.config)
    validate_config(config, require_frozen=False)
    if args.policy != "native_kernel":
        raise ValueError("R8 calibration is Native-only")
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    write_json(output / "input-config.json", config)
    calibration = config["r8_oom"]["calibration"]
    baseline_results: list[dict[str, Any]] = []
    baseline_currents: list[int] = []
    baseline_source = Path(args.baseline_from).resolve() if args.baseline_from else None
    if baseline_source is not None:
        try:
            source_config = read_json(baseline_source / "input-config.json")
        except (FileNotFoundError, OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"cannot read baseline source config: {exc}") from exc
        if canonical_sha256(source_config) != canonical_sha256(config):
            raise ValueError("baseline source config differs from current calibration config")
    for index in range(1, int(calibration["baseline_rounds"]) + 1):
        seed = int(calibration.get("seed", 20261001)) + index - 1
        run_dir = (
            baseline_source / f"baseline-{index:02d}"
            if baseline_source is not None else output / f"baseline-{index:02d}"
        )
        result = (
            read_json(run_dir / "run-result.json")
            if baseline_source is not None
            else run_one(config, "native_kernel", seed, run_dir, baseline_only=True, allow_unfrozen=True)
        )
        baseline_results.append(result)
        try:
            snapshot_before = read_json(run_dir / "r8-before-pressure.json")
            baseline_currents.append(int(snapshot_before["cgroup"]["memory_current"]))
        except (FileNotFoundError, OSError, KeyError, TypeError, ValueError):
            pass
    if len(baseline_currents) != int(calibration["baseline_rounds"]) or not all(row.get("valid") for row in baseline_results):
        report = {"status": "BLOCKED", "reason": "native no-pressure working-set calibration invalid", "baseline_runs": baseline_results}
        write_json(output / "calibration-report.json", report)
        print(output / "calibration-report.json")
        return 1
    p95 = _nearest_rank_p95(baseline_currents)
    requested_limit = memory_limit_from_p95(p95)
    cap = memory_limit_cap_bytes(memtotal_mib())
    if requested_limit > cap:
        report = {
            "status": "BLOCKED", "reason": "native P95-derived MemoryMax exceeds host cap",
            "p95_parent_memory_current_bytes": p95, "requested_memory_max_bytes": requested_limit,
            "host_cap_bytes": cap, "baseline_runs": baseline_results,
        }
        write_json(output / "calibration-report.json", report)
        print(output / "calibration-report.json")
        return 1
    candidate_results: list[dict[str, Any]] = []
    selected_burst = 0
    start = int(calibration["burst_start_mib"])
    step = int(calibration["burst_step_mib"])
    maximum = int(calibration["burst_max_mib"])
    rounds = int(calibration["candidate_rounds"])
    for burst in range(start, maximum + 1, step):
        rows: list[dict[str, Any]] = []
        for index in range(1, rounds + 1):
            candidate_config = copy.deepcopy(config)
            candidate_config["r8_oom"]["memory_max_mib"] = requested_limit // MIB
            candidate_config["r8_oom"]["burst_mib"] = burst
            seed = int(calibration.get("seed", 20261001)) + 1000 + (burst - start) // step * rounds + index - 1
            result = run_one(
                candidate_config, "native_kernel", seed, output / f"burst-{burst:04d}-round-{index:02d}",
                burst_override_mib=burst, allow_unfrozen=True,
            )
            rows.append(result)
        in_range = sum(1 for row in rows if 1 <= int(row.get("distinct_oom_victim_apps", 0)) <= 3)
        candidate = {
            "burst_mib": burst, "runs": rows, "all_valid": all(row.get("valid") for row in rows),
            "all_pressure_complete": all(row.get("pressure_complete") for row in rows),
            "in_range_rounds": in_range,
            "too_many_victim_rounds": sum(1 for row in rows if int(row.get("distinct_oom_victim_apps", 0)) > 3),
        }
        candidate_results.append(candidate)
        if (
            candidate["all_valid"] and candidate["all_pressure_complete"]
            and in_range >= int(calibration["minimum_in_range_rounds"])
            and candidate["too_many_victim_rounds"] == 0
        ):
            selected_burst = burst
            break
    status = "READY" if selected_burst else "INCONCLUSIVE"
    report = {
        "schema_version": 1, "status": status, "p95_parent_memory_current_bytes": p95,
        "memory_max_bytes": requested_limit, "host_cap_bytes": cap, "baseline_runs": baseline_results,
        "candidates": candidate_results, "selected_burst_mib": selected_burst,
    }
    if selected_burst:
        frozen = copy.deepcopy(config)
        frozen_r8 = frozen["r8_oom"]
        frozen_r8["memory_max_mib"] = requested_limit // MIB
        frozen_r8["burst_mib"] = selected_burst
        frozen_r8["calibration"]["frozen"] = True
        frozen_r8["calibration"]["native_parent_p95_bytes"] = p95
        frozen_r8["calibration"]["report_contract_sha256"] = canonical_sha256({
            "p95": p95, "memory_max": requested_limit, "burst_mib": selected_burst,
            "baseline": [row.get("action_plan_sha256") for row in baseline_results],
        })
        frozen_r8["calibration"].pop("frozen_config_sha256", None)
        frozen_r8["calibration"]["frozen_config_sha256"] = canonical_sha256(frozen_config_contract(frozen))
        validate_config(frozen, require_frozen=True)
        write_json(output / "frozen-config.json", frozen)
        report["frozen_config"] = str(output / "frozen-config.json")
        report["frozen_config_sha256"] = frozen_r8["calibration"]["frozen_config_sha256"]
    write_json(output / "calibration-report.json", report)
    print(output / "calibration-report.json")
    return 0 if selected_burst else 1


def _session_results(root: Path) -> list[dict[str, Any]]:
    summary = read_json(root / "summary.json")
    return [row for row in summary.get("runs", []) if row.get("scenario") == SCENARIO]


def report(native_root: Path, bin_root: Path) -> dict[str, Any]:
    native_rows = {int(row["seed"]): row for row in _session_results(native_root) if row.get("valid")}
    bin_rows = {int(row["seed"]): row for row in _session_results(bin_root) if row.get("valid")}
    pairs: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for seed in sorted(set(native_rows) & set(bin_rows)):
        native, binary = native_rows[seed], bin_rows[seed]
        reasons: list[str] = []
        if native.get("action_plan_sha256") != binary.get("action_plan_sha256"):
            reasons.append("action-plan hash differs")
        for side, row in (("native", native), ("bin", binary)):
            if not row.get("pressure_complete"):
                reasons.append(f"{side} pressure incomplete")
        if native.get("pressure_requested_bytes") != binary.get("pressure_requested_bytes") or native.get("pressure_committed_bytes") != binary.get("pressure_committed_bytes"):
            reasons.append("pressure byte count differs")
        if binary.get("policy") != "bin_lstm":
            reasons.append("bin result is not bin_lstm")
        if reasons:
            rejected.append({"seed": seed, "reasons": reasons})
        else:
            pairs.append({"seed": seed, "native": native, "bin": binary})
    selected = pairs[:10]
    native_counts = [int(row["native"].get("distinct_oom_victim_apps", 0)) for row in selected]
    bin_counts = [int(row["bin"].get("distinct_oom_victim_apps", 0)) for row in selected]
    native_total, bin_total = sum(native_counts), sum(bin_counts)
    if len(selected) < 10 or native_total < 10:
        status = "INCONCLUSIVE"
    else:
        reduction = 1.0 - bin_total / native_total
        native_median = statistics.median(native_counts)
        bin_median = statistics.median(bin_counts)
        status = "PASS" if reduction >= 0.30 and bin_median <= native_median else "FAIL"
    return {
        "schema_version": 1, "status": status, "valid_pairs": len(selected), "available_valid_pairs": len(pairs),
        "rejected_pairs": rejected, "pairs": selected, "native_total_victim_apps": native_total,
        "bin_total_victim_apps": bin_total,
        "reduction": (1.0 - bin_total / native_total) if native_total else None,
        "native_median": statistics.median(native_counts) if native_counts else None,
        "bin_median": statistics.median(bin_counts) if bin_counts else None,
    }


def command_report(args: Any) -> int:
    payload = report(Path(args.native), Path(args.bin))
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=False)
    write_json(output / "r8-comparison.json", payload)
    lines = [
        "# R8 多应用 OOM 生存对比", "", f"判定：**{payload['status']}**", "",
        f"有效配对：{payload['valid_pairs']}（可用 {payload['available_valid_pairs']}）", "",
        f"Native victim 应用总数：{payload['native_total_victim_apps']}",
        f"Bin victim 应用总数：{payload['bin_total_victim_apps']}",
        f"降幅：{payload['reduction']:.2%}" if payload["reduction"] is not None else "降幅：N/A",
        f"中位数（Native / Bin）：{payload['native_median']} / {payload['bin_median']}", "",
        "仅在 10 个有效同 seed 配对、两侧压力字节完全一致且 Native 总 victim 数至少为 10 时，才会给出 PASS/FAIL。",
    ]
    (output / "r8-comparison.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(output / "r8-comparison.json")
    return 0 if payload["status"] == "PASS" else 1
