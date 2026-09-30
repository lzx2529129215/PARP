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
LLM_SCENARIO = "r8_llm_weight_load"
R12_SCENARIO = "r12_current_kernel_oom_baseline"
R8_SCENARIOS = frozenset({SCENARIO, LLM_SCENARIO, R12_SCENARIO})
HEAVY_APPS = frozenset({"FIREFOX", "THUNDERBIRD", "GIMP", "LIBREOFFICE", "AUDACITY"})
MEDIUM_APPS = frozenset({"VLC", "EVINCE", "IMAGE_VIEWER", "RHYTHMBOX", "SHOTWELL", "FILES"})
LIGHT_APPS = frozenset({"CALCULATOR", "CALENDAR", "SYSTEM_MONITOR", "SOLITAIRE"})
ALL_R8_APPS = HEAVY_APPS | MEDIUM_APPS | LIGHT_APPS
R12_APPS = (
    "FIREFOX", "THUNDERBIRD", "GIMP", "LIBREOFFICE", "AUDACITY", "VLC",
    "EVINCE", "IMAGE_VIEWER", "RHYTHMBOX", "FILES", "CALCULATOR", "CALENDAR",
)

TRAINED: Any = None
ACCEPT: Any = None
RUNNER: Path | None = None
TEST_DIR = Path(__file__).resolve().parent
TEST_ROOT = TEST_DIR.parent
AUTOMATION = TEST_ROOT / "automation" / "app_automation.py"
ASSET_BUILDER = TEST_ROOT / "automation" / "create_real_pc_assets_lzx.py"
LLM_PRESSURE = TEST_ROOT / "automation" / "r8_llm_pressure_lzx.py"
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


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(8 * MIB):
            digest.update(chunk)
    return digest.hexdigest()


def configured_scenario(config: dict[str, Any]) -> str:
    scenarios = list(config.get("scenarios", []))
    if len(scenarios) != 1 or scenarios[0] not in R8_SCENARIOS:
        raise ValueError("R8 config must contain exactly one supported R8 scenario")
    return str(scenarios[0])


def is_llm_scenario(config: dict[str, Any]) -> bool:
    return configured_scenario(config) == LLM_SCENARIO


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
    scenario_name = configured_scenario(config)
    llm_mode = scenario_name == LLM_SCENARIO
    apps = list(config["apps"])
    if scenario_name == R12_SCENARIO:
        if apps != list(R12_APPS):
            raise ValueError("R12 requires the fixed 12 GUI applications in order")
    elif len(apps) != 15 or set(apps) != ALL_R8_APPS:
        raise ValueError("R8 requires the exact 15 LSAPP GUI applications")
    if set(config["hot_apps"]) | set(config["cold_apps"]) != set(apps):
        raise ValueError("R8 hot/cold sets must cover every application")
    if set(config["hot_apps"]) & set(config["cold_apps"]):
        raise ValueError("R8 hot/cold application sets overlap")
    if len(config["trained_history"]) != 5 or len(config["trained_history_vocab"]) != 5:
        raise ValueError("R8 requires the five-event LSTM history contract")
    expected_pressure = "llm_weight_load" if llm_mode else "firefox_arraybuffer_oom_burst"
    if config.get("pressure_mode") != expected_pressure:
        raise ValueError(f"{scenario_name} requires pressure_mode={expected_pressure}")
    r8 = config["r8_oom"]
    expected_aggressor = "LLM" if llm_mode else "FIREFOX"
    if r8.get("aggressor_app") != expected_aggressor:
        raise ValueError(f"{scenario_name} requires {expected_aggressor} as the only aggressor")
    victims = list(r8.get("victim_apps", []))
    expected_victims = set(apps) if llm_mode else set(apps) - {"FIREFOX"}
    if set(victims) != expected_victims or len(victims) != len(expected_victims):
        raise ValueError(f"{scenario_name} victim application set is invalid")
    if not llm_mode and int(r8.get("pressure_chunk_mib", 0)) != 64:
        raise ValueError("R8 Firefox pressure chunk must be exactly 64 MiB")
    if int(r8.get("memory_swap_max_mib", 0)) != 1024:
        raise ValueError("R8 MemorySwapMax must be 1024 MiB")
    if str(r8.get("memory_high", "")) != "infinity":
        raise ValueError("R8 MemoryHigh must remain infinity")
    expected_victim_score = 1000 if llm_mode else 500
    if (
        int(r8.get("victim_oom_score_adj", -1)) != expected_victim_score
        or int(r8.get("aggressor_oom_score_adj", -1)) != 0
    ):
        raise ValueError(
            f"{scenario_name} OOM score contract is aggressor=0 "
            f"and victims={expected_victim_score}"
        )
    if not bool(r8.get("memory_oom_group", False)):
        raise ValueError("R8 requires MemoryOOMGroup=yes per application scope")
    tiers = _tiers(config)
    if tiers["heavy"] < 128 or tiers["medium"] < 32 or tiers["light"] < 16:
        raise ValueError("R8 working-set thresholds may not be lowered")
    minimum_workset = 1024 if scenario_name == R12_SCENARIO else 1536
    if int(r8.get("minimum_total_working_set_mib", 0)) < minimum_workset:
        raise ValueError(f"aggregate working-set threshold must be at least {minimum_workset} MiB")
    calibration = r8.get("calibration", {})
    if scenario_name == R12_SCENARIO:
        if int(calibration.get("baseline_rounds", 0)) != 1:
            raise ValueError("R12 requires one no-pressure working-set measurement")
        if int(r8.get("minimum_victims", 0)) != 3 or int(r8.get("maximum_victims", 0)) != 4:
            raise ValueError("R12 requires 3-4 distinct OOM victim applications")
    elif int(calibration.get("baseline_rounds", 0)) != 3 or int(calibration.get("candidate_rounds", 0)) != 5:
        raise ValueError("R8 calibration requires three baseline and five candidate rounds")
    if scenario_name != R12_SCENARIO and not llm_mode and (
        int(calibration.get("burst_start_mib", 0)) != 512
        or int(calibration.get("burst_step_mib", 0)) != 128
    ):
        raise ValueError("R8 burst search must start at 512 MiB and step by 128 MiB")
    if scenario_name != R12_SCENARIO and int(calibration.get("minimum_in_range_rounds", 0)) < 4:
        raise ValueError("R8 calibration requires at least four in-range OOM rounds")
    if llm_mode:
        if "r8_llm" not in config:
            raise ValueError("R8 LLM config is missing r8_llm")
        llm = config["r8_llm"]
        if llm.get("runtime_kind") != "llama_cpp_server":
            raise ValueError("R8 LLM requires runtime_kind=llama_cpp_server")
        if llm.get("model_cache_state") != "cold_fadvise_dontneed":
            raise ValueError("R8 LLM requires cold_fadvise_dontneed model cache state")
        cached_ratio = float(llm.get("maximum_cached_model_ratio", -1))
        if not 0 <= cached_ratio <= 0.10:
            raise ValueError("R8 LLM maximum cached model ratio must be between 0 and 0.10")
        if not bool(llm.get("no_mmap")):
            raise ValueError("R8 LLM requires --no-mmap weight loading")
        if int(llm.get("context_length", 0)) <= 0 or int(llm.get("threads", 0)) <= 0:
            raise ValueError("R8 LLM context length and thread count must be positive")
        if int(llm.get("n_predict", 0)) != 1 or float(llm.get("temperature", -1)) != 0:
            raise ValueError("R8 LLM first phase requires one deterministic token")
        if not str(llm.get("prompt", "")):
            raise ValueError("R8 LLM prompt must be non-empty")
        if int(llm.get("minimum_model_bytes", 0)) < 128 * MIB:
            raise ValueError("R8 LLM minimum model size must be at least 128 MiB")
    if require_frozen:
        if not calibration.get("frozen"):
            raise ValueError("formal R8 runs require a frozen native calibration config")
        if int(r8.get("memory_max_mib", 0)) <= 0:
            raise ValueError("frozen R8 config lacks MemoryMax")
        if llm_mode:
            llm = config["r8_llm"]
            hashes = (str(llm.get("runtime_sha256", "")), str(llm.get("gguf_sha256", "")))
            if llm.get("status") != "ready" or any(not re.fullmatch(r"[0-9a-f]{64}", value) for value in hashes):
                raise ValueError("frozen R8 LLM config lacks ready runtime/GGUF hashes")
            if int(llm.get("model_size_bytes", 0)) < int(llm["minimum_model_bytes"]):
                raise ValueError("frozen R8 LLM config lacks a valid model size")
        else:
            if int(r8.get("burst_mib", 0)) <= 0:
                raise ValueError("frozen R8 config lacks Firefox burst")
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


def llm_asset_preflight(config: dict[str, Any], *, verify_hashes: bool = True) -> dict[str, Any]:
    """Fail closed unless the pinned real llama.cpp runtime and GGUF are local."""
    llm = config.get("r8_llm", {})
    runtime_text = str(llm.get("runtime_path", ""))
    model_text = str(llm.get("model_path", ""))
    runtime = Path(runtime_text) if runtime_text else Path("/__r8_missing_runtime__")
    model = Path(model_text) if model_text else Path("/__r8_missing_model__")
    checks = {
        "status_ready": llm.get("status") == "ready",
        "loader_exists": LLM_PRESSURE.is_file(),
        "runtime_executable": runtime.is_file() and os.access(runtime, os.X_OK),
        "gguf_regular_file": model.is_file(),
        "runtime_sha256_configured": bool(re.fullmatch(r"[0-9a-f]{64}", str(llm.get("runtime_sha256", "")))),
        "gguf_sha256_configured": bool(re.fullmatch(r"[0-9a-f]{64}", str(llm.get("gguf_sha256", "")))),
    }
    observed: dict[str, Any] = {
        "runtime_path": runtime_text,
        "model_path": model_text,
        "runtime_sha256": "",
        "gguf_sha256": "",
        "model_size_bytes": model.stat().st_size if model.is_file() else 0,
        "gguf_magic": "",
    }
    if model.is_file():
        try:
            observed["gguf_magic"] = model.open("rb").read(4).decode("ascii", errors="replace")
        except OSError:
            pass
    checks["gguf_magic"] = observed["gguf_magic"] == "GGUF"
    checks["model_size_matches"] = (
        observed["model_size_bytes"] == int(llm.get("model_size_bytes", 0))
        and observed["model_size_bytes"] >= int(llm.get("minimum_model_bytes", 0))
    )
    if verify_hashes and checks["runtime_executable"]:
        observed["runtime_sha256"] = file_sha256(runtime)
    if verify_hashes and checks["gguf_regular_file"]:
        observed["gguf_sha256"] = file_sha256(model)
    checks["runtime_sha256_matches"] = bool(
        verify_hashes and checks["runtime_sha256_configured"]
        and observed["runtime_sha256"] == str(llm.get("runtime_sha256", ""))
    )
    checks["gguf_sha256_matches"] = bool(
        verify_hashes and checks["gguf_sha256_configured"]
        and observed["gguf_sha256"] == str(llm.get("gguf_sha256", ""))
    )
    reasons = [name for name, passed in checks.items() if not passed]
    return {
        "schema_version": 1,
        "status": "READY" if not reasons else "BLOCKED",
        "checks": checks,
        "reasons": reasons,
        "observed": observed,
        "contract": {
            key: llm.get(key) for key in (
                "runtime_kind", "runtime_source_tag", "runtime_source_commit",
                "runtime_sha256", "model_repository", "model_revision",
                "model_filename", "model_quantization", "gguf_sha256", "model_size_bytes",
                "prompt", "context_length", "threads", "n_predict", "temperature",
                "model_cache_state", "maximum_cached_model_ratio", "no_mmap",
            )
        },
    }


def prune_round_working_assets(run_dir: Path) -> dict[str, Any]:
    """Drop reproducible per-round working copies after evidence is frozen."""
    names = (
        "fixtures", "firefox-profile", "firefox-pressure-profile",
        "firefox-pressure-a-profile", "firefox-pressure-b-profile",
        "thunderbird-profile",
    )
    removed: list[dict[str, Any]] = []
    for name in names:
        target = run_dir / name
        if not target.exists():
            continue
        files = sum(1 for path in target.rglob("*") if path.is_file())
        bytes_used = sum(
            path.stat().st_size
            for path in target.rglob("*")
            if path.is_file() and not path.is_symlink()
        )
        shutil.rmtree(target)
        removed.append({"name": name, "files": files, "logical_bytes": bytes_used})
    payload = {
        "schema_version": 1,
        "reason": "reproducible temporary working copies pruned after evidence capture",
        "removed": removed,
    }
    if removed:
        write_json(run_dir / "r8-pruned-working-assets.json", payload)
    return payload


def command_prune_outputs(args: Any) -> int:
    root = Path(args.root).resolve()
    if not root.is_dir() or "r8_calibration" not in root.name:
        raise ValueError("prune root must be one R8 calibration output directory")
    rows: list[dict[str, Any]] = []
    for run_dir in sorted(path for path in root.iterdir() if path.is_dir()):
        if not (run_dir / "asset-manifest.json").is_file():
            continue
        payload = prune_round_working_assets(run_dir)
        if payload["removed"]:
            rows.append({"run_dir": str(run_dir), **payload})
    summary = {"schema_version": 1, "root": str(root), "runs_pruned": len(rows), "runs": rows}
    write_json(root / "r8-prune-summary.json", summary)
    print(root / "r8-prune-summary.json")
    return 0


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
        return {"valid": False, "reason": "scope missing", "pids": [], "threads": []}
    pids = []
    threads = []
    try:
        pids = [int(item) for item in (path / "cgroup.procs").read_text(encoding="ascii").split()]
    except (FileNotFoundError, PermissionError, OSError, ValueError):
        pass
    try:
        threads = [int(item) for item in (path / "cgroup.threads").read_text(encoding="ascii").split()]
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
        "pids": pids, "threads": threads, "processes": processes,
    }


def _window_ids(window_class: str) -> list[str]:
    if not shutil.which("xdotool"):
        return []
    result = subprocess.run(
        ["xdotool", "search", "--onlyvisible", "--class", window_class],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False,
    )
    return [line for line in result.stdout.splitlines() if line.isdigit()]


def snapshot(
    cgroup: Path, apps: list[str], specs: dict[str, Any], label: str,
    *, include_llm: bool = False,
) -> dict[str, Any]:
    rows: dict[str, Any] = {}
    for app in apps:
        row = _scope_row(_scope_path(cgroup, app))
        row["window_ids"] = _window_ids(specs[app].window_class)
        row["window_alive"] = bool(row["window_ids"])
        row["scope_alive"] = bool(row.get("pids"))
        rows[app] = row
    payload = {
        "schema_version": 1, "label": label, "timestamp_ns": time.time_ns(),
        "cgroup": _scope_row(cgroup), "apps": rows,
    }
    if include_llm:
        llm_row = _scope_row(_scope_path(cgroup, "LLM_AGGRESSOR"))
        # Keep the LLM scope contract identical to application scope rows.
        # evaluate_result deliberately requires the aggressor to still own a
        # live PID after the pressure hold; a valid raw scope row alone does
        # not prove that because an empty systemd scope can briefly persist.
        llm_row["scope_alive"] = bool(llm_row.get("pids"))
        payload["llm"] = llm_row
    return payload


def _r8_specs(
    run_dir: Path, pressure_mib: int, seed: int, *, firefox_pressure: bool = True,
    two_lanes: bool = False, r12_mail: bool = False,
) -> dict[str, Any]:
    _need_bound()
    specs = ACCEPT.app_specs(run_dir)
    fixture = run_dir / "fixtures"
    effective_burst_mib = max(512, pressure_mib)
    total_chunks = effective_burst_mib // 64
    lane_a_mib = ((total_chunks + 1) // 2) * 64
    lane_b_mib = (total_chunks // 2) * 64
    browser_environment = [
        "env", "-u", "http_proxy", "-u", "https_proxy",
        "-u", "HTTP_PROXY", "-u", "HTTPS_PROXY",
        # WebKitGTK otherwise terminates its web process from the userspace
        # memory-pressure monitor before the ancestor memcg OOM killer can
        # select one of R8's higher-scored victim scopes.  R8 is specifically
        # an experiment of the kernel OOM decision, so keep that independent
        # userspace safety mechanism out of both browser instances.
        "WEBKIT_DISABLE_MEMORY_PRESSURE_MONITOR=1",
    ]
    pressure_a_command = [
        *browser_environment, "epiphany-browser", "--private-instance",
        f"--profile={run_dir / 'firefox-pressure-a-profile'}",
        (fixture / "oom-pressure.html").as_uri() + f"?mib={lane_a_mib}&seed={seed}",
    ]
    pressure_b_command = [
        *browser_environment, "epiphany-browser", "--private-instance",
        f"--profile={run_dir / 'firefox-pressure-b-profile'}",
        (fixture / "oom-pressure.html").as_uri() + f"?mib={lane_b_mib}&seed={seed + 1}",
    ]
    workset_command = [
        *browser_environment, "epiphany-browser", "--private-instance",
        f"--profile={run_dir / 'firefox-profile'}",
        (fixture / "local-page.html").as_uri(),
    ]
    pressure_commands: list[list[str]] = []
    if firefox_pressure:
        pressure_commands.append(pressure_a_command)
        if pressure_mib or two_lanes:
            pressure_commands.append(pressure_b_command)
    dual_browser_script = " ".join(
        f"{shlex.join(command)} &" for command in pressure_commands
    ) + f" exec {shlex.join(workset_command)}"
    specs["FIREFOX"] = dataclasses.replace(
        specs["FIREFOX"],
        command=shlex.join(["/bin/sh", "-c", dual_browser_script]),
    )
    if r12_mail:
        specs["THUNDERBIRD"] = dataclasses.replace(
            specs["THUNDERBIRD"],
            command=shlex.join([
                "thunderbird", "--no-remote", "--profile", str(run_dir / "thunderbird-profile"),
                "-file", str(fixture / "mail-test.eml"),
            ]),
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
    specs["GIMP"] = dataclasses.replace(
        specs["GIMP"],
        command=(
            f"env HOME={shlex.quote(str(fixture / 'gimp-home'))} "
            f"XDG_CONFIG_HOME={shlex.quote(str(fixture / 'gimp-config'))} "
            f"gimp --new-instance --no-splash --console-messages "
            f"--gimprc={shlex.quote(str(fixture / 'gimprc'))} "
            + " ".join(
                shlex.quote(str(fixture / f"image-test-{index:02d}.png"))
                for index in range(1, 7)
            )
        ),
    )
    audio = shlex.quote(str(fixture / "audio-test.wav"))
    specs["AUDACITY"] = dataclasses.replace(
        specs["AUDACITY"],
        command=(
            f"env HOME={shlex.quote(str(fixture / 'audacity-home'))} "
            f"XDG_CONFIG_HOME={shlex.quote(str(fixture / 'audacity-config'))} "
            f"audacity {' '.join([audio] * 8)}"
        ),
    )
    pdf = shlex.quote(str(fixture / "document-test.pdf"))
    evince_command = " ".join(
        f"evince --new-window {pdf} &" for _ in range(3)
    ) + f" exec evince --new-window {pdf}"
    specs["EVINCE"] = dataclasses.replace(
        specs["EVINCE"], command=shlex.join(["/bin/sh", "-c", evince_command]),
    )
    specs["CALCULATOR"] = dataclasses.replace(
        specs["CALCULATOR"],
        command="gnome-calculator --mode=programming --equation=100000!",
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


def _switch(
    spec: Any, label: str, *, pid_cmdline_contains: str | None = None,
) -> list[dict[str, Any]]:
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
        display = TRAINED.gui_environment().get("DISPLAY", ":0")
        dimensions = subprocess.run(
            ["xdpyinfo", "-display", display], text=True,
            capture_output=True, check=False, timeout=5,
        ).stdout
        match = re.search(r"dimensions:\s*(\d+)x(\d+)", dimensions)
        screen_height = int(match.group(2)) if match else 800
        window_contract.update({
            "minimum_foreground_width": 700,
            "minimum_foreground_height": min(500, max(280, int(screen_height * 0.72))),
            "dismiss_small_transient": True,
            "pid_cmdline_contains": pid_cmdline_contains or (
                "firefox-pressure-profile"
                if "PARP R8" in spec.window_title else "/firefox-profile"
            ),
        })
    elif spec.key == "GIMP":
        # First-run/recovery dialogs use the same WM_CLASS as the content
        # window.  They cannot satisfy a native working-set action.
        display = TRAINED.gui_environment().get("DISPLAY", ":0")
        dimensions = subprocess.run(
            ["xdpyinfo", "-display", display], text=True,
            capture_output=True, check=False, timeout=5,
        ).stdout
        match = re.search(r"dimensions:\s*(\d+)x(\d+)", dimensions)
        screen_height = int(match.group(2)) if match else 800
        window_contract.update({
            "minimum_foreground_width": 700,
            "minimum_foreground_height": min(500, max(280, int(screen_height * 0.72))),
            "dismiss_small_transient": True,
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
                for _ in range(8)
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
            {"type": "key", "key": "plus", "repeat": 5, "interval": 0.08},
            {"type": "key", "key": "Home"}, {"type": "key", "key": "Page_Down", "repeat": 239, "interval": 0.05},
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
            {"type": "key", "key": "ctrl+n", "repeat": 8, "interval": 0.3},
            {"type": "type", "text": "".join(f"{index}+{index}=" for index in range(1, 257)), "delay_ms": 2},
            {"type": "key", "key": "Return"},
            {"type": "key", "key": "ctrl+a"},
            {"type": "type", "text": "100000!", "delay_ms": 20},
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
    *, burst_mib: int, baseline_only: bool, adaptive_stop: bool = False,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build the R8-only GUI scenario and its static action-plan contract."""
    _need_bound()
    scenario_name = configured_scenario(config)
    llm_mode = scenario_name == LLM_SCENARIO
    apps = list(config["apps"])
    r8 = config["r8_oom"]
    aggressor = str(r8["aggressor_app"])
    specs = _r8_specs(
        run_dir, burst_mib, seed, firefox_pressure=not llm_mode,
        two_lanes=scenario_name == R12_SCENARIO,
        r12_mail=scenario_name == R12_SCENARIO,
    )
    actions: list[dict[str, Any]] = [{
        "type": "trace_marker", "event_type": "R8_START", "status": "running",
        "label": "R8_START", "metadata": {"scenario": scenario_name, "seed": seed},
    }]
    for app in apps:
        launch, wait = ACCEPT.app_launch_actions(specs[app])
        launch = dict(launch)
        score = int(r8["aggressor_oom_score_adj"] if app == aggressor else r8["victim_oom_score_adj"])
        launch["command"] = _score_wrapped(str(launch["command"]), score)
        # Some systemd 249 scopes remain in stop-sigterm until the default
        # 90-second timer even after cgroup.events reports populated=0.  Bound
        # that post-snapshot cleanup delay without changing pressure behavior.
        launch["scope_properties"] = {
            "MemoryOOMGroup": "yes", "TimeoutStopSec": "5s",
        }
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
    # The final workset app is Solitaire today, but do not rely on that order
    # to create the first trained-history transition.  Pin the foreground to
    # the configured current app before the marker so the first Thunderbird
    # switch always emits a distinct desktop event.  Otherwise a stale
    # Thunderbird foreground can silently shorten the five-event LSTM history.
    actions.extend(_switch(specs[str(config["current_app"])], "R8_PREDICTION_ANCHOR"))
    actions.append({
        "type": "wait", "seconds": float(config["history_dwell_seconds"]),
        "label": "R8_PREDICTION_ANCHOR_DWELL",
    })
    marker = run_dir / "r8-prediction-mark.json"
    actions.append(_evidence_action(["mark", "--output", str(marker)], "R8_PREDICTION_MARK"))
    for index, app in enumerate(config["trained_history"], start=1):
        actions.extend(_switch(specs[app], f"R8_TRAINED_{index:02d}"))
        actions.append(_runner_action([
            "r8-publish-verified-switch", "--app", app,
            "--sequence", str(index),
            "--title", str(specs[app].window_title),
            "--class", str(specs[app].window_class),
            "--output", str(run_dir / f"r8-prediction-switch-{index:02d}.json"),
        ], f"R8_TRAINED_{index:02d}_PUBLISH_{app}"))
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
        "--require-myfs" if policy in {"bin_lstm", "current_kernel"} else "--no-require-myfs",
    ]
    if scenario_name != R12_SCENARIO:
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
    elif llm_mode:
        llm = config["r8_llm"]
        state = run_dir / "r8-llm-state.json"
        samples = run_dir / "r8-llm-memory-samples.jsonl"
        server_log = run_dir / "r8-llm-server.log"
        port = int(llm["port_base"]) + seed % int(llm["port_span"])
        llm_command = shlex.join([
            sys.executable, str(LLM_PRESSURE),
            "--runtime", str(llm["runtime_path"]),
            "--model", str(llm["model_path"]),
            "--runtime-sha256", str(llm["runtime_sha256"]),
            "--gguf-sha256", str(llm["gguf_sha256"]),
            "--model-size-bytes", str(int(llm["model_size_bytes"])),
            "--prompt", str(llm["prompt"]),
            "--context-length", str(int(llm["context_length"])),
            "--threads", str(int(llm["threads"])),
            "--seed", str(seed), "--port", str(port),
            "--load-timeout", str(float(llm["load_timeout_seconds"])),
            "--completion-timeout", str(float(llm["completion_timeout_seconds"])),
            "--sample-interval", str(float(llm["sample_interval_seconds"])),
            "--maximum-cached-ratio", str(float(llm["maximum_cached_model_ratio"])),
            "--state", str(state), "--samples", str(samples),
            "--server-log", str(server_log),
        ])
        actions.append({
            "type": "trace_marker", "event_type": "R8_LLM_LOAD_START",
            "status": "running", "label": "R8_LLM_LOAD_START",
        })
        actions.append({
            "type": "launch", "name": "LLM", "app_key": "LLM",
            "scope_name": "llm-aggressor", "command": _score_wrapped(llm_command, 0),
            "scope_properties": {"MemoryOOMGroup": "yes", "TimeoutStopSec": "5s"},
            "label": "R8_LLM_LAUNCH",
        })
        actions.append({
            "type": "wait_json", "path": str(state), "field": "status",
            "equals": "FIRST_TOKEN_COMPLETE", "timeout": float(llm["load_timeout_seconds"]) + float(llm["completion_timeout_seconds"]),
            "poll_seconds": 0.1, "label": "R8_LLM_FIRST_TOKEN_GATE",
        })
        actions.append(_runner_action([
            "r8-llm-pressure-record", "--config", str(run_dir / "r8-config.json"),
            "--state", str(state), "--output", str(run_dir / "r8-pressure.json"),
        ], "R8_LLM_PRESSURE_COMPLETE_RECORD"))
        actions.append({
            "type": "wait", "seconds": float(r8["pressure_hold_seconds"]),
            "label": "R8_LLM_PRESSURE_HOLD",
        })
    elif scenario_name == R12_SCENARIO:
        actions.append({
            "type": "trace_marker", "event_type": "R8_PRESSURE_START",
            "status": "running", "label": "R12_PRESSURE_START",
        })
        actions.append(_runner_action([
            "r12-pressure", "--before", str(before), "--trace", str(run_dir / "trace.txt"),
            "--cgroup", str(cgroup), "--maximum-mib", str(burst_mib),
            "--output", str(run_dir / "r8-pressure.json"),
            *(["--adaptive-stop"] if adaptive_stop else []),
        ], "R12_PRESSURE_ALLOCATE"))
        actions.append({"type": "wait", "seconds": float(r8["pressure_hold_seconds"]), "label": "R12_PRESSURE_HOLD"})
    else:
        firefox = specs["FIREFOX"]
        pressure_firefox = dataclasses.replace(firefox, window_title="PARP R8")
        total_chunks = burst_mib // 64
        lane_targets = {
            "A": ((total_chunks + 1) // 2) * 64,
            "B": (total_chunks // 2) * 64,
        }
        lane_profiles = {
            "A": "firefox-pressure-a-profile",
            "B": "firefox-pressure-b-profile",
        }
        for lane in ("A", "B"):
            profile = lane_profiles[lane]
            actions.extend(_switch(
                pressure_firefox, f"R8_PRESSURE_{lane}",
                pid_cmdline_contains=profile,
            ))
            actions.append({
                "type": "wait_window_title", "name": firefox.name,
                "app_key": "FIREFOX", "class": firefox.window_class,
                "title": "PARP R8", "pid_cmdline_contains": profile,
                "expected_title": f"PARP R8 READY 0/{lane_targets[lane]} MiB",
                "timeout": float(r8["pressure_navigation_timeout_seconds"]),
                "poll_seconds": 0.02,
                "label": f"R8_PRESSURE_READY_FIREFOX_{lane}",
            })
        actions.append({
            "type": "trace_marker", "event_type": "R8_PRESSURE_START",
            "status": "running", "label": "R8_PRESSURE_START",
        })
        for chunk_index in range(1, burst_mib // 64 + 1):
            lane = "A" if chunk_index % 2 else "B"
            profile = lane_profiles[lane]
            completed = ((chunk_index + 1) // 2 if lane == "A" else chunk_index // 2) * 64
            prefix = f"R8_PRESSURE_CHUNK_{chunk_index:03d}"
            actions.extend([
                {"type": "click_window", "name": firefox.name, "app_key": "FIREFOX", "class": firefox.window_class,
                 "title": "PARP R8", "pid_cmdline_contains": profile,
                 "x_ratio": 0.5, "y_ratio": 0.5, "activate_before_click": True,
                 "label": f"{prefix}_REQUEST_FIREFOX_{lane}"},
                {"type": "wait_window_title", "name": firefox.name, "app_key": "FIREFOX", "class": firefox.window_class,
                 "title": "PARP R8", "pid_cmdline_contains": profile,
                 "expected_title": f"PARP R8 ALLOCATED {completed}/{lane_targets[lane]} MiB",
                 "timeout": float(r8["pressure_chunk_timeout_seconds"]), "poll_seconds": 0.01,
                 "label": f"{prefix}_READY_FIREFOX_{lane}"},
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
        *(["--include-llm"] if llm_mode and not baseline_only else []),
    ], "R8_SNAPSHOT_AFTER_PRESSURE"))
    if scenario_name == R12_SCENARIO and not baseline_only:
        actions.append(_runner_action([
            "r12-recovery", "--config", str(run_dir / "r8-config.json"),
            "--cgroup", str(cgroup), "--before", str(before), "--after", str(after),
            "--trace", str(run_dir / "trace.txt"), "--run-dir", str(run_dir),
            "--output", str(run_dir / "r12-recovery.json"),
        ], "R12_RECOVERY"))
    actions.append({"type": "trace_marker", "event_type": "R8_COMPLETE", "status": "success", "label": "R8_COMPLETE"})
    scenario = {"name": scenario_name, "seed": seed, "actions": actions, "keep_alive_after_s": 0}
    llm_contract = {}
    if llm_mode:
        llm_contract = {
            key: config["r8_llm"][key] for key in (
                "runtime_kind", "runtime_source_tag", "runtime_source_commit",
                "runtime_sha256", "model_repository", "model_revision",
                "model_filename", "model_quantization", "gguf_sha256", "model_size_bytes",
                "prompt", "context_length", "threads", "n_predict", "temperature",
                "model_cache_state", "no_mmap", "load_timeout_seconds",
                "maximum_cached_model_ratio", "completion_timeout_seconds",
                "sample_interval_seconds", "port_base", "port_span",
            )
        }
    static = {
        "schema_version": 1, "scenario": scenario_name, "seed": seed, "apps": apps,
        "asset_sha256": {key: row.get("sha256") for key, row in sorted(read_json(run_dir / "asset-manifest.json")["assets"].items())},
        "workset_actions": [
            {"label": str(action.get("label", "")), "type": str(action.get("type", "")),
             "key": action.get("key"), "repeat": action.get("repeat"), "interval": action.get("interval"),
             "seconds": action.get("seconds"), "timeout": action.get("timeout"),
             "text_bytes": len(str(action.get("text", "")).encode("utf-8"))}
            for action in actions if str(action.get("label", "")).startswith(
                ("R8_WORKSET_", "R8_PREDICTION_ANCHOR", "R8_TRAINED_")
            )
        ],
        "memory_max_mib": int(r8.get("memory_max_mib", 0)), "memory_swap_max_mib": int(r8["memory_swap_max_mib"]),
        "memory_oom_group": bool(r8["memory_oom_group"]), "aggressor_score": int(r8["aggressor_oom_score_adj"]),
        "victim_score": int(r8["victim_oom_score_adj"]),
        "pressure_kind": "llama_cpp_no_mmap_weight_load" if llm_mode else "firefox_arraybuffer",
        "pressure_chunk_mib": 0 if llm_mode else 64,
        "pressure_burst_mib": 0 if llm_mode else burst_mib,
        "firefox_pressure_window_mode": "disabled_llm_is_only_aggressor" if llm_mode else "same_scope_two_pressure_plus_workset_private_instances",
        "pressure_click_activates_target": not llm_mode,
        "adaptive_stop": adaptive_stop,
        "pressure_chunk_order": [
            {
                "index": index,
                "lane": "A" if index % 2 else "B",
                "lane_completed_mib": (
                    ((index + 1) // 2) if index % 2 else (index // 2)
                ) * 64,
            }
            for index in range(1, (0 if llm_mode else burst_mib // 64) + 1)
        ],
        "llm_contract": llm_contract,
        "runtime_monitor_reset": "systemd_restart_before_each_r8_round",
        "prediction_event_source": "verified_active_window_dbus_after_ui_switch",
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


def command_publish_verified_switch(args: Any) -> int:
    """Publish one LSTM edge only after verifying the real active GUI scope."""
    app = str(args.app).upper()
    if app not in ALL_R8_APPS:
        raise ValueError(f"unsupported R8 app: {app}")
    scope = f"automation-{app.lower().replace('_', '-')}.scope"
    active = subprocess.run(
        ["xdotool", "getactivewindow"], text=True, capture_output=True,
        check=False, timeout=5,
    )
    active_window_id = active.stdout.strip()

    def inspect_window(candidate: str) -> dict[str, Any]:
        title_result = subprocess.run(
            ["xdotool", "getwindowname", candidate], text=True, capture_output=True,
            check=False, timeout=5,
        )
        properties = subprocess.run(
            ["xprop", "-id", candidate, "WM_CLASS", "_NET_WM_PID"],
            text=True, capture_output=True, check=False, timeout=5,
        )
        title = title_result.stdout.strip()
        wm_class = ""
        pid = 0
        for line in properties.stdout.splitlines():
            if line.startswith("WM_CLASS") and "=" in line:
                wm_class = "|".join(
                    item.strip().strip('"') for item in line.split("=", 1)[1].split(",")
                )
            elif line.startswith("_NET_WM_PID") and "=" in line:
                try:
                    pid = int(line.split("=", 1)[1].strip())
                except ValueError:
                    pid = 0
        proc = Path("/proc") / str(pid)
        try:
            cgroup = (proc / "cgroup").read_text(encoding="utf-8", errors="replace")
            comm = (proc / "comm").read_text(encoding="utf-8", errors="replace").strip()
        except (FileNotFoundError, PermissionError, OSError):
            cgroup = ""
            comm = ""
        return {
            "window_id": candidate, "title": title, "wm_class": wm_class,
            "pid": pid, "comm": comm, "cgroup": cgroup,
        }

    inspected: list[dict[str, Any]] = []
    if active_window_id:
        inspected.append(inspect_window(active_window_id))
    for option, pattern in (("--class", str(args.window_class)), ("--name", str(args.title))):
        if not pattern:
            continue
        found = subprocess.run(
            ["xdotool", "search", "--onlyvisible", option, pattern],
            text=True, capture_output=True, check=False, timeout=5,
        )
        known = {str(row["window_id"]) for row in inspected}
        for candidate in found.stdout.splitlines():
            candidate = candidate.strip()
            if candidate and candidate not in known:
                inspected.append(inspect_window(candidate))
                known.add(candidate)
    eligible = [row for row in inspected if f"/{scope}" in str(row["cgroup"])]
    chosen = eligible[0] if eligible else (
        inspected[0] if inspected else {
            "window_id": "", "title": "", "wm_class": "", "pid": 0,
            "comm": "", "cgroup": "",
        }
    )
    window_id = str(chosen["window_id"])
    title = str(chosen["title"])
    wm_class = str(chosen["wm_class"])
    pid = int(chosen["pid"])
    comm = str(chosen["comm"])
    cgroup = str(chosen["cgroup"])
    reasons: list[str] = []
    if not window_id:
        reasons.append("no verified target X11 window")
    if pid <= 0:
        reasons.append("target window has no _NET_WM_PID")
    if f"/{scope}" not in cgroup:
        reasons.append(f"target window PID is not in {scope}")
    event = {
        "event_type": "Switched",
        "timestamp_ms": time.time_ns() // 1_000_000,
        "window_id": f"r8-verified-{window_id}",
        "title": title,
        "wm_class": wm_class,
        "gtk_app_id": "",
        "pid": pid,
        "is_minimized": False,
    }
    result: dict[str, Any] = {
        "schema_version": 1, "valid": not reasons, "reasons": reasons,
        "app": app, "sequence": int(args.sequence), "window_id": window_id,
        "active_window_id": active_window_id,
        "selection": "active_window" if window_id == active_window_id else "verified_scope_window",
        "title": title, "wm_class": wm_class, "pid": pid, "comm": comm,
        "expected_scope": scope, "cgroup": cgroup, "event": event,
        "source": "verified_active_window_dbus_after_ui_switch",
    }
    if not reasons:
        emitted = subprocess.run([
            "gdbus", "emit", "--session",
            "--object-path", "/org/huawei/RuntimeAppMonitor",
            "--signal", "org.huawei.RuntimeAppMonitor.WindowEvent",
            json.dumps(event, ensure_ascii=False, separators=(",", ":")),
        ], text=True, capture_output=True, check=False, timeout=5)
        result["gdbus_returncode"] = emitted.returncode
        result["gdbus_stdout"] = emitted.stdout.strip()
        result["gdbus_stderr"] = emitted.stderr.strip()
        if emitted.returncode != 0:
            result["valid"] = False
            result["reasons"].append("gdbus signal emission failed")
    write_json(args.output, result)
    print(args.output)
    return 0 if result["valid"] else 10


def _snapshot_for_cli(cgroup: Path, apps: list[str], label: str) -> dict[str, Any]:
    _need_bound()
    # The fixture directory is not needed to query existing X11 window classes.
    specs = ACCEPT.app_specs(Path("/tmp/parp-r8-snapshot"))
    return snapshot(cgroup, apps, specs, label)


def command_snapshot(args: Any) -> int:
    specs = ACCEPT.app_specs(Path("/tmp/parp-r8-snapshot"))
    payload = snapshot(
        args.cgroup, args.apps.split("|"), specs, args.label,
        include_llm=bool(getattr(args, "include_llm", False)),
    )
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
    if configured_scenario(config) == R12_SCENARIO:
        specs = ACCEPT.app_specs(Path("/tmp/parp-r12-snapshot"))
        for app in apps:
            owned_window = _r12_app_window(args.cgroup, app, specs[app])
            rows[app]["window_ids"] = [owned_window] if owned_window else []
            rows[app]["window_alive"] = bool(owned_window)
            if not owned_window:
                reasons.append(f"{app}: no window owned by its experiment scope")
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
        expected_score = int(
            config["r8_oom"]["aggressor_oom_score_adj"]
            if app == config["r8_oom"]["aggressor_app"]
            else config["r8_oom"]["victim_oom_score_adj"]
        )
        processes = list(row.get("processes", []))
        if not processes:
            reasons.append(f"{app}: no PID in scope")
        for process in processes:
            if process.get("oom_score_adj") != expected_score:
                reasons.append(f"{app}: PID {process.get('pid')} OOM score mismatch")
    if total < int(config["r8_oom"]["minimum_total_working_set_mib"]) * MIB:
        reasons.append("aggregate application working set below configured gate")
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
            if app == config["r8_oom"]["aggressor_app"]
            else config["r8_oom"]["victim_oom_score_adj"]
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


def _r12_window(profile: str) -> str | None:
    found = subprocess.run(
        ["xdotool", "search", "--onlyvisible", "--name", "PARP R8"],
        text=True, capture_output=True, check=False, timeout=5,
    )
    for window_id in found.stdout.splitlines():
        if not window_id.isdigit():
            continue
        prop = subprocess.run(
            ["xprop", "-id", window_id, "_NET_WM_PID"],
            text=True, capture_output=True, check=False, timeout=5,
        )
        match = re.search(r"=\s*(\d+)", prop.stdout)
        if match:
            try:
                command = (Path("/proc") / match.group(1) / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
            except OSError:
                continue
            if profile in command:
                return window_id
    return None


def _r12_victims(before: dict[str, Any], trace_path: Path, aggressor: str = "FIREFOX") -> set[str]:
    owners = _pid_map(before)
    return {
        str(owners[mark["pid"]]["app"])
        for mark in _trace_victims(trace_path)
        if mark["pid"] in owners and owners[mark["pid"]]["app"] != aggressor
    }


def _r12_press_window(window_id: str) -> None:
    subprocess.run(["xdotool", "windowactivate", "--sync", window_id], check=True, timeout=5)
    active = subprocess.run(
        ["xdotool", "getactivewindow"], text=True, capture_output=True,
        check=True, timeout=5,
    ).stdout.strip()
    if active != window_id:
        raise RuntimeError(f"pressure window focus mismatch: expected {window_id}, active {active}")
    # The R8 page has an explicit 'n' handler for the same 64 MiB operation.
    # A centered click can land above the button on compact VM displays.
    subprocess.run(["xdotool", "key", "--clearmodifiers", "n"], check=True, timeout=5)


def _r12_pressure_lane_owned(cgroup: Path, window_id: str) -> bool:
    prop = subprocess.run(
        ["xprop", "-id", window_id, "_NET_WM_PID"],
        text=True, capture_output=True, check=False, timeout=5,
    )
    match = re.search(r"=\s*(\d+)", prop.stdout)
    if not match:
        return False
    try:
        path = (Path("/proc") / match.group(1) / "cgroup").read_text(encoding="ascii")
    except OSError:
        return False
    return f"/{cgroup.name}/automation-firefox.scope" in path


def command_r12_pressure(args: Any) -> int:
    """Touch 64 MiB per real browser click and stop at the third distinct OOM."""
    before = read_json(args.before)
    maximum = int(args.maximum_mib)
    if maximum <= 0 or maximum % 64:
        raise ValueError("R12 maximum pressure must be a positive multiple of 64 MiB")
    lanes: dict[str, str] = {}
    for lane in ("A", "B"):
        profile = f"firefox-pressure-{lane.lower()}-profile"
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            window = _r12_window(profile)
            if window and "READY 0/" in subprocess.run(
                ["xdotool", "getwindowname", window], text=True, capture_output=True,
                check=False, timeout=5,
            ).stdout:
                lanes[lane] = window
                break
            time.sleep(0.2)
        if lane not in lanes:
            raise RuntimeError(f"R12 browser pressure lane {lane} did not become ready")
        if not _r12_pressure_lane_owned(args.cgroup, lanes[lane]):
            raise RuntimeError(f"R12 browser pressure lane {lane} left the Firefox experiment scope")
    chunks: list[dict[str, Any]] = []
    committed = 0
    failure = ""
    try:
        for index in range(1, maximum // 64 + 1):
            lane = "A" if index % 2 else "B"
            window = lanes[lane]
            target = ((index + 1) // 2 if lane == "A" else index // 2) * 64
            previous_title = subprocess.run(
                ["xdotool", "getwindowname", window], text=True, capture_output=True,
                check=False, timeout=5,
            ).stdout.strip()
            _r12_press_window(window)
            deadline = time.monotonic() + 90
            fallback_at = time.monotonic() + 5
            input_retries = 0
            expected = f"PARP R8 ALLOCATED {target}/"
            while time.monotonic() < deadline:
                title = subprocess.run(
                    ["xdotool", "getwindowname", window], text=True, capture_output=True,
                    check=False, timeout=5,
                ).stdout.strip()
                if title.startswith(expected):
                    break
                if "FAILED" in title:
                    raise RuntimeError(f"browser allocation failed: {title}")
                if title != previous_title and title.startswith("PARP R8 ALLOCATED"):
                    raise RuntimeError(f"browser allocation jumped past {target} MiB on lane {lane}: {title}")
                if time.monotonic() >= fallback_at and input_retries < 2:
                    current = read_int(args.cgroup / "memory.current") or 0
                    cap = read_int(args.cgroup / "memory.max") or 0
                    if title == previous_title and cap - current > 256 * MIB:
                        _r12_press_window(window)
                        input_retries += 1
                    fallback_at = time.monotonic() + 5
                time.sleep(0.05)
            else:
                raise RuntimeError(f"browser did not confirm {target} MiB on lane {lane}; last title: {title}")
            committed += 64
            time.sleep(0.3)
            victims = _r12_victims(before, args.trace)
            chunks.append({"index": index, "lane": lane, "committed_mib": committed,
                           "input_retries": input_retries,
                           "victims": sorted(victims), "timestamp_ns": time.time_ns()})
            if len(victims) > 4 or (args.adaptive_stop and len(victims) >= 3):
                break
    except (RuntimeError, subprocess.SubprocessError, OSError) as exc:
        failure = str(exc)
    payload = {
        "schema_version": 1, "pressure_requested_bytes": committed * MIB,
        "pressure_committed_bytes": committed * MIB, "pressure_complete": committed > 0 and not failure,
        "pressure_chunk_bytes": 64 * MIB, "maximum_bytes": maximum * MIB,
        "adaptive_stop": bool(args.adaptive_stop), "victims_at_stop": sorted(_r12_victims(before, args.trace)),
        "chunks": chunks, "failure": failure,
    }
    write_json(args.output, payload)
    print(args.output)
    return 0 if payload["pressure_complete"] else 10


def _r12_app_window(cgroup: Path, app: str, spec: Any) -> str | None:
    scope = f"automation-{app.lower().replace('_', '-')}.scope"
    for window_id in _window_ids(spec.window_class):
        if app == "THUNDERBIRD":
            title = subprocess.run(
                ["xdotool", "getwindowname", window_id], text=True,
                capture_output=True, check=False, timeout=5,
            ).stdout
            if "PARP local message" not in title:
                continue
        prop = subprocess.run(
            ["xprop", "-id", window_id, "_NET_WM_PID"],
            text=True, capture_output=True, check=False, timeout=5,
        )
        match = re.search(r"=\s*(\d+)", prop.stdout)
        if not match:
            continue
        try:
            path = (Path("/proc") / match.group(1) / "cgroup").read_text(encoding="ascii")
            command = (Path("/proc") / match.group(1) / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
        except OSError:
            continue
        if app == "FIREFOX" and ("/firefox-profile" not in command or "firefox-pressure" in command):
            continue
        if f"/{cgroup.name}/{scope}" in path:
            return window_id
    return None


def _r12_capture(window_id: str, path: Path) -> bool:
    try:
        result = subprocess.run(
            ["import", "-window", window_id, str(path)],
            check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15,
        )
        return result.returncode == 0 and path.is_file()
    except (OSError, subprocess.TimeoutExpired):
        return False


def _r12_visual_change(before: Path, after: Path) -> float | None:
    try:
        from PIL import Image, ImageChops
        with Image.open(before) as first, Image.open(after) as second:
            left = first.convert("RGB")
            right = second.convert("RGB")
            if left.size != right.size:
                return 1.0
            difference = ImageChops.difference(left, right).convert("L")
            histogram = difference.histogram()
            return sum(histogram[12:]) / max(1, left.width * left.height)
    except (OSError, ImportError):
        return None


def command_r12_recovery(args: Any) -> int:
    config = read_json(args.config)
    before = read_json(args.before)
    after = read_json(args.after)
    victims = _r12_victims(before, args.trace)
    run_dir = args.run_dir
    specs = _r8_specs(run_dir, 0, 0, firefox_pressure=False, r12_mail=True)
    shots = run_dir / "recovery-screenshots"
    shots.mkdir(parents=True, exist_ok=True)
    released: list[str] = []
    lane_ownership: dict[str, bool] = {}
    for lane in ("A", "B"):
        window = _r12_window(f"firefox-pressure-{lane.lower()}-profile")
        lane_ownership[lane] = bool(window and _r12_pressure_lane_owned(args.cgroup, window))
        if window:
            subprocess.run(["xdotool", "windowclose", window], check=False, timeout=5)
            released.append(window)
    for window in released:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            still_open = subprocess.run(
                ["xdotool", "getwindowname", window],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                check=False, timeout=5,
            ).returncode == 0
            if not still_open:
                break
            time.sleep(0.1)
        else:
            # Closing a WebKit top-level window can leave its X client alive
            # and focused. The pressure phase is finished, so terminate that
            # window before testing the surviving workset browser.
            subprocess.run(["xdotool", "windowkill", window], check=False, timeout=5)
    limit = read_int(args.cgroup / "memory.max") or 0
    deadline = time.monotonic() + 30
    while limit and (read_int(args.cgroup / "memory.current") or 0) > int(limit * 0.8) and time.monotonic() < deadline:
        time.sleep(0.25)
    rows: dict[str, Any] = {}
    env = TRAINED.gui_environment()
    for app in config["apps"]:
        spec = specs[app]
        killed = app in victims
        start_ns = time.monotonic_ns()
        launch_error = ""
        if killed:
            unit = f"automation-{app.lower().replace('_', '-')}.scope"
            # The GUI process may be OOM-killed while helper processes keep
            # its scope active. Reusing the unit name then fails silently and
            # makes a healthy relaunch look like an unresponsive application.
            active = subprocess.run(
                ["systemctl", "--user", "is-active", "--quiet", unit],
                check=False, timeout=5,
            ).returncode == 0
            if active:
                stopped = subprocess.run(
                    ["systemctl", "--user", "stop", unit],
                    text=True, capture_output=True, check=False, timeout=20,
                )
                if stopped.returncode != 0:
                    launch_error = f"old scope stop failed: {stopped.stderr.strip()}"
            subprocess.run(["systemctl", "--user", "reset-failed", unit], check=False, timeout=10)
            command = [
                "systemd-run", "--user", "--scope", f"--unit={unit}",
                f"--slice={config['slice']}",
                sys.executable, str(RUNNER), "oom-score-exec", "--score",
                str(config["r8_oom"]["victim_oom_score_adj"]), "--", *shlex.split(spec.command),
            ]
            if not launch_error:
                try:
                    log = (run_dir / f"relaunch-{app.lower()}.log").open("w", encoding="utf-8")
                    with log:
                        process = subprocess.Popen(command, env=env, stdout=log, stderr=subprocess.STDOUT)
                    time.sleep(0.5)
                    if process.poll() not in (None, 0):
                        launch_error = f"systemd-run exited {process.returncode}; see relaunch log"
                except OSError as exc:
                    launch_error = str(exc)
        window = None
        window_deadline = time.monotonic() + 45
        while time.monotonic() < window_deadline:
            window = _r12_app_window(args.cgroup, app, spec)
            if window:
                break
            time.sleep(0.2)
        responsive = False
        change: float | None = None
        direct_target = False
        active_before = ""
        if window:
            first = shots / f"{app.lower()}-before.png"
            second = shots / f"{app.lower()}-after.png"
            try:
                activated = subprocess.run(
                    ["xdotool", "windowactivate", window], check=False, timeout=5,
                ).returncode == 0
                focus_deadline = time.monotonic() + 8
                while activated and time.monotonic() < focus_deadline:
                    active_before = subprocess.run(
                        ["xdotool", "getactivewindow"], text=True, capture_output=True,
                        check=False, timeout=5,
                    ).stdout.strip()
                    if active_before == window:
                        break
                    time.sleep(0.1)
                else:
                    activated = False
            except subprocess.TimeoutExpired:
                activated = False
            if app == "FIREFOX" and not activated:
                # A WebKit child can retain X focus after the pressure
                # windows close. A real click in the workset page can return
                # focus to its verified top-level window before reload.
                try:
                    subprocess.run(["xdotool", "windowraise", window], check=False, timeout=5)
                    subprocess.run([
                        "xdotool", "mousemove", "--window", window, "400", "220", "click", "1",
                    ], check=False, timeout=5)
                    focus_deadline = time.monotonic() + 5
                    while time.monotonic() < focus_deadline:
                        active_before = subprocess.run(
                            ["xdotool", "getactivewindow"], text=True, capture_output=True,
                            check=False, timeout=5,
                        ).stdout.strip()
                        if active_before == window:
                            activated = True
                            break
                        time.sleep(0.1)
                except (OSError, subprocess.TimeoutExpired):
                    pass
            time.sleep(0.3)
            captured = _r12_capture(window, first)
            if app == "FIREFOX":
                # A WebKit content process can be reclaimed while the browser
                # UI remains alive; reloading the local page tests both.
                direct_target = not activated
                command = ["xdotool", "key", "--clearmodifiers", "ctrl+r"]
                if direct_target:
                    command = ["xdotool", "key", "--window", window, "--clearmodifiers", "ctrl+r"]
            elif app == "FILES":
                command = ["xdotool", "key", "--clearmodifiers", "ctrl+l"]
            elif app == "EVINCE":
                command = ["xdotool", "mousemove", "--window", window, "110", "150",
                           "click", "--repeat", "2", "--delay", "100", "1"]
            elif app == "CALENDAR":
                command = ["xdotool", "mousemove", "--window", window, "180", "22", "click", "1"]
            else:
                if activated and spec.operation_key == "Page_Down":
                    subprocess.run(["xdotool", "key", "--clearmodifiers", "Home"], check=False, timeout=5)
                    time.sleep(0.2)
                command = ["xdotool", "key", "--clearmodifiers", spec.operation_key]
            try:
                typed = (activated or direct_target) and subprocess.run(command, check=False, timeout=5).returncode == 0
            except (OSError, subprocess.TimeoutExpired):
                typed = False
            operation_ns = time.monotonic_ns()
            response_ms: float | None = None
            # The calculator appends one visible digit in its program view;
            # this covers about 0.0259% of the window on the Native desktop.
            minimum_change = 0.0002 if app == "CALCULATOR" else 0.0005
            response_deadline = time.monotonic() + (15 if app == "FIREFOX" else 8)
            while (activated or direct_target) and captured and time.monotonic() < response_deadline:
                if _r12_capture(window, second):
                    change = _r12_visual_change(first, second)
                    if change is not None and change >= minimum_change:
                        response_ms = (time.monotonic_ns() - operation_ns) / 1e6
                        break
                time.sleep(0.2)
            try:
                active = subprocess.run(
                    ["xdotool", "getactivewindow"], text=True, capture_output=True,
                    check=False, timeout=5,
                ).stdout.strip()
            except (OSError, subprocess.TimeoutExpired):
                active = ""
            responsive = typed and response_ms is not None and (
                active == window or app == "EVINCE" or direct_target
            )
        else:
            response_ms = None
        rows[app] = {
            "oom_victim": killed, "pre_pressure_alive": bool(before.get("apps", {}).get(app, {}).get("scope_alive")),
            "after_pressure_alive": bool(after.get("apps", {}).get(app, {}).get("scope_alive")),
            "relaunch_error": launch_error, "window_id": window,
            "recovery_ms": (time.monotonic_ns() - start_ns) / 1e6,
            "response_ms": response_ms, "visual_change_ratio": change,
            "input_delivery": "direct_target" if direct_target else "active_window",
            "focus_observed_window_id": active_before,
            "responsive": responsive,
        }
    payload = {
        "schema_version": 1,
        "valid": all(row["responsive"] for row in rows.values()) and all(lane_ownership.values()),
        "victims": sorted(victims), "pressure_windows_closed": released,
        "pressure_lanes_alive_at_release": lane_ownership,
        "memory_current_after_release": read_int(args.cgroup / "memory.current"),
        "apps": rows,
    }
    write_json(args.output, payload)
    print(args.output)
    return 0 if payload["valid"] else 11


def command_llm_pressure_record(args: Any) -> int:
    config = read_json(args.config)
    validate_config(config)
    state = read_json(args.state)
    llm = config["r8_llm"]
    requested = int(llm["model_size_bytes"])
    resident = int(state.get("resident_after_load_bytes", 0) or 0)
    peak_delta = int(state.get("peak_memory_delta_bytes", 0) or 0)
    complete = bool(
        state.get("status") == "FIRST_TOKEN_COMPLETE"
        and state.get("runtime_sha256") == llm.get("runtime_sha256")
        and state.get("gguf_sha256") == llm.get("gguf_sha256")
        and int(state.get("model_size_bytes", 0)) == requested
        and state.get("fadvise_dontneed_applied") is True
        and state.get("cold_cache_gate", {}).get("valid") is True
        and state.get("no_mmap") is True
        and state.get("oom_score_gate", {}).get("valid") is True
        and resident >= int(llm["minimum_resident_delta_mib"]) * MIB
    )
    payload = {
        "schema_version": 1,
        "pressure_requested_bytes": requested,
        # A successful llama.cpp READY state with --no-mmap proves the complete
        # model was loaded.  The separately reported resident/peak deltas retain
        # the actual cgroup measurement rather than pretending file size is RSS.
        "pressure_committed_bytes": requested if complete else 0,
        "pressure_complete": complete,
        "pressure_kind": "llama_cpp_no_mmap_weight_load",
        "llm_load_complete": state.get("status") == "FIRST_TOKEN_COMPLETE",
        "llm_first_token_complete": state.get("status") == "FIRST_TOKEN_COMPLETE",
        "llm_resident_after_load_bytes": resident,
        "llm_peak_memory_delta_bytes": peak_delta,
        "llm_state": state,
    }
    write_json(args.output, payload)
    print(args.output)
    return 0 if complete else 12


def _pid_map(snapshot_payload: dict[str, Any]) -> dict[int, dict[str, Any]]:
    result: dict[int, dict[str, Any]] = {}
    for app, row in snapshot_payload.get("apps", {}).items():
        for process in row.get("processes", []):
            pid = int(process.get("pid", 0) or 0)
            if pid:
                result[pid] = {"app": app, **process}
        # oom/mark_victim can report a non-leader thread TID (for example
        # LibreOffice's PipeIPC thread), while cgroup.procs contains only
        # thread-group leaders.  cgroup.threads is sampled before pressure so
        # those marks remain attributable after the group has been killed.
        for tid in row.get("threads", []):
            tid = int(tid or 0)
            if tid:
                result.setdefault(tid, {"app": app, "pid": tid, "thread": True})
    llm = snapshot_payload.get("llm", {})
    for process in llm.get("processes", []):
        pid = int(process.get("pid", 0) or 0)
        if pid:
            result[pid] = {"app": "LLM", **process}
    for tid in llm.get("threads", []):
        tid = int(tid or 0)
        if tid:
            result.setdefault(tid, {"app": "LLM", "pid": tid, "thread": True})
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
    scenario_name = configured_scenario(config)
    llm_mode = scenario_name == LLM_SCENARIO
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
    if scenario_name != R12_SCENARIO and not prediction_gate.get("valid"):
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
    all_pids.update(_pid_map(after))
    llm_state = pressure.get("llm_state", {}) if llm_mode else {}
    for key in ("wrapper_pid", "server_pid"):
        pid = int(llm_state.get(key, 0) or 0)
        if pid:
            all_pids.setdefault(pid, {"app": "LLM", "pid": pid})
    victims: set[str] = set()
    unknown_marks: list[dict[str, Any]] = []
    aggressor_marks: list[dict[str, Any]] = []
    score_mismatches: list[dict[str, Any]] = []
    r8 = config["r8_oom"]
    aggressor_app = str(r8["aggressor_app"])
    aggressor_label = "Firefox" if aggressor_app == "FIREFOX" else aggressor_app
    for mark in trace_victims:
        owner = all_pids.get(int(mark["pid"]))
        if owner is None:
            unknown_marks.append(mark)
            continue
        app = str(owner["app"])
        expected = int(r8["aggressor_oom_score_adj"] if app == aggressor_app else r8["victim_oom_score_adj"])
        if mark.get("oom_score_adj") != expected:
            score_mismatches.append(mark)
        if app == aggressor_app:
            aggressor_marks.append(mark)
        else:
            victims.add(app)
    if unknown_marks:
        reasons.append("host or unknown OOM mark")
    if aggressor_marks:
        reasons.append(f"{aggressor_label} aggressor was selected as an OOM victim")
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
        # The desktop may contain unrelated windows of the same application.
        # R12 survival must be proven by a PID in this application's scope.
        survived = bool(after_row.get("scope_alive")) if scenario_name == R12_SCENARIO else bool(
            after_row.get("scope_alive") or after_row.get("window_alive")
        )
        disappeared = bool(before_row.get("scope_alive") and not survived)
        if disappeared and app not in victims:
            untraced_disappearances.append(app)
        app_rows[app] = {
            "before": before_row, "after": after_row, "survived": survived,
            "oom_victim": app in victims, "disappeared": disappeared,
        }
    if untraced_disappearances:
        reasons.append("application disappeared without an OOM trace")
    if baseline_only:
        aggressor_survived = True
    elif llm_mode:
        llm_after = after.get("llm", {})
        aggressor_survived = bool(
            llm_after.get("scope_alive")
            and llm_state.get("status") == "FIRST_TOKEN_COMPLETE"
        )
    else:
        aggressor_survived = bool(app_rows["FIREFOX"]["survived"])
    if not aggressor_survived:
        reasons.append(f"{aggressor_label} aggressor did not survive")
    if not baseline_only:
        requested = int(config["r8_llm"]["model_size_bytes"]) if llm_mode else (
            int(pressure.get("pressure_requested_bytes", 0)) if pressure.get("adaptive_stop")
            else int(r8["burst_mib"]) * MIB
        )
        if int(pressure.get("pressure_requested_bytes", -1)) != requested:
            reasons.append("pressure request does not equal the frozen aggressor contract")
        if not pressure.get("pressure_complete") or int(pressure.get("pressure_committed_bytes", -1)) != requested:
            reasons.append(f"{aggressor_label} pressure was not fully committed")
        if llm_mode:
            llm = config["r8_llm"]
            if not pressure.get("llm_load_complete") or not pressure.get("llm_first_token_complete"):
                reasons.append("LLM model load or first-token inference did not complete")
            if int(pressure.get("llm_resident_after_load_bytes", 0)) < int(llm["minimum_resident_delta_mib"]) * MIB:
                reasons.append("LLM resident memory delta is below the frozen gate")
            if llm_state.get("runtime_sha256") != llm.get("runtime_sha256") or llm_state.get("gguf_sha256") != llm.get("gguf_sha256"):
                reasons.append("LLM runtime or GGUF hash differs from the frozen contract")
            if llm_state.get("oom_score_gate", {}).get("valid") is not True:
                reasons.append("LLM aggressor OOM score/cgroup gate did not pass")
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
    recovery: dict[str, Any] = {}
    if scenario_name == R12_SCENARIO:
        if not baseline_only:
            if int(parent_before.get("oom", 0)) or int(parent_before.get("oom_kill", 0)):
                reasons.append("OOM occurred before R12 pressure started")
            if policy == "current_kernel" and not 3 <= len(victims) <= 4:
                reasons.append(f"R12 requires 3-4 distinct OOM applications; got {len(victims)}")
            if not all(before.get("apps", {}).get(app, {}).get("scope_alive") and
                       before.get("apps", {}).get(app, {}).get("window_alive") for app in config["apps"]):
                reasons.append("not all 12 applications were live before pressure")
            try:
                recovery = read_json(run_dir / "r12-recovery.json")
            except (FileNotFoundError, OSError, json.JSONDecodeError):
                reasons.append("R12 recovery evidence missing")
            else:
                if not recovery.get("valid"):
                    reasons.append("R12 recovery incomplete")
    result = {
        "status": "VALID" if not reasons else "INVALID", "valid": not reasons,
        "invalid_reasons": list(dict.fromkeys(reasons)), "scenario": scenario_name, "policy": policy,
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
        "llm": {
            "enabled": llm_mode,
            "state": llm_state,
            "scope_after": after.get("llm", {}) if llm_mode else {},
            "load_complete": bool(pressure.get("llm_load_complete")),
            "first_token_complete": bool(pressure.get("llm_first_token_complete")),
            "resident_after_load_bytes": int(pressure.get("llm_resident_after_load_bytes", 0)),
            "peak_memory_delta_bytes": int(pressure.get("llm_peak_memory_delta_bytes", 0)),
        },
        "policy_before": policy_before, "policy_after": policy_after,
        "recovery": recovery,
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


def _restart_runtime_monitor_for_round(timeout_seconds: float = 90.0) -> dict[str, Any]:
    """Give every R8 round an independent desktop/LSTM event session."""
    unit = "parp-runtime-monitor.service"
    restarted = ACCEPT.run(
        ["systemctl", "--user", "restart", unit], timeout=max(5.0, timeout_seconds),
    )
    payload: dict[str, Any] = {
        "schema_version": 1,
        "valid": False,
        "unit": unit,
        "contract": "systemd_restart_before_each_r8_round",
        "restart_returncode": restarted.returncode,
        "restart_stderr": restarted.stderr.strip(),
        "main_pid": 0,
        "active_state": "",
        "sub_state": "",
    }
    if restarted.returncode != 0:
        return payload
    deadline = time.monotonic() + timeout_seconds
    stable_pid = 0
    stable_observations = 0
    while time.monotonic() < deadline:
        shown = ACCEPT.run([
            "systemctl", "--user", "show", unit,
            "-p", "MainPID", "-p", "ActiveState", "-p", "SubState",
        ], timeout=5)
        values: dict[str, str] = {}
        for line in shown.stdout.splitlines():
            key, separator, value = line.partition("=")
            if separator:
                values[key] = value
        try:
            pid = int(values.get("MainPID", "0"))
        except ValueError:
            pid = 0
        active = values.get("ActiveState", "")
        sub = values.get("SubState", "")
        payload.update({"main_pid": pid, "active_state": active, "sub_state": sub})
        if pid > 0 and active == "active" and sub == "running":
            if pid == stable_pid:
                stable_observations += 1
            else:
                stable_pid = pid
                stable_observations = 1
            if stable_observations >= 3:
                payload["valid"] = True
                return payload
        else:
            stable_pid = 0
            stable_observations = 0
        time.sleep(1.0)
    payload["reason"] = "runtime monitor did not remain active with one PID"
    return payload


def _record_pid_history(cgroup: Path, apps: list[str], history: dict[int, dict[str, Any]]) -> None:
    for app in apps:
        row = _scope_row(_scope_path(cgroup, app))
        for process in row.get("processes", []):
            pid = int(process.get("pid", 0) or 0)
            if pid:
                history[pid] = {"app": app, **process}
        for tid in row.get("threads", []):
            tid = int(tid or 0)
            if tid:
                history.setdefault(tid, {"app": app, "pid": tid, "thread": True})
    llm_row = _scope_row(_scope_path(cgroup, "LLM_AGGRESSOR"))
    for process in llm_row.get("processes", []):
        pid = int(process.get("pid", 0) or 0)
        if pid:
            history[pid] = {"app": "LLM", **process}
    for tid in llm_row.get("threads", []):
        tid = int(tid or 0)
        if tid:
            history.setdefault(tid, {"app": "LLM", "pid": tid, "thread": True})


def run_one(
    config: dict[str, Any], policy: str, seed: int, run_dir: Path,
    expected_plan: dict[str, Any] | None = None, *, baseline_only: bool = False,
    burst_override_mib: int | None = None, allow_unfrozen: bool = False,
    adaptive_stop: bool = False,
) -> dict[str, Any]:
    _need_bound()
    run_dir = run_dir.resolve()
    validate_config(config, require_frozen=not allow_unfrozen)
    scenario_name = configured_scenario(config)
    llm_mode = scenario_name == LLM_SCENARIO
    allowed = {"native_kernel", "bin_lstm", "current_kernel"} if scenario_name == R12_SCENARIO else {"native_kernel", "bin_lstm"}
    if policy not in allowed:
        raise ValueError(f"{scenario_name} does not support policy {policy}")
    run_dir.mkdir(parents=True, exist_ok=False)
    if llm_mode:
        llm_preflight = llm_asset_preflight(config)
        write_json(run_dir / "llm-preflight.json", llm_preflight)
        if llm_preflight["status"] != "READY":
            result = {
                "status": "BLOCKED", "valid": False, "scenario": scenario_name,
                "policy": policy, "seed": seed, "preflight": llm_preflight,
                "invalid_reasons": [
                    "LLM asset preflight failed: " + ",".join(llm_preflight["reasons"]),
                ],
                "run_dir": str(run_dir),
            }
            write_json(run_dir / "run-result.json", result)
            return result
    runtime_reset = _restart_runtime_monitor_for_round()
    write_json(run_dir / "runtime-monitor-reset.json", runtime_reset)
    if not runtime_reset["valid"]:
        result = {
            "status": "BLOCKED", "valid": False, "scenario": scenario_name,
            "policy": policy, "seed": seed,
            "invalid_reasons": ["runtime monitor round reset failed"],
            "runtime_monitor_reset": runtime_reset,
            "run_dir": str(run_dir),
        }
        write_json(run_dir / "run-result.json", result)
        return result
    runtime_pid = int(runtime_reset.get("main_pid", 0) or 0)
    try:
        runtime_command = (Path("/proc") / str(runtime_pid) / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
    except OSError:
        runtime_command = ""
    write_json(run_dir / "environment.json", {
        "kernel_release": os.uname().release,
        "kernel_cmdline": Path("/proc/cmdline").read_text(encoding="utf-8").strip(),
        "runtime_monitor": runtime_reset,
        "runtime_monitor_command": runtime_command,
        "policy_before": ACCEPT.policy_state(),
        "swap": Path("/proc/swaps").read_text(encoding="utf-8"),
    })
    asset_manifest = prepare_assets(config, run_dir)
    write_json(run_dir / "asset-manifest.json", asset_manifest)
    # The in-scenario gate reads this immutable per-round copy, never a path
    # supplied by the caller that could change during a paired run.
    write_json(run_dir / "r8-config.json", config)
    preflight = TRAINED.preflight(config, policy)
    if llm_mode:
        for name, passed in llm_preflight["checks"].items():
            preflight["checks"][f"llm_{name}"] = passed
    else:
        preflight["checks"]["r8_pressure_asset"] = (run_dir / "fixtures" / "oom-pressure.html").is_file()
    preflight["status"] = "READY" if all(preflight["checks"].values()) else "BLOCKED"
    write_json(run_dir / "preflight.json", preflight)
    if preflight["status"] != "READY":
        result = {
            "status": "BLOCKED", "valid": False, "scenario": scenario_name,
            "policy": policy, "seed": seed, "preflight": preflight,
            "run_dir": str(run_dir),
        }
        write_json(run_dir / "run-result.json", result)
        prune_round_working_assets(run_dir)
        return result
    r8 = config["r8_oom"]
    burst_mib = 0 if baseline_only or llm_mode else int(
        burst_override_mib if burst_override_mib is not None else r8["burst_mib"]
    )
    if not llm_mode and burst_mib and burst_mib % 64:
        raise ValueError("R8 burst must be divisible by the 64 MiB browser chunk")
    cap_mib = memory_limit_cap_bytes(memtotal_mib()) // MIB
    configured_max = int(r8.get("memory_max_mib", 0))
    memory_max_mib = configured_max if configured_max > 0 else cap_mib
    if memory_max_mib <= 0 or memory_max_mib * MIB > memory_limit_cap_bytes(memtotal_mib()):
        result = {
            "status": "BLOCKED", "valid": False, "scenario": scenario_name,
            "policy": policy, "seed": seed,
            "invalid_reasons": ["R8 MemoryMax exceeds host calibration cap"],
            "run_dir": str(run_dir),
        }
        write_json(run_dir / "run-result.json", result)
        prune_round_working_assets(run_dir)
        return result
    setup = _setup(config, memory_max_mib=memory_max_mib)
    variant = "bin_apply" if policy == "bin_lstm" else "observe" if policy == "current_kernel" else "native"
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
            config, run_dir, cgroup, seed, policy, burst_mib=burst_mib,
            baseline_only=baseline_only, adaptive_stop=adaptive_stop,
        )
        write_json(run_dir / "scenario.json", scenario)
        write_json(run_dir / "action-plan.json", action_plan)
        if expected_plan is not None and action_plan.get("sha256") != expected_plan.get("sha256"):
            result = {
                "status": "BLOCKED", "valid": False, "scenario": scenario_name, "policy": policy, "seed": seed,
                "run_dir": str(run_dir), "expected_action_plan_sha256": expected_plan.get("sha256"),
                "observed_action_plan_sha256": action_plan.get("sha256"),
                "invalid_reasons": ["action-plan hash differs from Native replay"],
            }
            write_json(run_dir / "run-result.json", result)
            prune_round_working_assets(run_dir)
            return result
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
            "--scenario-id", f"real_{scenario_name}", "--test-slice", str(config["slice"]),
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
        try:
            ACCEPT.cleanup_slice(setup)
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
            cleanup_error = f"CLEANUP_ERROR:{type(exc).__name__}:{exc}"
            abort_reason = f"{cleanup_error}; {abort_reason}" if abort_reason else cleanup_error
            subprocess.run(
                ["systemctl", "--user", "stop", "--no-block", str(setup["slice"])],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False, timeout=10,
            )
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
    prune_round_working_assets(run_dir)
    return result


def command_run(args: Any) -> int:
    config = read_json(args.config)
    validate_config(config, require_frozen=True)
    scenario_name = configured_scenario(config)
    if args.scenario not in {"all", scenario_name}:
        raise ValueError("this configuration only supports R8")
    resume_session = getattr(args, "resume_session", None)
    if resume_session is not None:
        session = Path(resume_session).resolve()
        existing_summary = read_json(session / "summary.json")
        existing_config = read_json(session / "config.json")
        if canonical_sha256(existing_config) != canonical_sha256(config):
            raise ValueError("resume session config differs")
        if existing_summary.get("policy") != args.policy:
            raise ValueError("resume session policy differs")
        results = list(existing_summary.get("runs", []))
    else:
        root = Path(config["output_root"])
        stamp = time.strftime("%Y%m%d_%H%M%S")
        profile = "r8-llm" if scenario_name == LLM_SCENARIO else "r8"
        session = root / f"{profile}-{args.policy}-{stamp}-{os.uname().release}"
        session.mkdir(parents=True, exist_ok=False)
        write_json(session / "config.json", config)
        results = []
    native_runs: dict[int, dict[str, Any]] = {}
    if args.replay_from is not None:
        native_summary = read_json(args.replay_from / "summary.json")
        for row in native_summary.get("runs", []):
            if row.get("scenario") == scenario_name and row.get("valid"):
                native_runs[int(row["seed"])] = row
    replay_seeds = sorted(native_runs)[:int(args.rounds)] if native_runs else []
    attempt = 0
    used_seeds = {int(row["seed"]) for row in results if "seed" in row}
    while sum(1 for row in results if row.get("valid")) < int(args.rounds):
        if replay_seeds:
            completed_seeds = {
                int(row["seed"]) for row in results
                if row.get("valid") and "seed" in row
            }
            remaining = [seed for seed in replay_seeds if seed not in completed_seeds]
            if not remaining:
                break
            seed = remaining[0]
            if sum(1 for row in results if row.get("seed") == seed) >= 8:
                results.append({
                    "status": "BLOCKED", "valid": False, "scenario": scenario_name,
                    "policy": args.policy, "seed": seed,
                    "invalid_reasons": ["eight replay attempts for this seed were invalid"],
                })
                break
        else:
            seed = int(args.seed) + attempt
            attempt += 1
            if seed in used_seeds:
                continue
        used_seeds.add(seed)
        index = len(results) + 1
        expected: dict[str, Any] | None = None
        if args.replay_from is not None:
            native = native_runs.get(seed)
            if native is None:
                result = {
                    "status": "BLOCKED", "valid": False, "scenario": scenario_name, "policy": args.policy,
                    "seed": seed, "invalid_reasons": ["no valid reference round with this seed for replay"],
                }
                results.append(result)
                break
            try:
                expected = read_json(Path(native["run_dir"]) / "action-plan.json")
            except (FileNotFoundError, OSError, json.JSONDecodeError) as exc:
                result = {
                    "status": "BLOCKED", "valid": False, "scenario": scenario_name, "policy": args.policy,
                    "seed": seed, "invalid_reasons": [f"reference action plan unavailable: {exc}"],
                }
                results.append(result)
                break
        run_dir = session / f"round-{index:02d}-{scenario_name}"
        valid_before = sum(1 for row in results if row.get("valid"))
        print(
            f"R8 policy={args.policy} attempt={index} "
            f"valid={valid_before}/{args.rounds} seed={seed}", flush=True,
        )
        result = run_one(config, args.policy, seed, run_dir, expected_plan=expected)
        results.append(result)
        print(f"status={result['status']} output={run_dir}", flush=True)
        if any(str(reason).startswith("CLEANUP_ERROR:") for reason in result.get("invalid_reasons", [])):
            break
        if not result.get("valid") and not args.keep_going:
            break
    summary = {
        "schema_version": 1,
        "status": "COMPLETE" if sum(1 for row in results if row.get("valid")) >= int(args.rounds) else "INCOMPLETE",
        "scenario": scenario_name, "policy": args.policy, "rounds_requested": int(args.rounds),
        "runs": results, "config_sha256": canonical_sha256(frozen_config_contract(config)),
        "replay_from": str(args.replay_from) if args.replay_from else None,
    }
    write_json(session / "summary.json", summary)
    print(session)
    return 0 if summary["status"] == "COMPLETE" else 1


def command_calibrate_r12(args: Any) -> int:
    """Calibrate on the running PARP kernel, then freeze the Native replay inputs."""
    config = read_json(args.config)
    validate_config(config)
    if configured_scenario(config) != R12_SCENARIO or args.policy != "current_kernel":
        raise ValueError("R12 calibration requires current_kernel and the R12 profile")
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    write_json(output / "input-config.json", config)
    baseline_from = getattr(args, "baseline_from", None)
    if baseline_from:
        previous = Path(baseline_from).resolve()
        if canonical_sha256(read_json(previous / "input-config.json")) != canonical_sha256(config):
            raise ValueError("R12 reused working-set config differs")
        baseline = read_json(previous / "no-pressure-workset/run-result.json")
        report_source = previous / "no-pressure-workset"
    else:
        baseline = run_one(
            config, "current_kernel", int(config["r8_oom"]["calibration"]["seed"]),
            output / "no-pressure-workset", baseline_only=True, allow_unfrozen=True,
        )
        report_source = output / "no-pressure-workset"
    if not baseline.get("valid"):
        write_json(output / "calibration-report.json", {"status": "BLOCKED", "reason": "no-pressure workset invalid", "baseline": baseline})
        return 1
    before = read_json(report_source / "r8-before-pressure.json")
    monitor_samples = read_json(report_source / "monitor.json")
    p95 = max(
        int(before["cgroup"]["memory_current"]),
        max((int(row.get("cgroup", {}).get("memory_current") or 0) for row in monitor_samples), default=0),
    )
    cap = memory_limit_cap_bytes(memtotal_mib())
    # GUI process startup varies by hundreds of MiB across rounds. Leave
    # enough room that applications finish their normal work before pressure.
    maximum = memory_limit_from_p95(p95 + 1024 * MIB)
    if maximum > cap:
        write_json(output / "calibration-report.json", {
            "status": "BLOCKED", "reason": "working-set memory limit exceeds host cap",
            "requested_bytes": maximum, "host_cap_bytes": cap,
        })
        return 1
    attempts: list[dict[str, Any]] = []
    selected: dict[str, Any] | None = None
    for index in range(3):
        candidate = copy.deepcopy(config)
        candidate["r8_oom"]["memory_max_mib"] = maximum // MIB
        candidate["r8_oom"]["burst_mib"] = int(config["r8_oom"]["calibration"]["burst_max_mib"])
        result = run_one(
            candidate, "current_kernel", int(config["r8_oom"]["calibration"]["seed"]) + 100 + index,
            output / f"adaptive-{index + 1:02d}", allow_unfrozen=True, adaptive_stop=True,
        )
        attempts.append(result)
        count = int(result.get("distinct_oom_victim_apps", 0))
        if result.get("valid") and 3 <= count <= 4:
            selected = result
            break
        if result.get("host_or_unknown_oom") or not result.get("pressure_complete") or not result.get("aggressor_survived", True):
            break
        if 3 <= count <= 4:
            # The pressure setting already met the OOM target; a recovery
            # probe failure should not change the memory contract.
            continue
        maximum += (-128 if count < 3 else 128) * MIB
        if maximum <= p95 or maximum > cap:
            break
    report: dict[str, Any] = {
        "schema_version": 1, "status": "READY" if selected else "INCONCLUSIVE",
        "baseline": baseline, "attempts": attempts, "working_set_bytes": p95,
        "host_cap_bytes": cap,
    }
    if selected:
        frozen = copy.deepcopy(config)
        frozen["r8_oom"]["memory_max_mib"] = int(read_json(Path(selected["run_dir"]) / "r8-config.json")["r8_oom"]["memory_max_mib"])
        frozen["r8_oom"]["burst_mib"] = int(selected["pressure_committed_bytes"]) // MIB
        frozen_calibration = frozen["r8_oom"]["calibration"]
        frozen_calibration["frozen"] = True
        frozen_calibration["current_kernel_working_set_bytes"] = p95
        frozen_calibration["calibration_run"] = str(selected["run_dir"])
        frozen_calibration["frozen_config_sha256"] = canonical_sha256(frozen_config_contract(frozen))
        validate_config(frozen, require_frozen=True)
        write_json(output / "frozen-config.json", frozen)
        report["frozen_config"] = str(output / "frozen-config.json")
        report["frozen_config_sha256"] = frozen_calibration["frozen_config_sha256"]
    write_json(output / "calibration-report.json", report)
    print(output / "calibration-report.json")
    return 0 if selected else 1


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
    resume_sources = [Path(source).resolve() for source in (args.resume_from or [])]
    explicit_baseline_source = (
        Path(args.baseline_from).resolve() if args.baseline_from else None
    )
    baseline_source = (
        explicit_baseline_source
        if explicit_baseline_source is not None
        else (resume_sources[0] if resume_sources else None)
    )
    reuse_sources = []
    for source in (*resume_sources, baseline_source):
        if source is not None and source not in reuse_sources:
            reuse_sources.append(source)
    for source in reuse_sources:
        try:
            source_config = read_json(source / "input-config.json")
        except (FileNotFoundError, OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"cannot read resume source config: {source}: {exc}") from exc
        if canonical_sha256(source_config) != canonical_sha256(config):
            raise ValueError(f"resume source config differs: {source}")
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
    rerun_from = int(getattr(args, "rerun_candidates_from", 0) or 0)
    for burst in range(start, maximum + 1, step):
        rows: list[dict[str, Any]] = []
        for index in range(1, rounds + 1):
            candidate_config = copy.deepcopy(config)
            candidate_config["r8_oom"]["memory_max_mib"] = requested_limit // MIB
            candidate_config["r8_oom"]["burst_mib"] = burst
            seed = int(calibration.get("seed", 20261001)) + 1000 + (burst - start) // step * rounds + index - 1
            relative_run = Path(f"burst-{burst:04d}-round-{index:02d}")
            resume_run: Path | None = None
            resumed_result: dict[str, Any] | None = None
            for source in (() if rerun_from and burst >= rerun_from else reuse_sources):
                candidate_run = source / relative_run
                candidate_result_path = candidate_run / "run-result.json"
                if not candidate_result_path.is_file():
                    continue
                candidate_result = read_json(candidate_result_path)
                # Invalid/incomplete rounds never satisfy the five-new-round
                # calibration contract.  Re-run that slot in the new output.
                if not candidate_result.get("valid"):
                    continue
                resume_run = candidate_run
                resumed_result = candidate_result
                break
            result: dict[str, Any]
            if resume_run is not None and resumed_result is not None:
                resumed_config = read_json(resume_run / "r8-config.json")
                if canonical_sha256(resumed_config) != canonical_sha256(candidate_config):
                    raise ValueError(f"resume candidate config mismatch: {resume_run}")
                result = resumed_result
                if int(result.get("seed", -1)) != seed:
                    raise ValueError(f"resume candidate seed mismatch: {resume_run}")
            else:
                result = run_one(
                    candidate_config, "native_kernel", seed,
                    output / f"burst-{burst:04d}-round-{index:02d}",
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


def command_calibrate_llm(args: Any) -> int:
    """Freeze MemoryMax around one pinned, real llama.cpp weight-load burst."""
    config = read_json(args.config)
    validate_config(config, require_frozen=False)
    if configured_scenario(config) != LLM_SCENARIO:
        raise ValueError("calibrate-r8-llm requires an r8_llm_weight_load config")
    if args.policy != "native_kernel":
        raise ValueError("R8 LLM calibration is Native-only")
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    write_json(output / "input-config.json", config)
    asset_preflight = llm_asset_preflight(config)
    write_json(output / "llm-preflight.json", asset_preflight)
    if asset_preflight["status"] != "READY":
        report_payload = {
            "schema_version": 1,
            "scenario": LLM_SCENARIO,
            "status": "BLOCKED",
            "reason": "pinned local llama.cpp runtime/GGUF asset preflight failed",
            "llm_preflight": asset_preflight,
            "baseline_runs": [],
            "model_load_runs": [],
        }
        write_json(output / "calibration-report.json", report_payload)
        print(output / "calibration-report.json")
        return 1

    native_preflight = TRAINED.preflight(config, "native_kernel")
    write_json(output / "native-preflight.json", native_preflight)
    if native_preflight["status"] != "READY":
        report_payload = {
            "schema_version": 1,
            "scenario": LLM_SCENARIO,
            "status": "BLOCKED",
            "reason": "Native host preflight failed",
            "llm_preflight": asset_preflight,
            "native_preflight": native_preflight,
            "baseline_runs": [],
            "model_load_runs": [],
        }
        write_json(output / "calibration-report.json", report_payload)
        print(output / "calibration-report.json")
        return 1

    calibration = config["r8_oom"]["calibration"]
    seed_base = int(calibration.get("seed", 20261001))
    baseline_results: list[dict[str, Any]] = []
    baseline_currents: list[int] = []
    for index in range(1, int(calibration["baseline_rounds"]) + 1):
        run_dir = output / f"baseline-{index:02d}"
        result = run_one(
            config, "native_kernel", seed_base + index - 1, run_dir,
            baseline_only=True, allow_unfrozen=True,
        )
        baseline_results.append(result)
        try:
            before = read_json(run_dir / "r8-before-pressure.json")
            baseline_currents.append(int(before["cgroup"]["memory_current"]))
        except (FileNotFoundError, OSError, KeyError, TypeError, ValueError):
            pass
    if (
        len(baseline_currents) != int(calibration["baseline_rounds"])
        or not all(row.get("valid") for row in baseline_results)
    ):
        report_payload = {
            "schema_version": 1, "scenario": LLM_SCENARIO, "status": "BLOCKED",
            "reason": "native no-LLM working-set calibration invalid",
            "llm_preflight": asset_preflight, "baseline_runs": baseline_results,
            "model_load_runs": [],
        }
        write_json(output / "calibration-report.json", report_payload)
        print(output / "calibration-report.json")
        return 1

    p95 = _nearest_rank_p95(baseline_currents)
    requested_limit = memory_limit_from_p95(p95)
    cap = memory_limit_cap_bytes(memtotal_mib())
    if requested_limit > cap:
        report_payload = {
            "schema_version": 1, "scenario": LLM_SCENARIO, "status": "BLOCKED",
            "reason": "native P95-derived MemoryMax exceeds host cap",
            "p95_parent_memory_current_bytes": p95,
            "requested_memory_max_bytes": requested_limit, "host_cap_bytes": cap,
            "llm_preflight": asset_preflight, "baseline_runs": baseline_results,
            "model_load_runs": [],
        }
        write_json(output / "calibration-report.json", report_payload)
        print(output / "calibration-report.json")
        return 1

    candidate_config = copy.deepcopy(config)
    candidate_config["r8_oom"]["memory_max_mib"] = requested_limit // MIB
    model_load_results: list[dict[str, Any]] = []
    for index in range(1, int(calibration["candidate_rounds"]) + 1):
        result = run_one(
            candidate_config, "native_kernel", seed_base + 1000 + index - 1,
            output / f"model-load-round-{index:02d}", allow_unfrozen=True,
        )
        model_load_results.append(result)
    victim_counts = [
        int(row.get("distinct_oom_victim_apps", 0)) for row in model_load_results
    ]
    in_range = sum(1 for count in victim_counts if 1 <= count <= 3)
    too_many = sum(1 for count in victim_counts if count > 3)
    accepted = bool(
        all(row.get("valid") for row in model_load_results)
        and all(row.get("pressure_complete") for row in model_load_results)
        and in_range >= int(calibration["minimum_in_range_rounds"])
        and too_many == 0
    )
    status = "READY" if accepted else "INCONCLUSIVE"
    report_payload = {
        "schema_version": 1, "scenario": LLM_SCENARIO, "status": status,
        "p95_parent_memory_current_bytes": p95,
        "memory_max_bytes": requested_limit, "host_cap_bytes": cap,
        "llm_preflight": asset_preflight, "baseline_runs": baseline_results,
        "model_load_runs": model_load_results, "victim_counts": victim_counts,
        "in_range_rounds": in_range, "too_many_victim_rounds": too_many,
    }
    if accepted:
        frozen = copy.deepcopy(candidate_config)
        frozen_r8 = frozen["r8_oom"]
        frozen_r8["calibration"]["frozen"] = True
        frozen_r8["calibration"]["native_parent_p95_bytes"] = p95
        frozen_r8["calibration"]["report_contract_sha256"] = canonical_sha256({
            "p95": p95,
            "memory_max": requested_limit,
            "llm_contract": asset_preflight["contract"],
            "baseline": [row.get("action_plan_sha256") for row in baseline_results],
            "model_load": [row.get("action_plan_sha256") for row in model_load_results],
        })
        frozen_r8["calibration"].pop("frozen_config_sha256", None)
        frozen_r8["calibration"]["frozen_config_sha256"] = canonical_sha256(
            frozen_config_contract(frozen)
        )
        validate_config(frozen, require_frozen=True)
        write_json(output / "frozen-config.json", frozen)
        report_payload["frozen_config"] = str(output / "frozen-config.json")
        report_payload["frozen_config_sha256"] = frozen_r8["calibration"]["frozen_config_sha256"]
    write_json(output / "calibration-report.json", report_payload)
    print(output / "calibration-report.json")
    return 0 if accepted else 1


def _session_results(root: Path, scenario_name: str = SCENARIO) -> list[dict[str, Any]]:
    summary = read_json(root / "summary.json")
    return [row for row in summary.get("runs", []) if row.get("scenario") == scenario_name]


def report(
    native_root: Path, bin_root: Path, scenario_name: str = SCENARIO,
) -> dict[str, Any]:
    native_rows = {
        int(row["seed"]): row
        for row in _session_results(native_root, scenario_name) if row.get("valid")
    }
    bin_rows = {
        int(row["seed"]): row
        for row in _session_results(bin_root, scenario_name) if row.get("valid")
    }
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
        "schema_version": 1, "scenario": scenario_name, "status": status,
        "valid_pairs": len(selected), "available_valid_pairs": len(pairs),
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


def command_report_llm(args: Any) -> int:
    payload = report(Path(args.native), Path(args.bin), LLM_SCENARIO)
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=False)
    write_json(output / "r8-llm-comparison.json", payload)
    lines = [
        "# R8-LLM 权重加载 OOM 生存对比", "", f"判定：**{payload['status']}**", "",
        f"有效配对：{payload['valid_pairs']}（可用 {payload['available_valid_pairs']}）", "",
        f"Native victim 应用总数：{payload['native_total_victim_apps']}",
        f"Bin victim 应用总数：{payload['bin_total_victim_apps']}",
        f"降幅：{payload['reduction']:.2%}" if payload["reduction"] is not None else "降幅：N/A",
        f"中位数（Native / Bin）：{payload['native_median']} / {payload['bin_median']}", "",
        "仅接受 10 个同 seed 有效配对；两侧必须加载同一哈希的 GGUF、完成首 token，且压力字节一致。",
    ]
    (output / "r8-llm-comparison.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(output / "r8-llm-comparison.json")
    return 0 if payload["status"] == "PASS" else 1
