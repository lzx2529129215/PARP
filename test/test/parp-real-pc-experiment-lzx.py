#!/usr/bin/env python3
"""Run paired Native/PARP validation with real desktop applications. lzx-note"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import importlib.util
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


TEST_DIR = Path(__file__).resolve().parent
TEST_ROOT = TEST_DIR.parent
CONFIG_DEFAULT = TEST_DIR / "parp-real-pc-config-lzx.json"
AUTOMATION = TEST_ROOT / "automation/app_automation.py"
ASSET_BUILDER = TEST_ROOT / "automation/create_real_pc_assets_lzx.py"
PRESSURE = TEST_ROOT / "automation/oom_threshold_pressure_lzx.py"
FIXTURE = TEST_DIR / "memory-fixture-lzx.py"
REUSE_LAUNCHER = TEST_ROOT / "automation/launch_app_with_reuse_lzx.py"
SUBSTITUTION_FIXTURE = TEST_DIR / "reclaim-substitution-fixture-lzx.py"  # lzx-note
SUBSTITUTION_LAUNCHER = TEST_ROOT / "automation/launch_app_with_substitution_lzx.py"  # lzx-note
TRAINED_RUNNER = TEST_DIR / "parp-trained-sequence-experiment-lzx.py"
TRAINED_EVIDENCE = TEST_DIR / "parp-trained-sequence-evidence-lzx.py"
MIB = 1024 * 1024
DIRTY_SYSCTLS = {
    "dirty_background_bytes": Path("/proc/sys/vm/dirty_background_bytes"),
    "dirty_bytes": Path("/proc/sys/vm/dirty_bytes"),
    "dirty_background_ratio": Path("/proc/sys/vm/dirty_background_ratio"),
    "dirty_ratio": Path("/proc/sys/vm/dirty_ratio"),
}  # lzx-note
LAPTOP_MODE_SYSCTL = Path("/proc/sys/vm/laptop_mode")  # lzx-note
SUBSTITUTION_SCENARIOS = {
    "cold_dirty_preserve_hot_clean",
    "cold_writeback_gate_hot_reuse",
}  # lzx-note
APP_NATIVE_SCENARIOS = {
    "r1_app_cold_retire",
    "r2_app_predicted_return",
    "r3_app_source_distribution",
    "r4_app_dirty_substitution",
    "r5_app_writeback_gate",
    "r6_app_serial_major_reuse",
    "r7_app_fairness_misprediction",
}  # lzx-note: real GUI actions create and revisit the measured working sets.
APP_NATIVE_DIRTY_SCENARIOS = {
    "r4_app_dirty_substitution",
    "r5_app_writeback_gate",
}  # lzx-note
SERIAL_REUSE_SCENARIO = "r6_app_serial_major_reuse"  # lzx-note
FAIRNESS_SCENARIO = "r7_app_fairness_misprediction"  # lzx-note


def load_module(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


TRAINED = load_module("parp_trained_real_pc_adapter", TRAINED_RUNNER)
ACCEPT = TRAINED.ACCEPT
R8 = load_module("parp_r8_oom_survival_adapter", TEST_DIR / "parp-r8-oom-survival-lzx.py")
R8.bind(TRAINED, ACCEPT, Path(__file__).resolve())


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
    values: dict[str, int] = {}
    try:
        for line in path.read_text(encoding="ascii").splitlines():
            fields = line.split()
            if len(fields) == 2:
                values[fields[0]] = int(fields[1])
    except (FileNotFoundError, PermissionError, OSError, ValueError):
        pass
    return values


def read_psi(path: Path) -> dict[str, float | int]:
    """Read cgroup/system PSI averages and cumulative stall microseconds. lzx-note"""
    values: dict[str, float | int] = {}
    try:
        for line in path.read_text(encoding="ascii").splitlines():
            fields = line.split()
            if not fields:
                continue
            prefix = fields[0]
            for item in fields[1:]:
                if "=" not in item:
                    continue
                key, value = item.split("=", 1)
                values[f"{prefix}_{key}"] = int(value) if key == "total" else float(value)
    except (FileNotFoundError, PermissionError, OSError, ValueError):
        pass
    return values


def load_config(path: Path) -> dict[str, Any]:
    value = read_json(path)
    if "r8_multi_app_oom_survival" in value.get("scenarios", []):
        R8.validate_config(value)
        return value
    required = {
        "output_root", "asset_root", "slice", "apps", "hot_apps", "cold_apps",
        "trained_history", "trained_history_vocab", "expected_next_vocab",
        "pressure_mib", "reclaim_target_mib", "scenarios", "safety",
    }
    missing = sorted(required - value.keys())
    if missing:
        raise ValueError("real-PC config missing: " + ",".join(missing))
    if set(value["apps"]) != set(value["hot_apps"]) | set(value["cold_apps"]):
        raise ValueError("apps must equal hot_apps union cold_apps")
    if set(value["hot_apps"]) & set(value["cold_apps"]):
        raise ValueError("hot_apps and cold_apps overlap")
    if len(value["trained_history"]) != 5:
        raise ValueError("the active LSTM checkpoint requires five history entries")
    requested_native = APP_NATIVE_SCENARIOS.intersection(value["scenarios"])
    if requested_native:
        native = value.get("app_native", {})
        if str(value.get("pressure_mode", "")) != "app_native_reclaim":
            raise ValueError("R1-R7 require pressure_mode=app_native_reclaim")
        if int(native.get("minimum_total_working_set_mib", 0)) <= 0:
            raise ValueError("R1-R7 require a positive application working-set gate")
        ratio = float(native.get("minimum_reclaim_achieved_ratio", 0))
        if not 0 < ratio <= 1:
            raise ValueError("minimum_reclaim_achieved_ratio must be in (0,1]")
        if APP_NATIVE_DIRTY_SCENARIOS.intersection(requested_native):
            if int(native.get("minimum_cold_costly_reclaim_mib", 0)) <= 0:
                raise ValueError(
                    "R4/R5 require a positive cold costly-reclaim gate"
                )
        if SERIAL_REUSE_SCENARIO in requested_native:
            serial = value.get("serial_reuse", {})
            allocation = int(serial.get("allocation_mib", 0))
            chunk = int(serial.get("chunk_mib", 0))
            if allocation <= 0 or chunk <= 0 or allocation % chunk:
                raise ValueError(
                    "R6 requires positive allocation_mib divisible by chunk_mib"
                )
            if int(serial.get("minimum_native_major_faults", 0)) <= 0:
                raise ValueError("R6 requires a positive Native major-fault gate")
            swappiness = str(serial.get("reclaim_swappiness", ""))
            if swappiness != "max" and not (
                swappiness.isdigit() and 0 <= int(swappiness) <= 200
            ):
                raise ValueError("R6 reclaim_swappiness must be 0..200 or max")
        if FAIRNESS_SCENARIO in requested_native:
            fairness = value.get("fairness_misprediction", {})
            participants = list(fairness.get("participants", ()))
            unexpected = str(fairness.get("unexpected_reuse_app", ""))
            if len(participants) < 3 or not set(participants).issubset(value["apps"]):
                raise ValueError("R7 requires at least three valid participants")
            if unexpected not in participants or unexpected not in value["cold_apps"]:
                raise ValueError("R7 unexpected_reuse_app must be a prediction-cold participant")
            if int(fairness.get("minimum_unexpected_reclaimed_mib", 0)) <= 0:
                raise ValueError("R7 requires a positive unexpected-app reclaim gate")
            if int(fairness.get("minimum_native_unexpected_faults", 0)) <= 0:
                raise ValueError("R7 requires a positive Native unexpected-reuse fault gate")
            swappiness = str(fairness.get("reclaim_swappiness", ""))
            if swappiness != "max" and not (
                swappiness.isdigit() and 0 <= int(swappiness) <= 200
            ):
                raise ValueError("R7 reclaim_swappiness must be 0..200 or max")
    if SUBSTITUTION_SCENARIOS.intersection(value["scenarios"]):
        layout = value.get("substitution_layout", {})
        if set(layout) != set(value["apps"]):
            raise ValueError("substitution_layout must contain exactly one entry per app")
        required_sizes = {"clean_mib", "dirty_mib", "hot_mib"}
        if any(required_sizes - set(sizes) for sizes in layout.values()):
            raise ValueError("each substitution_layout entry needs clean_mib, dirty_mib and hot_mib")
        target = int(value["reclaim_target_mib"])
        cold_clean = sum(int(layout[app]["clean_mib"]) for app in value["cold_apps"])
        cold_dirty = sum(int(layout[app]["dirty_mib"]) for app in value["cold_apps"])
        hot_clean = sum(int(layout[app]["clean_mib"]) for app in value["hot_apps"])
        if not (cold_clean < target <= cold_clean + cold_dirty):
            raise ValueError("substitution contract requires cold_clean < target <= cold_clean+cold_dirty")
        if cold_clean + hot_clean < target:
            raise ValueError("clean-only baseline must be able to reach the reclaim target")
    if {"cold_writeback_gate_hot_reuse", "r5_app_writeback_gate"}.intersection(
        value["scenarios"]
    ):
        gate = value.get("reclaim_writeback_gate", {})
        if int(gate.get("laptop_mode", 0)) <= 0:
            raise ValueError("writeback-gate scenario requires laptop_mode > 0")
        expected_mode = (
            "app_native_reclaim"
            if "r5_app_writeback_gate" in value["scenarios"]
            else "cgroup_writeback_gate"
        )
        if str(value.get("pressure_mode", "")) != expected_mode:
            raise ValueError(
                f"writeback-gate scenario requires {expected_mode} pressure"
            )
    return value


def scenario_reclaim_target_mib(config: dict[str, Any], scenario_name: str) -> int:
    """Return a scenario-specific reclaim target while preserving paired replay."""
    if scenario_name == SERIAL_REUSE_SCENARIO:
        return int(config["serial_reuse"].get("reclaim_target_mib", config["reclaim_target_mib"]))
    if scenario_name == FAIRNESS_SCENARIO:
        return int(
            config["fairness_misprediction"].get(
                "reclaim_target_mib", config["reclaim_target_mib"]
            )
        )
    return int(config["reclaim_target_mib"])  # lzx-note


def controlled_layout(config: dict[str, Any], scenario_name: str) -> dict[str, Any]:
    """Select the page composition without changing the GUI/LSTM sequence.  lzx-note"""
    if scenario_name == "workload_matrix_reclaim":
        layout = config.get("workload_layout", {})
        if layout:
            return layout
    return config.get("reuse_layout", {})


def helper_action(arguments: list[str], label: str) -> dict[str, Any]:
    return {
        "type": "shell",
        "command": shlex.join([sys.executable, str(Path(__file__).resolve()), *arguments]),
        "label": label,
    }


def trained_evidence_action(arguments: list[str], label: str) -> dict[str, Any]:
    return {
        "type": "shell",
        "command": shlex.join([sys.executable, str(TRAINED_EVIDENCE), *arguments]),
        "label": label,
    }


def apply_dirty_writeback_control(config: dict[str, Any]) -> dict[str, int]:
    """Hold controlled dirty pages until reclaim, preserving originals. lzx-note"""
    original = {
        key: int(path.read_text(encoding="utf-8").strip())
        for key, path in DIRTY_SYSCTLS.items()
    }
    control = config["dirty_writeback_control"]
    try:
        ACCEPT.privileged_write(
            DIRTY_SYSCTLS["dirty_background_bytes"],
            int(control["background_mib"]) * MIB,
        )
        ACCEPT.privileged_write(
            DIRTY_SYSCTLS["dirty_bytes"],
            int(control["limit_mib"]) * MIB,
        )
    except Exception:
        restore_dirty_writeback_control(original)
        raise
    return original


def restore_dirty_writeback_control(original: dict[str, int]) -> None:
    if not original:
        return
    # Setting byte controls clears ratio controls and vice versa. Restore the
    # byte controls first, then the ratios, matching the common 0-byte + ratio
    # configuration without leaving a partially restored global state.
    # lzx-note
    if original["dirty_background_bytes"]:
        ACCEPT.privileged_write(
            DIRTY_SYSCTLS["dirty_background_bytes"], original["dirty_background_bytes"],
        )
    else:
        ACCEPT.privileged_write(
            DIRTY_SYSCTLS["dirty_background_ratio"], original["dirty_background_ratio"],
        )
    if original["dirty_bytes"]:
        ACCEPT.privileged_write(DIRTY_SYSCTLS["dirty_bytes"], original["dirty_bytes"])
    else:
        ACCEPT.privileged_write(DIRTY_SYSCTLS["dirty_ratio"], original["dirty_ratio"])


def apply_reclaim_writeback_gate(config: dict[str, Any]) -> dict[str, int]:
    """Force the initial reclaim pass to start with may_writepage=0. lzx-note"""
    original = int(LAPTOP_MODE_SYSCTL.read_text(encoding="ascii").strip())
    requested = int(config["reclaim_writeback_gate"]["laptop_mode"])
    try:
        ACCEPT.privileged_write(LAPTOP_MODE_SYSCTL, requested)
        observed = int(LAPTOP_MODE_SYSCTL.read_text(encoding="ascii").strip())
        if observed != requested:
            raise RuntimeError(
                f"laptop_mode mismatch: requested={requested} observed={observed}"
            )
    except Exception:
        ACCEPT.privileged_write(LAPTOP_MODE_SYSCTL, original)
        raise
    return {"original": original, "requested": requested, "observed": observed}


def restore_reclaim_writeback_gate(state: dict[str, int]) -> None:
    if state:
        ACCEPT.privileged_write(LAPTOP_MODE_SYSCTL, int(state["original"]))


def command_writeback_gate(args: argparse.Namespace) -> int:
    """Capture the gate immediately before pressure, not only at setup. lzx-note"""
    observed = read_int(LAPTOP_MODE_SYSCTL)
    payload = {
        "schema_version": 1,
        "valid": observed == args.expected_laptop_mode and observed is not None,
        "expected_laptop_mode": args.expected_laptop_mode,
        "observed_laptop_mode": observed,
        "captured_realtime_ns": time.time_ns(),
    }
    write_json(args.output, payload)
    print(args.output)
    return 0 if payload["valid"] else 9


def resolve_scope(root: Path, name: str) -> Path | None:
    matches = [path for path in root.rglob(name) if path.is_dir()]
    return matches[0] if len(matches) == 1 else None


def scope_snapshot(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {"valid": False, "reason": "scope missing"}
    try:
        identity = path.stat()
    except OSError as exc:
        return {"valid": False, "reason": str(exc)}
    return {
        "valid": True,
        "path": str(path),
        "device": identity.st_dev,
        "inode": identity.st_ino,
        "memory_current": read_int(path / "memory.current"),
        "memory_peak": read_int(path / "memory.peak"),
        "memory_stat": read_kv(path / "memory.stat"),
        "memory_events": read_kv(path / "memory.events"),
        "cpu_stat": read_kv(path / "cpu.stat"),
        # The io controller is only required by dirty/writeback scenarios.
        # Native R1-R3 scopes legitimately omit io.stat; keep their memory
        # evidence valid and represent unavailable optional I/O counters as an
        # empty mapping. lzx-note
        "io_stat": (
            ACCEPT.read_io_stat(path / "io.stat")
            if (path / "io.stat").is_file() else {}
        ),
        "memory_pressure": read_psi(path / "memory.pressure"),  # lzx-note
    }


def snapshot_payload(cgroup: Path, apps: list[str], label: str) -> dict[str, Any]:
    app_rows: dict[str, Any] = {}
    for app in apps:
        slug = app.lower().replace("_", "-")
        app_rows[app] = scope_snapshot(resolve_scope(cgroup, f"automation-{slug}.scope"))
    return {
        "schema_version": 1,
        "label": label,
        "timestamp_ns": time.time_ns(),
        "monotonic_ns": time.monotonic_ns(),
        "kernel_release": os.uname().release,
        "cgroup": scope_snapshot(cgroup),
        "apps": app_rows,
        "host": ACCEPT.snapshot(cgroup),
    }


def command_snapshot(args: argparse.Namespace) -> int:
    payload = snapshot_payload(args.cgroup, args.apps.split("|"), args.label)
    invalid = [app for app, row in payload["apps"].items() if not row.get("valid")]
    payload["valid"] = not invalid and payload["cgroup"].get("valid", False)
    payload["invalid_apps"] = invalid
    write_json(args.output, payload)
    print(args.output)
    return 0 if payload["valid"] else 5


def stat_delta(before: dict[str, Any], after: dict[str, Any], key: str) -> int:
    return int(after.get(key, 0)) - int(before.get(key, 0))


MEMORY_KEYS = (
    "anon", "file", "shmem", "kernel", "slab", "sock",
    "pgfault", "pgmajfault", "workingset_refault_file", "workingset_refault_anon",
    "workingset_activate_file", "workingset_restore_file",
    "pgscan", "pgsteal", "pgscan_direct", "pgsteal_direct",
    "pgscan_kswapd", "pgsteal_kswapd", "pswpin", "pswpout",
)


def comparison_payload(before: dict[str, Any], after: dict[str, Any], hot: set[str]) -> dict[str, Any]:
    apps: dict[str, Any] = {}
    for app in sorted(before["apps"]):
        first, last = before["apps"][app], after["apps"].get(app, {})
        valid = (
            first.get("valid") and last.get("valid")
            and first.get("device") == last.get("device")
            and first.get("inode") == last.get("inode")
        )
        first_current = int(first.get("memory_current") or 0)
        last_current = int(last.get("memory_current") or 0)
        first_stat = first.get("memory_stat", {})
        last_stat = last.get("memory_stat", {})
        first_io = first.get("io_stat", {})
        last_io = last.get("io_stat", {})
        apps[app] = {
            "valid": valid,
            "class": "hot" if app in hot else "cold",
            "memory_current_before": first_current,
            "memory_current_after": last_current,
            "memory_current_drop_bytes": max(0, first_current - last_current),
            "file_drop_bytes": max(0, int(first_stat.get("file", 0)) - int(last_stat.get("file", 0))),
            "anon_drop_bytes": max(0, int(first_stat.get("anon", 0)) - int(last_stat.get("anon", 0))),
            "stat_deltas": {key: stat_delta(first_stat, last_stat, key) for key in MEMORY_KEYS},
            "io_deltas": {
                key: stat_delta(first_io, last_io, key)
                for key in ("rbytes", "wbytes", "rios", "wios")
            },  # lzx-note
            "psi_stall_us": {
                key: int(last.get("memory_pressure", {}).get(key, 0))
                - int(first.get("memory_pressure", {}).get(key, 0))
                for key in ("some_total", "full_total")
            },  # lzx-note
        }
    total = sum(row["memory_current_drop_bytes"] for row in apps.values())
    hot_drop = sum(row["memory_current_drop_bytes"] for row in apps.values() if row["class"] == "hot")
    cold_drop = total - hot_drop
    first_parent = before["cgroup"].get("memory_stat", {})
    last_parent = after["cgroup"].get("memory_stat", {})
    first_parent_io = before["cgroup"].get("io_stat", {})
    last_parent_io = after["cgroup"].get("io_stat", {})
    return {
        "schema_version": 1,
        "valid": all(row["valid"] for row in apps.values()),
        "apps": apps,
        "source_distribution": {
            "total_app_memory_drop_bytes": total,
            "hot_drop_bytes": hot_drop,
            "cold_drop_bytes": cold_drop,
            "hot_share_percent": 100.0 * hot_drop / total if total else None,
            "cold_share_percent": 100.0 * cold_drop / total if total else None,
        },
        "parent_deltas": {
            key: stat_delta(first_parent, last_parent, key) for key in MEMORY_KEYS
        },
        "parent_io_deltas": {
            key: stat_delta(first_parent_io, last_parent_io, key)
            for key in ("rbytes", "wbytes", "rios", "wios")
        },  # lzx-note
        "psi_stall_us": {
            key: int(after["cgroup"].get("memory_pressure", {}).get(key, 0))
            - int(before["cgroup"].get("memory_pressure", {}).get(key, 0))
            for key in ("some_total", "full_total")
        },  # lzx-note
    }


def command_compare(args: argparse.Namespace) -> int:
    payload = comparison_payload(
        read_json(args.before), read_json(args.after), set(args.hot.split("|"))
    )
    write_json(args.output, payload)
    print(args.output)
    return 0 if payload["valid"] else 6


def command_boundary(args: argparse.Namespace) -> int:
    """Set the dynamic real-PC boundary and preserve its explicit swap budget."""
    current = read_int(args.cgroup / "memory.current")
    if current is None:
        raise RuntimeError(f"cannot read {args.cgroup / 'memory.current'}")
    headroom = (args.pressure_mib - args.reclaim_mib) * MIB
    if headroom <= 0:
        raise ValueError("pressure-mib must exceed reclaim-mib")
    maximum = current + headroom
    swap_maximum = args.swap_max_mib * MIB
    outcome = subprocess.run(
        [
            "systemctl", "--user", "set-property", "--runtime", args.slice,
            f"MemoryMax={maximum}", "MemoryHigh=infinity",
            f"MemorySwapMax={swap_maximum}",
        ],
        text=True, capture_output=True, check=False, timeout=15,
    )
    observed = read_int(args.cgroup / "memory.max")
    observed_swap = read_int(args.cgroup / "memory.swap.max")
    payload = {
        "schema_version": 1,
        "valid": outcome.returncode == 0 and observed == maximum and observed_swap == swap_maximum,
        "slice": args.slice, "cgroup": str(args.cgroup),
        "memory_current_before": current,
        "pressure_bytes": args.pressure_mib * MIB,
        "reclaim_target_bytes": args.reclaim_mib * MIB,
        "headroom_bytes": headroom,
        "requested_memory_max": maximum, "observed_memory_max": observed,
        "requested_memory_swap_max": swap_maximum,
        "observed_memory_swap_max": observed_swap,
        "stderr": outcome.stderr.strip(),
    }
    write_json(args.output, payload)
    print(args.output)
    return 0 if payload["valid"] else 7


def command_app_native_gate(args: argparse.Namespace) -> int:
    """Reject rounds whose real applications did not build the required working set."""
    apps = args.apps.split("|")
    cold = set(args.cold.split("|"))
    snapshot = snapshot_payload(args.cgroup, apps, "app_native_gate")
    invalid_apps = [app for app, row in snapshot["apps"].items() if not row.get("valid")]
    total = sum(int(row.get("memory_current") or 0) for row in snapshot["apps"].values())
    cold_dirty = sum(
        int(row.get("memory_stat", {}).get("file_dirty", 0))
        for app, row in snapshot["apps"].items() if app in cold
    )
    # Anonymous application memory has no clean backing store: reclaiming it
    # requires swap-out just as reclaiming file_dirty requires writeback. Keep
    # the two counters separate in evidence, but gate the application-native
    # R4/R5 workload on their combined costly-reclaim population. lzx-note
    cold_anon_dirty = sum(
        int(row.get("memory_stat", {}).get("anon", 0))
        for app, row in snapshot["apps"].items() if app in cold
    )
    cold_costly = cold_dirty + cold_anon_dirty
    hot_file = sum(
        max(
            0,
            int(row.get("memory_stat", {}).get("file", 0))
            - int(row.get("memory_stat", {}).get("shmem", 0)),
        )
        for app, row in snapshot["apps"].items() if app not in cold
    )
    minimum_total = args.minimum_total_mib * MIB
    minimum_dirty = args.minimum_cold_dirty_mib * MIB
    minimum_costly = args.minimum_cold_costly_mib * MIB
    valid = (
        not invalid_apps
        and total >= minimum_total
        and cold_dirty >= minimum_dirty
        and (not args.require_dirty or cold_costly >= minimum_costly)
    )
    payload = {
        "schema_version": 1,
        "valid": valid,
        "working_set_origin": "application_ui",
        "synthetic_app_working_set": False,
        "total_app_memory_bytes": total,
        "minimum_total_app_memory_bytes": minimum_total,
        "cold_file_dirty_bytes": cold_dirty,
        "minimum_cold_file_dirty_bytes": minimum_dirty,
        "cold_anon_dirty_bytes": cold_anon_dirty,
        "cold_costly_reclaim_bytes": cold_costly,
        "minimum_cold_costly_reclaim_bytes": minimum_costly,
        "require_dirty": bool(args.require_dirty),
        "dirty_contract": "file_dirty_plus_swap_backed_anonymous",
        "hot_disk_file_bytes": hot_file,
        "invalid_apps": invalid_apps,
        "snapshot": snapshot,
    }
    write_json(args.output, payload)
    print(args.output)
    return 0 if valid else 10


def command_app_native_reclaim(args: argparse.Namespace) -> int:
    """Reclaim real application pages without launching a synthetic allocator."""
    reclaim_path = args.cgroup / "memory.reclaim"
    current_before = read_int(args.cgroup / "memory.current")
    if current_before is None or not reclaim_path.is_file():
        raise RuntimeError("application-native cgroup does not expose memory.reclaim")
    requested = args.target_mib * MIB
    minimum_achieved = int(requested * args.minimum_achieved_ratio)
    events_before = read_kv(args.cgroup / "memory.events")
    reclaim_request = str(requested)
    if args.swappiness:
        reclaim_request += f" swappiness={args.swappiness}"
    outcome = subprocess.run(
        ["sudo", "-n", "tee", str(reclaim_path)],
        input=f"{reclaim_request}\n", text=True, stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE, timeout=args.timeout,
    )
    deadline = time.monotonic() + max(1.0, args.settle_timeout)
    current_after = current_before
    while time.monotonic() <= deadline:
        observed = read_int(args.cgroup / "memory.current")
        if observed is not None:
            current_after = observed
        if current_before - current_after >= minimum_achieved:
            break
        time.sleep(0.1)
    events_after = read_kv(args.cgroup / "memory.events")
    achieved = max(0, current_before - current_after)
    oom_delta = (
        events_after.get("oom", 0) - events_before.get("oom", 0)
        + events_after.get("oom_kill", 0) - events_before.get("oom_kill", 0)
    )
    valid = achieved >= minimum_achieved and oom_delta == 0
    payload = {
        "schema_version": 1,
        "status": "HOLDING" if valid else "FAILED",
        "valid": valid,
        "pressure_kind": "memory_reclaim_of_application_native_working_sets",
        "synthetic_allocator": False,
        "requested_reclaim_bytes": requested,
        "reclaim_request": reclaim_request,
        "reclaim_swappiness": args.swappiness or None,
        "minimum_achieved_bytes": minimum_achieved,
        "achieved_reclaim_bytes": achieved,
        "achieved_ratio": achieved / requested if requested else None,
        "memory_current_before": current_before,
        "memory_current_after": current_after,
        "memory_reclaim_returncode": outcome.returncode,
        "memory_reclaim_stderr": outcome.stderr.strip(),
        "memory_events_before": events_before,
        "memory_events_after": events_after,
        "oom_event_delta": oom_delta,
    }
    write_json(args.state, payload)
    print(args.state)
    return 0 if valid else 11


def prepare_assets(config: dict[str, Any], run_dir: Path) -> dict[str, Any]:
    asset_root = Path(config["asset_root"])
    subprocess.run(
        [sys.executable, str(ASSET_BUILDER), "--output", str(asset_root)],
        check=True, timeout=300,
    )
    ACCEPT.write_local_app_fixtures(run_dir)
    fixture_dir = run_dir / "fixtures"
    asset_names = [
        "local-page.html", "serial-reuse.html", "writer-test.txt", "mail-test.eml",
        "writer-test.odt", "image-test.png", "audio-test.wav",
        "document-test.pdf",
        *(f"image-test-{index:02d}.png" for index in range(1, 9)),
    ]
    for name in asset_names:
        target = fixture_dir / name
        target.unlink(missing_ok=True)
        # LibreOffice edits and saves this document during R4/R5.  Never
        # hard-link a writable per-round asset back into the shared cache.
        # lzx-note
        if name == "writer-test.odt":
            shutil.copy2(asset_root / name, target)
            continue
        try:
            os.link(asset_root / name, target)
        except OSError:
            shutil.copy2(asset_root / name, target)
    return read_json(asset_root / "manifest.json")


def switch_actions(spec: Any, label: str, dwell: float, operate: bool = True) -> list[dict[str, Any]]:
    window_contract = {
        "name": spec.name, "app_key": spec.key,
        "class": spec.window_class, "title": spec.window_title,
    }
    if spec.key == "FIREFOX":
        window_contract.update({
            "minimum_foreground_width": 700,
            "minimum_foreground_height": 500,
            "dismiss_small_transient": True,
        })
    actions: list[dict[str, Any]] = [
        {
            "type": "switch", **window_contract,
            "label": f"{label}_SWITCH_{spec.key}",
        },
        {
            "type": "verify_foreground", **window_contract,
            "label": f"{label}_VERIFY_{spec.key}",
        },
    ]
    if operate:
        actions.append({
            "type": "key", "name": spec.name, "app_key": spec.key,
            "key": spec.operation_key, "label": f"{label}_OPERATE_{spec.key}",
        })
    actions.append({"type": "wait", "seconds": dwell, "label": f"{label}_DWELL_{spec.key}"})
    return actions


def app_native_steps(app: str, phase: str, run_dir: Path) -> list[dict[str, Any]]:
    """Return deterministic user-visible operations, never fixture commands."""
    fixtures = run_dir / "fixtures"
    if phase == "prepare":
        steps: dict[str, list[dict[str, Any]]] = {
            "FIREFOX": [
                # A brand-new Epiphany profile may initially expose a blank
                # tab even when --new-window received the fixture URI. Drive
                # the same address-bar action a user would perform so the
                # measured working set always comes from the offline page.
                # lzx-note
                {"type": "key", "key": "ctrl+l"},
                {"type": "type", "text": (fixtures / "local-page.html").as_uri()},
                {"type": "key", "key": "Return"},
                {"type": "wait", "seconds": 2.0},
                {"type": "key", "key": "Home"},
                {"type": "key", "key": "Page_Down", "repeat": 18, "interval": 0.08},
                {"type": "key", "key": "Home"},
            ],
            "THUNDERBIRD": [
                {"type": "key", "key": "Home"},
                {"type": "key", "key": "Page_Down", "repeat": 14, "interval": 0.08},
                {"type": "key", "key": "Home"},
            ],
            "VLC": [
                {"type": "key", "key": "space"},
                {"type": "key", "key": "ctrl+Right", "repeat": 5, "interval": 0.12},
                {"type": "key", "key": "space"},
            ],
            "GIMP": [
                *(
                    {"type": "open_file", "path": str(fixtures / f"image-test-{index:02d}.png"),
                     "wait_after": 4.0}
                    for index in range(2, 7)
                ),
                {"type": "key", "key": "plus", "repeat": 3},
            ],
            "LIBREOFFICE": [
                {"type": "key", "key": "ctrl+End"},
                {"type": "key", "key": "Page_Up", "repeat": 12, "interval": 0.06},
                {"type": "key", "key": "ctrl+Home"},
            ],
            "EVINCE": [
                {"type": "key", "key": "Home"},
                {"type": "key", "key": "Page_Down", "repeat": 28, "interval": 0.06},
                {"type": "key", "key": "Home"},
            ],
            "IMAGE_VIEWER": [
                {"type": "open_file", "path": str(fixtures / "image-test-07.png"),
                 "wait_after": 1.0},
                {"type": "key", "key": "plus", "repeat": 4},
                {"type": "key", "key": "minus", "repeat": 2},
            ],
            "SOLITAIRE": [
                {"type": "key", "key": "F2"},
                {"type": "key", "key": "Right", "repeat": 8},
                {"type": "key", "key": "Return", "repeat": 4},
            ],
        }
        return steps.get(app, [])
    if phase == "dirty_refresh":
        if app == "GIMP":
            return [
                # Modify every decoded 4096x4096 image through GIMP's own
                # command search.  With the per-run 64 MiB tile cache this
                # produces application-owned GEGL swap pages, and waiting for
                # each command avoids painting an unfinished Open dialog.
                # lzx-note
                *(
                    action
                    for _ in range(6)
                    for action in (
                        {"type": "key", "key": "slash"},
                        {"type": "type", "text": "Invert"},
                        {"type": "key", "key": "Return"},
                        {"type": "wait", "seconds": 1.0},
                        {"type": "key", "key": "ctrl+Page_Up"},
                    )
                ),
            ]
        if app == "LIBREOFFICE":
            return [
                {"type": "key", "key": "ctrl+End"},
                {
                    "type": "paste_text",
                    "text": "\nPARP application-native edited paragraph for dirty working-set validation.",
                    "repeat": 4096,
                },
                {"type": "key", "key": "ctrl+s"},
                {"type": "key", "key": "ctrl+End"},
            ]
        return []
    if phase == "fairness_probe" and app == "GIMP":
        return [
            action
            for _ in range(6)
            for action in (
                {"type": "key", "key": "ctrl+Page_Down"},
                {"type": "key", "key": "slash"},
                {"type": "type", "text": "Invert"},
                {"type": "key", "key": "Return"},
                {
                    "type": "wait_cgroup_pagein_stable",
                    "timeout": 12.0, "poll_seconds": 0.05,
                    "minimum_wait_seconds": 0.2, "stable_samples": 4,
                },
            )
        ]  # lzx-note: force reuse of each decoded image after a cold prediction.
    if phase in {"reuse", "fairness_probe"}:
        steps = {
            "FIREFOX": [
                {"type": "key", "key": "Home"},
                {"type": "key", "key": "Page_Down", "repeat": 14, "interval": 0.06},
                {"type": "key", "key": "ctrl+f"},
                {"type": "type", "text": "Project section 1200"},
                {"type": "key", "key": "Return"},
                {"type": "key", "key": "Escape"},
                {"type": "key", "key": "ctrl+plus"},
            ],
            "THUNDERBIRD": [
                {"type": "key", "key": "Home"},
                {"type": "key", "key": "Page_Down", "repeat": 12, "interval": 0.06},
                {"type": "key", "key": "ctrl+plus"},
            ],
            "VLC": [
                {"type": "key", "key": "space"},
                {"type": "key", "key": "ctrl+Right", "repeat": 4, "interval": 0.1},
                {"type": "key", "key": "space"},
            ],
            "GIMP": [
                {"type": "key", "key": "ctrl+Page_Down", "repeat": 6, "interval": 0.08},
                {"type": "key", "key": "plus", "repeat": 2, "interval": 0.08},
                {"type": "key", "key": "minus"},
            ],
        }
        return steps.get(app, [])
    raise ValueError(f"unknown application-native phase: {phase}")


def app_native_actions(
    spec: Any, phase: str, run_dir: Path, label: str, *, dwell: float = 0.25,
) -> list[dict[str, Any]]:
    """Expand native operations with explicit app/window identity and trace labels."""
    actions = switch_actions(spec, label, dwell, operate=False)
    if phase == "reuse":
        actions.append({
            "type": "capture_visual_baseline",
            "name": spec.name, "app_key": spec.key,
            "class": spec.window_class, "title": spec.window_title,
            "baseline_key": label,
            "sample_width": 32, "sample_height": 32,
            "label": f"{label}_BASELINE_{spec.key}",
        })  # lzx-note
    for index, template in enumerate(app_native_steps(spec.key, phase, run_dir), start=1):
        action = dict(template)
        action.update({
            "name": spec.name, "app_key": spec.key,
            "class": spec.window_class, "title": spec.window_title,
            "label": f"{label}_{phase.upper()}_{index:02d}_{spec.key}",
            "metadata": {
                "working_set_origin": "application_ui",
                "phase": phase, "app": spec.key,
            },
        })
        actions.append(action)
    if phase == "fairness_probe":
        actions.append({
            "type": "wait_cgroup_pagein_stable",
            "name": spec.name, "app_key": spec.key,
            "class": spec.window_class, "title": spec.window_title,
            "timeout": 8.0, "poll_seconds": 0.05,
            "minimum_wait_seconds": 0.2, "stable_samples": 4,
            "label": f"{label}_READY_{spec.key}",
            "metadata": {
                "working_set_origin": "application_ui",
                "phase": phase, "app": spec.key,
                "latency_endpoint": "application_cgroup_pagein_stable",
            },
        })
    elif phase == "reuse":
        actions.append({
            "type": "wait_visual_stable",
            "name": spec.name, "app_key": spec.key,
            "class": spec.window_class, "title": spec.window_title,
            "timeout": 8.0, "poll_seconds": 0.05,
            "stable_samples": 3, "mean_abs_delta": 0.75,
            "baseline_key": label, "require_visual_change": True,
            "minimum_change_delta": 1.0,
            "sample_width": 32, "sample_height": 32,
            "label": f"{label}_READY_{spec.key}",
            "metadata": {
                "working_set_origin": "application_ui",
                "phase": phase, "app": spec.key,
                "latency_endpoint": "rendered_window_stable",
            },
        })  # lzx-note: measure visible readiness, not only input injection.
    return actions


def action_plan_payload(
    config: dict[str, Any], scenario_name: str, seed: int,
    scenario: dict[str, Any], asset_manifest: dict[str, Any],
) -> dict[str, Any]:
    """Build a path-independent contract for Native/PARP GUI replay."""
    stable_fields = {
        "type", "label", "app_key", "key", "repeat", "interval", "seconds",
        "direction", "amount", "button", "x_ratio", "y_ratio", "x1_ratio",
        "y1_ratio", "x2_ratio", "y2_ratio", "duration_ms", "state",
        "delay_ms", "expected_title", "timeout", "poll_seconds",
        "stable_samples", "mean_abs_delta", "sample_width", "sample_height",
        "baseline_key", "require_visual_change", "minimum_change_delta",
        "minimum_wait_seconds",
        "minimum_foreground_width", "minimum_foreground_height",
        "dismiss_small_transient",
    }
    relevant_prefixes = (
        "LAUNCH_", "WAIT_", "REAL_APP_", "REAL_COLD_INIT_",
        "REAL_TRAINED_", "REAL_REUSE_", "REAL_FAIR_WARM_",
    )
    actions: list[dict[str, Any]] = []
    for source in scenario.get("actions", []):
        label = str(source.get("label", ""))
        if not label.startswith(relevant_prefixes):
            continue
        row = {key: source[key] for key in stable_fields if key in source}
        if "path" in source:
            row["asset_name"] = Path(str(source["path"])).name
        if "text" in source:
            text = str(source["text"])
            if text.startswith("file://"):
                # Per-round fixture roots differ across Native/PARP. Preserve
                # the intended asset identity while keeping replay hashes
                # path-independent. The manifest already pins its contents.
                # lzx-note
                row["text_asset_name"] = Path(text.removeprefix("file://")).name
            else:
                row["text_bytes"] = len(text.encode("utf-8"))
                row["text_sha256"] = hashlib.sha256(text.encode("utf-8")).hexdigest()
        actions.append(row)
    assets = {
        name: row.get("sha256")
        for name, row in sorted(asset_manifest.get("assets", {}).items())
    }
    contract = {
        "schema_version": 1,
        "scenario": scenario_name,
        "seed": seed,
        "apps": list(config["apps"]),
        "hot_apps": list(config["hot_apps"]),
        "cold_apps": list(config["cold_apps"]),
        "trained_history": list(config["trained_history"]),
        "reclaim_target_mib": scenario_reclaim_target_mib(config, scenario_name),
        "pressure_mode": str(config.get("pressure_mode", "")),
        "asset_schema_version": int(asset_manifest.get("schema_version", 0)),
        "asset_sha256": assets,
        "actions": actions,
    }
    canonical = json.dumps(contract, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return {
        **contract,
        "sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
    }  # lzx-note


def reuse_socket(run_dir: Path, app: str) -> Path:
    token = hashlib.sha256(f"{run_dir}:{app}".encode()).hexdigest()[:12]
    return Path(f"/run/user/{os.getuid()}/parp-reuse-{token}.sock")  # lzx-note


def reuse_fixture_action(socket_path: Path, command: str, label: str) -> dict[str, Any]:
    return ACCEPT.py_fixture_action(
        socket_path, command, timeout=1200, wait=30, label=label,
    )  # lzx-note


def generate_scenario(
    config: dict[str, Any], scenario_name: str, run_dir: Path,
    cgroup: Path, seed: int, require_myfs: bool, minimum_myfs_abi: int,
    trace_instance: str, require_workload_profiles: bool = False,
) -> dict[str, Any]:
    specs = ACCEPT.app_specs(run_dir)
    apps = list(config["apps"])
    hot = list(config["hot_apps"])
    cold = list(config["cold_apps"])
    reclaim_target_mib = scenario_reclaim_target_mib(config, scenario_name)
    app_text = "|".join(apps)
    actions: list[dict[str, Any]] = [{
        "type": "trace_marker", "event_type": "REAL_PC_START", "status": "running",
        "label": "REAL_PC_START", "metadata": {"scenario": scenario_name, "seed": seed},
    }]
    exact_reuse = scenario_name == "page_reuse_cold_only"
    cold_dirty_reclaim = scenario_name == "cold_dirty_reclaim"  # lzx-note
    workload_matrix = scenario_name == "workload_matrix_reclaim"  # lzx-note
    substitution = scenario_name in SUBSTITUTION_SCENARIOS  # lzx-note
    app_native = scenario_name in APP_NATIVE_SCENARIOS  # lzx-note
    app_native_dirty = scenario_name in APP_NATIVE_DIRTY_SCENARIOS  # lzx-note
    writeback_gate = scenario_name in {
        "cold_writeback_gate_hot_reuse", "r5_app_writeback_gate",
    }  # lzx-note
    controlled_fixture = exact_reuse or cold_dirty_reclaim or workload_matrix or substitution  # lzx-note
    sockets: dict[str, Path] = {}
    # A workload-matrix dirty profile must be sampled by the final, exact
    # trained-history prediction.  It is therefore materialized immediately
    # before that final foreground switch below, rather than during fixture
    # setup where normal writeback can clean it before inference. lzx-note
    workload_dirty_apps = (
        list(config.get("workload_dirty_apps", ())) if workload_matrix else []
    )
    ballast = run_dir / "reuse-working-sets"
    ballast.mkdir(parents=True, exist_ok=True)
    launches = {app: ACCEPT.app_launch_actions(specs[app]) for app in apps}
    serial_url = ""
    if scenario_name == SERIAL_REUSE_SCENARIO:
        serial = config["serial_reuse"]
        serial_url = (
            (run_dir / "fixtures/serial-reuse.html").as_uri()
            + f"?mib={int(serial['allocation_mib'])}"
            + f"&chunk={int(serial['chunk_mib'])}&seed={seed}"
        )
        local_url = (run_dir / "fixtures/local-page.html").as_uri()
        firefox_launch = launches["FIREFOX"][0]
        if local_url not in str(firefox_launch["command"]):
            raise ValueError("R6 Firefox launch command is missing its local fixture URL")
        firefox_launch["command"] = str(firefox_launch["command"]).replace(
            local_url, shlex.quote(serial_url), 1,
        )
        launches["FIREFOX"][1]["title"] = "PARP R6"
        # Load the application-owned anonymous working set at browser launch.
        # Re-navigating a long file URI through the address bar after preparing
        # all applications is focus-sensitive and adds no user-workload value.
        # lzx-note
    if controlled_fixture:
        layout = (
            config.get("substitution_layout", {})
            if substitution else controlled_layout(config, scenario_name)
        )
        if set(layout) != set(apps):
            raise ValueError("controlled memory scenario requires one layout entry per app")
        for app in apps:
            socket_path = reuse_socket(run_dir, app)
            sockets[app] = socket_path
            sizes = layout[app]
            slug = app.lower().replace("_", "-")
            if substitution:
                command = [
                    sys.executable, str(SUBSTITUTION_LAUNCHER),
                    "--fixture", str(SUBSTITUTION_FIXTURE), "--app", app,
                    "--socket", str(socket_path),
                    "--log", str(ballast / f"{slug}.csv"),
                    "--clean-file", str(ballast / f"{slug}-clean.data"),
                    "--dirty-file", str(ballast / f"{slug}-dirty.data"),
                    "--hot-file", str(ballast / f"{slug}-hot.data"),
                    "--clean-bytes", str(int(sizes["clean_mib"]) * MIB),
                    "--dirty-bytes", str(int(sizes["dirty_mib"]) * MIB),
                    "--hot-bytes", str(int(sizes["hot_mib"]) * MIB),
                    "--", *shlex.split(specs[app].command),
                ]
            else:
                command = [
                    sys.executable, str(REUSE_LAUNCHER),
                    "--fixture", str(FIXTURE), "--app", app,
                    "--socket", str(socket_path),
                    "--file", str(ballast / f"{slug}.data"),
                    "--log", str(ballast / f"{slug}.csv"),
                    "--file-bytes", str(int(sizes["file_mib"]) * MIB),
                    "--anon-bytes", str(int(sizes["anon_mib"]) * MIB),
                    "--hot-bytes", str(int(sizes["file_mib"]) * MIB),
                    "--materialize-file", "--", *shlex.split(specs[app].command),
                ]
            launches[app][0] = {
                "type": "launch", "name": specs[app].name,
                "scope_name": specs[app].name, "app_key": app,
                "command": shlex.join(command), "label": f"LAUNCH_{app}",
            }
    for app in apps:
        actions.append(launches[app][0])
    for app in apps:
        actions.append(launches[app][1])
    actions.append({
        "type": "wait", "seconds": float(config["startup_settle_seconds"]),
        "label": "REAL_PC_STARTUP_SETTLE",
    })
    if controlled_fixture:
        for app in apps:
            actions.append(reuse_fixture_action(
                sockets[app], "STATUS", f"REAL_REUSE_FIXTURE_READY_{app}",
            ))
        for app in apps:
            actions.append(reuse_fixture_action(
                sockets[app], "PREPARE", f"REAL_REUSE_PREPARE_{app}",
            ))
        # The legacy all-dirty scenario deliberately dirties every fixture at
        # setup.  The matrix scenario has a stricter timing contract and is
        # handled just before its final trained switch below. lzx-note
        if cold_dirty_reclaim:
            for app in apps:
                if app not in sockets:
                    raise ValueError(f"workload dirty app not in scenario: {app}")
                actions.append(reuse_fixture_action(
                    sockets[app], "COLD_DIRTY_FILE", f"REAL_COLD_DIRTY_CREATE_{app}",
                ))
        # The substitution scenario dirties its file mappings immediately
        # before the last trained-history switch below.  Doing this during
        # setup left enough GUI/history time for background writeback to turn
        # the intended dirty reclaim candidates into clean pages. lzx-note

    if app_native:
        # Materialize every measured working set through visible application
        # operations.  These switches intentionally precede the five-entry
        # prediction window, so they cannot alter the scored LSTM history.
        # lzx-note
        actions.append({
            "type": "trace_marker", "event_type": "REAL_APP_WORKSET_START",
            "status": "running", "label": "REAL_APP_WORKSET_START",
        })
        for index, app in enumerate(apps, start=1):
            if scenario_name == SERIAL_REUSE_SCENARIO and app == "FIREFOX":
                firefox = specs[app]
                prefix = f"REAL_APP_PREPARE_{index:02d}"
                r6_window = {
                    "name": firefox.name, "app_key": app,
                    "class": firefox.window_class, "title": "PARP R6",
                    "minimum_foreground_width": 700,
                    "minimum_foreground_height": 500,
                    "dismiss_small_transient": True,
                }
                actions.extend([
                    {"type": "switch", **r6_window, "label": f"{prefix}_SWITCH_{app}"},
                    {"type": "verify_foreground", **r6_window,
                     "label": f"{prefix}_VERIFY_{app}"},
                    {"type": "wait_window_title", **r6_window,
                     "expected_title": (
                         "PARP R6 READY 0/"
                         f"{int(config['serial_reuse']['allocation_mib']) // int(config['serial_reuse']['chunk_mib'])}"
                     ),
                     "timeout": float(config["serial_reuse"].get("setup_timeout_seconds", 60)),
                     "poll_seconds": 0.02, "label": f"{prefix}_READY_{app}"},
                    {"type": "wait", "seconds": 0.3,
                     "label": f"{prefix}_DWELL_{app}"},
                ])
            else:
                actions.extend(app_native_actions(
                    specs[app], "prepare", run_dir,
                    f"REAL_APP_PREPARE_{index:02d}", dwell=0.3,
                ))
        actions.append({
            "type": "trace_marker", "event_type": "REAL_APP_WORKSET_DONE",
            "status": "success", "label": "REAL_APP_WORKSET_DONE",
        })

    if scenario_name == SERIAL_REUSE_SCENARIO:
        serial = config["serial_reuse"]
        firefox = specs["FIREFOX"]
        r6_window = {
            "name": firefox.name, "app_key": "FIREFOX",
            "class": firefox.window_class, "title": "PARP R6",
            "minimum_foreground_width": 700,
            "minimum_foreground_height": 500,
            "dismiss_small_transient": True,
        }
        actions.extend([
            {
                "type": "switch", **r6_window,
                "label": "REAL_APP_R6_SERIAL_SETUP_SWITCH_FIREFOX",
            },
            {
                "type": "verify_foreground", **r6_window,
                "label": "REAL_APP_R6_SERIAL_SETUP_VERIFY_FIREFOX",
            },
            {
                "type": "wait_window_title", **r6_window, "expected_title": (
                    f"PARP R6 READY 0/{int(serial['allocation_mib']) // int(serial['chunk_mib'])}"
                ),
                "timeout": float(serial.get("setup_timeout_seconds", 60)),
                "poll_seconds": 0.02, "label": "REAL_APP_R6_SERIAL_READY",
            },
        ])  # lzx-note: application-owned anonymous pages, no allocator fixture.

    if scenario_name == FAIRNESS_SCENARIO:
        participants = list(config["fairness_misprediction"]["participants"])
        unexpected = str(config["fairness_misprediction"]["unexpected_reuse_app"])
        actions.extend(app_native_actions(
            specs[unexpected], "fairness_probe", run_dir,
            "REAL_APP_R7_PRIME_GIMP", dwell=0.2,
        ))
        actions.append({
            "type": "trace_marker", "event_type": "REAL_FAIR_WARM_START",
            "status": "running", "label": "REAL_FAIR_WARM_START",
        })
        for index, app in enumerate(participants, start=1):
            actions.extend(app_native_actions(
                specs[app], "fairness_probe", run_dir,
                f"REAL_FAIR_WARM_{index:02d}", dwell=0.2,
            ))
        actions.append({
            "type": "trace_marker", "event_type": "REAL_FAIR_WARM_DONE",
            "status": "success", "label": "REAL_FAIR_WARM_DONE",
        })  # lzx-note: per-app warm baseline for normalized fairness.

    # These are genuine application interactions. The five cold applications
    # are used once and then left in the background, as in a normal workday.
    for index, app in enumerate(cold, start=1):
        actions.extend(switch_actions(specs[app], f"REAL_COLD_INIT_{index:02d}", 0.6))
        if exact_reuse:
            actions.append(reuse_fixture_action(
                sockets[app], "TOUCH_BOTH", f"REAL_COLD_INIT_REUSE_{app}",
            ))
        elif substitution:
            actions.append(reuse_fixture_action(
                sockets[app], "TOUCH_HOT", f"REAL_COLD_INIT_HOT_{app}",
            ))

    workload_profile_marker = run_dir / "workload-profile-window-start.json"
    if app_native_dirty:
        # Keep application-created dirty state close to the prediction and
        # reclaim window.  GIMP's bounded tile cache writes its real GEGL
        # swap file, while LibreOffice edits its document through the GUI.
        # Performing these actions after the one-time cold-app visits avoids
        # losing the file-dirty state to background writeback.  The five
        # trained switches below still replace these foreground events before
        # the prediction gate samples the LSTM history. lzx-note
        actions.append(trained_evidence_action(
            ["mark", "--output", str(workload_profile_marker)],
            "REAL_WORKLOAD_PROFILE_MARK",
        ))
        for index, app in enumerate(("GIMP", "LIBREOFFICE"), start=1):
            actions.extend(app_native_actions(
                specs[app], "dirty_refresh", run_dir,
                f"REAL_APP_DIRTY_{index:02d}", dwell=0.2,
            ))

    marker = run_dir / "prediction-window-start.json"
    prediction_gate_marker = marker  # lzx-note
    actions.append(trained_evidence_action(["mark", "--output", str(marker)], "REAL_PREDICTION_MARK"))
    for index, app in enumerate(config["trained_history"], start=1):
        if workload_matrix and index == len(config["trained_history"]):
            # The switch that follows has the exact five-app LSTM history and
            # atomically publishes the freshly dirty Evince cgroup in the V3
            # binding set.  This prevents a stale FILE_CLEAN profile from
            # standing in for the requested FILE_DIRTY workload. lzx-note
            for dirty_app in workload_dirty_apps:
                if dirty_app not in sockets:
                    raise ValueError(f"workload dirty app not in scenario: {dirty_app}")
                actions.append(reuse_fixture_action(
                    sockets[dirty_app], "COLD_DIRTY_FILE",
                    f"REAL_COLD_DIRTY_CREATE_{dirty_app}",
                ))
        if substitution and index == len(config["trained_history"]) - 1:
            # The direct event backend can observe a VLC focus transition
            # shortly before xdotool completes the final switch action.  Dirty
            # the mappings before the penultimate trained switch so both the
            # penultimate and final prediction events necessarily sample the
            # new workload state, while still keeping dirty-page age below the
            # background writeback window.
            # Only the small hot-control mapping is refreshed; the measured
            # clean and dirty mappings remain untouched until reclaim. lzx-note
            for fixture_app in apps:
                actions.append(reuse_fixture_action(
                    sockets[fixture_app], "COLDIFY",
                    f"REAL_SUBSTITUTION_COLDIFY_{fixture_app}",
                ))
            for hot_app in hot:
                actions.append(reuse_fixture_action(
                    sockets[hot_app], "TOUCH_HOT",
                    f"REAL_SUBSTITUTION_REFRESH_HOT_{hot_app}",
                ))
        if substitution and index == len(config["trained_history"]):
            # Refresh only prediction-cold dirty candidates immediately before
            # the final model event.  This closes the nondeterministic window
            # in which background writeback can clean a mapping between the
            # initial COLDIFY and workload-profile sampling. lzx-note
            for cold_app in cold:
                actions.append(reuse_fixture_action(
                    sockets[cold_app], "REDIRTY",
                    f"REAL_SUBSTITUTION_REDIRTY_{cold_app}",
                ))
            prediction_gate_marker = run_dir / "prediction-profile-window-start.json"
            actions.append(trained_evidence_action(
                ["mark", "--output", str(prediction_gate_marker)],
                "REAL_PREDICTION_PROFILE_MARK",
            ))
        actions.extend(
            switch_actions(
                specs[app], f"REAL_TRAINED_{index:02d}",
                float(config["history_dwell_seconds"]),
            )
        )
        if exact_reuse:
            actions.append(reuse_fixture_action(
                sockets[app], "TOUCH_BOTH", f"REAL_TRAINED_REUSE_{index:02d}_{app}",
            ))
        elif substitution:
            actions.append(reuse_fixture_action(
                sockets[app], "TOUCH_HOT", f"REAL_TRAINED_HOT_{index:02d}_{app}",
            ))
    actions.append({
        "type": "wait", "seconds": float(config["prediction_settle_seconds"]),
        "label": "REAL_PREDICTION_SETTLE",
    })

    gate = config["prediction_gate"]
    gate_args = [
        "prediction-gate", "--after-mark", str(prediction_gate_marker),
        "--output", str(run_dir / "prediction-gate.json"),
        "--history", "|".join(config["trained_history_vocab"]),
        "--opened", "|".join(ACCEPT.LSAPP_NAME_BY_APP_KEY[app] for app in apps),
        "--current", str(config["current_vocab"]),
        "--current-key", str(config["current_app"]),
        "--expected-next", "|".join(config["expected_next_vocab"]),
        "--cold", "|".join(ACCEPT.LSAPP_NAME_BY_APP_KEY[app] for app in cold),
        "--minimum-hot-probability", str(gate["minimum_hot_probability"]),
        "--maximum-cold-probability", str(gate["maximum_cold_probability"]),
        "--minimum-bindings", str(gate["minimum_bindings"]),
        "--minimum-myfs-abi", str(minimum_myfs_abi),  # lzx-note
        "--timeout", str(gate["timeout_seconds"]),
        "--require-myfs" if require_myfs else "--no-require-myfs",
    ]
    if (workload_matrix or substitution) and require_workload_profiles:
        expected_profiles = "|".join(
            f"{app}:{workload_class}"
            for app, workload_class in sorted(
                config.get("workload_expected_classes", {}).items()
            )
        )
        gate_args.extend(["--expected-workload-profiles", expected_profiles])  # lzx-note
    if app_native_dirty and require_workload_profiles:
        expected_profiles = "|".join(
            f"{app}:{workload_class}"
            for app, workload_class in sorted(
                config.get("workload_expected_classes", {}).items()
            )
        )
        actions.append(trained_evidence_action([
            "workload-profile-gate",
            "--after-mark", str(workload_profile_marker),
            "--output", str(run_dir / "workload-profile-gate.json"),
            "--expected-workload-profiles", expected_profiles,
            "--minimum-bindings", str(gate["minimum_bindings"]),
            "--minimum-myfs-abi", str(minimum_myfs_abi),
            "--timeout", str(gate["timeout_seconds"]),
        ], "REAL_WORKLOAD_PROFILE_GATE"))  # lzx-note
    actions.append(trained_evidence_action(gate_args, "REAL_PREDICTION_GATE"))
    if writeback_gate and substitution:
        # The prediction/profile event is already frozen by the strict gate.
        # Refresh only the five cold dirty mappings once more immediately
        # before residency/snapshot/pressure evidence.  Fixture socket I/O
        # cannot create a foreground event, so this narrows the dirty-state
        # window without changing the LSTM history or published ranks.
        # lzx-note
        for cold_app in cold:
            actions.append(reuse_fixture_action(
                sockets[cold_app], "REDIRTY",
                f"REAL_WRITEBACK_GATE_FINAL_REDIRTY_{cold_app}",
            ))
    if cold_dirty_reclaim or workload_matrix or substitution:
        # mincore observes file/anonymous residency without faulting pages in.
        # lzx-note
        for app in apps:
            actions.append(reuse_fixture_action(
                sockets[app], "RESIDENCY_BEFORE", f"REAL_COLD_DIRTY_RESIDENT_BEFORE_{app}",
            ))
    if app_native:
        native = config["app_native"]
        gate_args = [
            "app-native-gate", "--cgroup", str(cgroup),
            "--apps", app_text, "--cold", "|".join(cold),
            "--minimum-total-mib", str(int(native["minimum_total_working_set_mib"])),
            "--minimum-cold-dirty-mib",
            str(int(native.get("minimum_cold_file_dirty_mib", 0))),
            "--minimum-cold-costly-mib",
            str(int(native.get("minimum_cold_costly_reclaim_mib", 0))),
            "--output", str(run_dir / "app-native-gate.json"),
        ]
        if app_native_dirty:
            gate_args.append("--require-dirty")
        actions.append(helper_action(gate_args, "REAL_APP_NATIVE_GATE"))
    actions.append(ACCEPT.trace_action(trace_instance, "enable-reclaim", "REAL_TRACE_ENABLE"))

    if writeback_gate:
        actions.append(helper_action([
            "writeback-gate", "--expected-laptop-mode",
            str(int(config["reclaim_writeback_gate"]["laptop_mode"])),
            "--output", str(run_dir / "writeback-gate-evidence.json"),
        ], "REAL_WRITEBACK_GATE_EVIDENCE"))

    before = run_dir / "snapshot-before-pressure.json"
    under = run_dir / "snapshot-under-pressure.json"
    actions.append(helper_action([
        "snapshot", "--cgroup", str(cgroup), "--apps", app_text,
        "--label", "before_pressure", "--output", str(before),
    ], "REAL_SNAPSHOT_BEFORE"))
    global_pressure = str(config.get("pressure_mode", "")).startswith("global_memfree")
    if app_native:
        # All memory already belongs to the real GUI applications.  Ask the
        # cgroup to reclaim a fixed amount instead of launching the anonymous
        # allocation fixture used by M1-M5. lzx-note
        pass
    elif global_pressure:
        actions.append(trained_evidence_action([
            "global-pressure-plan",
            "--target-memfree-mib", str(config["global_target_memfree_mib"]),
            "--reclaim-probe-mib", str(config["global_reclaim_probe_mib"]),
            "--max-allocate-mib", str(config["global_max_allocate_mib"]),
            "--output", str(run_dir / "pressure-boundary.json"),
        ], "REAL_SET_GLOBAL_PRESSURE_BOUNDARY"))
    else:
        actions.append(helper_action([
            "boundary", "--slice", str(config["slice"]), "--cgroup", str(cgroup),
            "--pressure-mib", str(config["pressure_mib"]),
            "--reclaim-mib", str(reclaim_target_mib),
            "--swap-max-mib", str(config.get("memory_swap_max_mib", 1024)),
            "--output", str(run_dir / "pressure-boundary.json"),
        ], "REAL_SET_PRESSURE_BOUNDARY"))
    pressure_state = run_dir / "pressure-state.json"
    if app_native:
        native = config["app_native"]
        reclaim_arguments = [
            "app-native-reclaim", "--cgroup", str(cgroup),
            "--target-mib", str(reclaim_target_mib),
            "--minimum-achieved-ratio",
            str(float(native["minimum_reclaim_achieved_ratio"])),
            "--timeout", str(float(config["pressure_ramp_timeout_seconds"])),
            "--settle-timeout", str(float(native.get("settle_timeout_seconds", 5.0))),
            "--state", str(pressure_state),
        ]
        if scenario_name == SERIAL_REUSE_SCENARIO:
            reclaim_arguments.extend([
                "--swappiness", str(config["serial_reuse"]["reclaim_swappiness"]),
            ])
        elif scenario_name == FAIRNESS_SCENARIO:
            reclaim_arguments.extend([
                "--swappiness",
                str(config["fairness_misprediction"]["reclaim_swappiness"]),
            ])
        pressure_actions = [
            {
                "type": "trace_marker", "event_type": "REAL_PRESSURE_START",
                "status": "running", "label": "REAL_PRESSURE_START",
            },
            helper_action(reclaim_arguments, "REAL_APP_NATIVE_RECLAIM"),
            {
                "type": "wait_json", "path": str(pressure_state), "field": "status",
                "equals": "HOLDING", "timeout": float(config["pressure_ramp_timeout_seconds"]),
                "poll_seconds": 0.2, "label": "REAL_PRESSURE_HOLDING",
            },
            trained_evidence_action([
                "capture-json", "--input", str(pressure_state),
                "--output", str(run_dir / "pressure-holding-state.json"),
            ], "REAL_PRESSURE_HOLDING_EVIDENCE"),
        ]
    else:
        pressure_command = [sys.executable, str(PRESSURE)]
        if global_pressure:
            pressure_command.extend([
                "--target-memfree-mib", str(config["global_target_memfree_mib"]),
                "--max-allocate-mib", str(config["global_max_allocate_mib"]),
                "--reclaim-probe-mib", str(config["global_reclaim_probe_mib"]),
            ])
        else:
            pressure_command.extend([
                "--target-mib", str(config["pressure_mib"]),
                "--reclaim-probe-mib", "0",
            ])
        pressure_command.extend([
            "--chunk-mib", str(config["pressure_chunk_mib"]),
            "--ramp-interval", str(config["pressure_ramp_interval_seconds"]),
            "--hold-seconds", str(config["pressure_hold_seconds"]),
            "--oom-score-adj", "1000", "--seed", str(seed), "--state", str(pressure_state),
        ])
        pressure_actions = [
            {
                "type": "trace_marker", "event_type": "REAL_PRESSURE_START",
                "status": "running", "label": "REAL_PRESSURE_START",
            },
            {
                "type": "launch", "name": "real-pressure", "scope_name": "real-pressure",
                "command": shlex.join(pressure_command), "label": "REAL_PRESSURE_LAUNCH",
            },
            {
                "type": "wait_json", "path": str(pressure_state), "field": "status",
                "equals": "HOLDING", "timeout": float(config["pressure_ramp_timeout_seconds"]),
                "poll_seconds": 0.2, "label": "REAL_PRESSURE_HOLDING",
            },
            trained_evidence_action([
                "capture-json", "--input", str(pressure_state),
                "--output", str(run_dir / "pressure-holding-state.json"),
            ], "REAL_PRESSURE_HOLDING_EVIDENCE"),
            {
                "type": "wait",
                "seconds": float(config.get("post_pressure_settle_seconds", 2.0)),
                "label": "REAL_RECLAIM_SETTLE",
            },
        ]
    if cold_dirty_reclaim or workload_matrix or substitution:
        for app in apps:
            pressure_actions.append(reuse_fixture_action(
                sockets[app], "RESIDENCY_AFTER", f"REAL_COLD_DIRTY_RESIDENT_AFTER_{app}",
            ))
    pressure_actions.extend([
        helper_action([
            "snapshot", "--cgroup", str(cgroup), "--apps", app_text,
            "--label", "under_pressure", "--output", str(under),
        ], "REAL_SNAPSHOT_UNDER"),
        helper_action([
            "compare", "--before", str(before), "--after", str(under),
            "--hot", "|".join(hot), "--output", str(run_dir / "reclaim-source.json"),
        ], "REAL_RECLAIM_SOURCE"),
    ])
    actions.extend(pressure_actions)

    if scenario_name == SERIAL_REUSE_SCENARIO:
        serial = config["serial_reuse"]
        firefox = specs["FIREFOX"]
        steps = int(serial["allocation_mib"]) // int(serial["chunk_mib"])
        actions.extend(switch_actions(
            firefox, "REAL_REUSE_SERIAL", 0.0, operate=False,
        ))
        for step in range(1, steps + 1):
            prefix = f"REAL_REUSE_SERIAL_STEP_{step:02d}"
            actions.extend([
                {
                    "type": "click_window", "name": firefox.name,
                    "app_key": "FIREFOX", "class": firefox.window_class,
                    "title": "PARP R6", "x_ratio": 0.5, "y_ratio": 0.5,
                    "label": f"{prefix}_REQUEST_FIREFOX",
                    "metadata": {
                        "latency_start": "serial_page_touch_request",
                        "serial_step": step,
                    },
                },
                {
                    "type": "wait_window_title", "name": firefox.name,
                    "app_key": "FIREFOX", "class": firefox.window_class,
                    "title": "PARP R6", "expected_title": f"PARP R6 STEP {step:02d}/{steps}",
                    "timeout": float(serial.get("step_timeout_seconds", 30)),
                    "poll_seconds": float(serial.get("poll_seconds", 0.01)),
                    "label": f"{prefix}_READY_FIREFOX",
                    "metadata": {
                        "latency_endpoint": "browser_main_thread_serial_page_touch_done",
                        "serial_step": step,
                    },
                },
            ])
    elif scenario_name == FAIRNESS_SCENARIO:
        participants = list(config["fairness_misprediction"]["participants"])
        for index, app in enumerate(participants, start=1):
            actions.extend(app_native_actions(
                specs[app], "fairness_probe", run_dir,
                f"REAL_REUSE_FAIR_{index:02d}", dwell=0.2,
            ))
    elif scenario_name == "r2_app_predicted_return":
        actions.extend(app_native_actions(
            specs["FIREFOX"], "reuse", run_dir, "REAL_REUSE_APP_01", dwell=0.2,
        ))
    elif scenario_name in {
        "r3_app_source_distribution", "r4_app_dirty_substitution",
        "r5_app_writeback_gate",
    }:
        for index, app in enumerate(("FIREFOX", "THUNDERBIRD", "VLC"), start=1):
            actions.extend(app_native_actions(
                specs[app], "reuse", run_dir,
                f"REAL_REUSE_APP_{index:02d}", dwell=0.2,
            ))
    elif scenario_name == "predicted_return":
        actions.extend(switch_actions(specs["FIREFOX"], "REAL_REUSE", 1.0))
        actions.extend([
            {"type": "key", "name": specs["FIREFOX"].name, "app_key": "FIREFOX", "key": "Page_Down", "repeat": 4, "label": "REAL_REUSE_SCROLL_FIREFOX"},
            {"type": "wait", "seconds": 1.0, "label": "REAL_REUSE_RENDER_SETTLE"},
        ])
    elif scenario_name == "mixed_multitask":
        for index, app in enumerate(("FIREFOX", "THUNDERBIRD", "VLC"), start=1):
            actions.extend(switch_actions(specs[app], f"REAL_REUSE_{index:02d}", 0.8))
    elif exact_reuse:
        for index, app in enumerate(config.get("reuse_targets", ["FIREFOX"]), start=1):
            prefix = f"exact-{index:02d}-{app.lower().replace('_', '-')}"
            actions.extend(switch_actions(
                specs[app], f"REAL_REUSE_EXACT_{index:02d}", 0.4, operate=False,
            ))
            actions.extend([
                helper_action([
                    "snapshot", "--cgroup", str(cgroup), "--apps", app_text,
                    "--label", f"{prefix}_before", "--output", str(run_dir / f"{prefix}-before.json"),
                ], f"REAL_REUSE_EXACT_{index:02d}_SNAPSHOT_BEFORE"),
                reuse_fixture_action(
                    sockets[app], "TOUCH_FILE", f"REAL_REUSE_EXACT_{index:02d}_FILE_{app}",
                ),
                helper_action([
                    "snapshot", "--cgroup", str(cgroup), "--apps", app_text,
                    "--label", f"{prefix}_after_file", "--output", str(run_dir / f"{prefix}-after-file.json"),
                ], f"REAL_REUSE_EXACT_{index:02d}_SNAPSHOT_FILE"),
                reuse_fixture_action(
                    sockets[app], "TOUCH_ANON", f"REAL_REUSE_EXACT_{index:02d}_ANON_{app}",
                ),
                helper_action([
                    "snapshot", "--cgroup", str(cgroup), "--apps", app_text,
                    "--label", f"{prefix}_after_anon", "--output", str(run_dir / f"{prefix}-after-anon.json"),
                ], f"REAL_REUSE_EXACT_{index:02d}_SNAPSHOT_ANON"),
                reuse_fixture_action(
                    sockets[app], "TOUCH_FILE", f"REAL_REUSE_EXACT_{index:02d}_WARM_FILE_{app}",
                ),
                reuse_fixture_action(
                    sockets[app], "TOUCH_ANON", f"REAL_REUSE_EXACT_{index:02d}_WARM_ANON_{app}",
                ),
                helper_action([
                    "snapshot", "--cgroup", str(cgroup), "--apps", app_text,
                    "--label", f"{prefix}_after_warm", "--output", str(run_dir / f"{prefix}-after-warm.json"),
                ], f"REAL_REUSE_EXACT_{index:02d}_SNAPSHOT_WARM"),
            ])
    elif substitution:
        for index, app in enumerate(config.get("reuse_targets", hot), start=1):
            prefix = f"substitution-{index:02d}-{app.lower().replace('_', '-')}"
            actions.extend(switch_actions(
                specs[app], f"REAL_REUSE_SUBSTITUTION_{index:02d}", 0.4,
                operate=False,
            ))
            actions.extend([
                helper_action([
                    "snapshot", "--cgroup", str(cgroup), "--apps", app_text,
                    "--label", f"{prefix}_before",
                    "--output", str(run_dir / f"{prefix}-before.json"),
                ], f"REAL_REUSE_SUBSTITUTION_{index:02d}_SNAPSHOT_BEFORE"),
                reuse_fixture_action(
                    sockets[app], "TOUCH_CLEAN",
                    f"REAL_REUSE_SUBSTITUTION_{index:02d}_CLEAN_{app}",
                ),
                helper_action([
                    "snapshot", "--cgroup", str(cgroup), "--apps", app_text,
                    "--label", f"{prefix}_after_clean",
                    "--output", str(run_dir / f"{prefix}-after-clean.json"),
                ], f"REAL_REUSE_SUBSTITUTION_{index:02d}_SNAPSHOT_CLEAN"),
                reuse_fixture_action(
                    sockets[app], "TOUCH_CLEAN_WARM",
                    f"REAL_REUSE_SUBSTITUTION_{index:02d}_WARM_{app}",
                ),
                helper_action([
                    "snapshot", "--cgroup", str(cgroup), "--apps", app_text,
                    "--label", f"{prefix}_after_warm",
                    "--output", str(run_dir / f"{prefix}-after-warm.json"),
                ], f"REAL_REUSE_SUBSTITUTION_{index:02d}_SNAPSHOT_WARM"),
            ])
    if scenario_name not in {"cold_retire", "r1_app_cold_retire"}:
        actions.append(helper_action([
            "snapshot", "--cgroup", str(cgroup), "--apps", app_text,
            "--label", "after_reuse", "--output", str(run_dir / "snapshot-after-reuse.json"),
        ], "REAL_SNAPSHOT_AFTER_REUSE"))

    actions.extend([
        {
            "type": "trace_marker", "event_type": "REAL_PRESSURE_DONE",
            "status": "success", "label": "REAL_PRESSURE_DONE",
        },
        *([] if app_native else [{
            "type": "close", "name": "real-pressure", "label": "REAL_PRESSURE_STOP",
        }]),
        ACCEPT.trace_action(trace_instance, "disable", "REAL_TRACE_DISABLE"),
    ])
    for app in reversed(apps):
        actions.append(ACCEPT.app_close_action(specs[app]))
    actions.append({
        "type": "trace_marker", "event_type": "REAL_PC_DONE",
        "status": "success", "label": "REAL_PC_DONE",
    })
    return {
        "description": f"Real desktop {scenario_name} validation. lzx-note",
        "validation_mode": True,
        "metadata": {
            "schema_version": 1, "workload_kind": "real_gui_applications",
            "scenario": scenario_name, "seed": seed, "apps": apps,
            "hot_apps": hot, "cold_apps": cold,
            "trained_history": config["trained_history"],
            "synthetic_app_working_set": controlled_fixture,
            "working_set_kind": (
                "application_native_ui" if app_native
                else
                "controlled_exact_page_reuse" if exact_reuse
                else "controlled_clean_dirty_substitution" if substitution
                else "controlled_workload_matrix" if workload_matrix
                else "controlled_cold_dirty_file" if cold_dirty_reclaim
                else "native_gui_only"
            ),
            "pressure_kind": (
                "memory_reclaim_of_application_native_working_sets" if app_native
                else "global_memfree_reclaim_probe" if global_pressure
                else "controlled_memcg_allocator"
            ),
        },
        "actions": actions,
    }


def _trace_rows(path: Path) -> list[dict[str, str]]:
    try:
        with path.open(encoding="utf-8", newline="") as stream:
            return list(csv.DictReader(stream))
    except (FileNotFoundError, OSError):
        return []


def _programmed_pacing_ms(action: dict[str, Any]) -> float:
    """Return deliberate harness pacing that is not application response time."""
    action_type = str(action.get("type", ""))
    if action_type == "wait":
        return max(0.0, float(action.get("seconds", 1.0))) * 1000.0
    if action_type in {"key", "hotkey"}:
        repeat = max(1, int(action.get("repeat", 1)))
        interval = max(0.0, float(action.get("interval", 0.1)))
        return max(0, repeat - 1) * interval * 1000.0
    if action_type in {"type", "text"}:
        text = str(action.get("text", ""))
        repeat = max(1, int(action.get("repeat", 1)))
        delay_ms = max(0, int(action.get("delay_ms", 20)))
        return max(0, len(text) - 1) * repeat * delay_ms
    if action_type == "wait_cgroup_pagein_stable":
        return max(0.0, float(action.get("minimum_wait_seconds", 0.2))) * 1000.0
    return 0.0  # lzx-note


def _actions_by_label(scenario: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    if not scenario:
        return {}
    return {
        str(action.get("label")): action
        for action in scenario.get("actions", []) if action.get("label")
    }


def parse_visual_ready_spans(
    path: Path, scenario: dict[str, Any] | None,
    prefixes: tuple[str, ...] = ("REAL_REUSE",),
) -> list[dict[str, Any]]:
    """Measure switch request through rendered-window stability per application."""
    rows = _trace_rows(path)
    action_map = _actions_by_label(scenario)
    starts: dict[str, tuple[int, str]] = {}
    spans: list[dict[str, Any]] = []
    for row in rows:
        label = row.get("label", "")
        if not label.startswith(prefixes):
            continue
        try:
            timestamp = int(row.get("ts_ns", "0"))
        except ValueError:
            continue
        if row.get("phase") == "start" and "_SWITCH_" in label:
            base = label.split("_SWITCH_", 1)[0]
            starts[base] = (timestamp, row.get("app_key", ""))
        elif row.get("phase") == "end" and "_READY_" in label:
            base = label.split("_READY_", 1)[0]
            if base not in starts:
                continue
            start, app = starts.pop(base)
            pacing = sum(
                _programmed_pacing_ms(action)
                for action_label, action in action_map.items()
                if action_label.startswith(base)
            )
            gross = (timestamp - start) / 1_000_000.0
            endpoint_action = action_map.get(label, {})
            endpoint = endpoint_action.get("metadata", {}).get(
                "latency_endpoint", "rendered_window_stable",
            )
            spans.append({
                "group": base, "app": app, "status": row.get("status"),
                "gross_ms": gross, "programmed_pacing_ms": pacing,
                "net_responsive_ms": max(0.0, gross - pacing),
                "endpoint": endpoint,
            })
    return spans  # lzx-note


def parse_fairness_pagein_waits(
    path: Path, scenario: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    """Sum the dynamic part of R7 cgroup page-in waits per app/phase."""
    action_map = _actions_by_label(scenario)
    starts: dict[str, int] = {}
    grouped: dict[str, list[dict[str, float]]] = {}
    pattern = re.compile(r"^(REAL_(?:FAIR_WARM|REUSE_FAIR)_\d{2})")
    for row in _trace_rows(path):
        label = row.get("label", "")
        action = action_map.get(label, {})
        if action.get("type") != "wait_cgroup_pagein_stable":
            continue
        match = pattern.match(label)
        if match is None:
            continue
        try:
            timestamp = int(row.get("ts_ns", "0"))
        except ValueError:
            continue
        key = row.get("step_id", "") + "|" + label
        if row.get("phase") == "start":
            starts[key] = timestamp
        elif row.get("phase") == "end" and key in starts:
            gross = (timestamp - starts.pop(key)) / 1_000_000.0
            pacing = _programmed_pacing_ms(action)
            grouped.setdefault(match.group(1), []).append({
                "gross_ms": gross,
                "programmed_guard_ms": pacing,
                "net_pagein_wait_ms": max(0.0, gross - pacing),
            })
    return {
        group: {
            "samples": len(samples), "actions": samples,
            "gross_ms": sum(item["gross_ms"] for item in samples),
            "programmed_guard_ms": sum(
                item["programmed_guard_ms"] for item in samples
            ),
            "net_pagein_wait_ms": sum(
                item["net_pagein_wait_ms"] for item in samples
            ),
        }
        for group, samples in grouped.items()
    }  # lzx-note


def parse_serial_reuse_latencies(path: Path) -> list[dict[str, Any]]:
    """Measure every synchronous R6 page-touch request through title acknowledgement."""
    starts: dict[str, int] = {}
    samples: list[dict[str, Any]] = []
    for row in _trace_rows(path):
        label = row.get("label", "")
        if not label.startswith("REAL_REUSE_SERIAL_STEP_"):
            continue
        try:
            timestamp = int(row.get("ts_ns", "0"))
        except ValueError:
            continue
        if row.get("phase") == "start" and "_REQUEST_" in label:
            starts[label.split("_REQUEST_", 1)[0]] = timestamp
        elif row.get("phase") == "end" and "_READY_" in label:
            base = label.split("_READY_", 1)[0]
            if base in starts:
                samples.append({
                    "step": int(base.rsplit("_", 1)[1]),
                    "app": row.get("app_key"), "status": row.get("status"),
                    "latency_ms": (timestamp - starts.pop(base)) / 1_000_000.0,
                    "endpoint": "browser_main_thread_serial_page_touch_done",
                })
    return sorted(samples, key=lambda item: item["step"])  # lzx-note


def parse_action_latencies(
    path: Path, scenario: dict[str, Any] | None = None,
) -> dict[str, Any]:
    rows = _trace_rows(path)
    if not rows:
        return {"samples": 0, "actions": [], "responsive_spans": []}
    action_map = _actions_by_label(scenario)
    starts: dict[str, int] = {}
    samples: list[dict[str, Any]] = []
    for row in rows:
        label = row.get("label", "")
        if not label.startswith("REAL_REUSE"):
            continue
        if "_DWELL_" in label or "RENDER_SETTLE" in label:
            continue
        key = row.get("step_id", "") + "|" + label
        try:
            timestamp = int(row.get("ts_ns", "0"))
        except ValueError:
            continue
        if row.get("phase") == "start":
            starts[key] = timestamp
        elif row.get("phase") == "end" and key in starts:
            duration = (timestamp - starts[key]) / 1_000_000.0
            pacing = _programmed_pacing_ms(action_map.get(label, {}))
            samples.append({
                "label": label, "app": row.get("app_key"), "action": row.get("action"),
                "status": row.get("status"),
                "duration_ms": duration, "programmed_pacing_ms": pacing,
                "net_duration_ms": max(0.0, duration - pacing),
            })
    responsive = parse_visual_ready_spans(path, scenario)
    return {
        "samples": len(samples),
        "actions": samples,
        "successful_total_ms": sum(
            item["duration_ms"] for item in samples if item["status"] == "success"
        ),
        "successful_net_total_ms": sum(
            item["net_duration_ms"] for item in samples if item["status"] == "success"
        ),
        "responsive_spans": responsive,
        "responsive_gross_total_ms": sum(
            item["gross_ms"] for item in responsive if item["status"] == "success"
        ),
        "responsive_net_total_ms": sum(
            item["net_responsive_ms"] for item in responsive
            if item["status"] == "success"
        ),
    }


def serial_major_reuse_payload(
    config: dict[str, Any], policy: str, trace_path: Path,
    reuse: dict[str, Any],
) -> dict[str, Any]:
    samples = parse_serial_reuse_latencies(trace_path)
    expected_steps = int(config["serial_reuse"]["allocation_mib"]) // int(
        config["serial_reuse"]["chunk_mib"]
    )
    firefox = reuse.get("apps", {}).get("FIREFOX", {})
    stats = firefox.get("stat_deltas", {})
    major = int(stats.get("pgmajfault", 0))
    refault = int(stats.get("workingset_refault_file", 0)) + int(
        stats.get("workingset_refault_anon", 0)
    )
    swapin = int(stats.get("pswpin", 0))
    minimum_native = int(config["serial_reuse"]["minimum_native_major_faults"])
    step_valid = (
        len(samples) == expected_steps
        and all(sample.get("status") == "success" for sample in samples)
    )
    fault_gate = policy != "native_kernel" or major >= minimum_native
    return {
        "schema_version": 1, "valid": step_valid and fault_gate,
        "steps_expected": expected_steps, "steps_observed": len(samples),
        "allocation_mib": int(config["serial_reuse"]["allocation_mib"]),
        "chunk_mib": int(config["serial_reuse"]["chunk_mib"]),
        "access_order": "deterministic_page_stride_on_browser_main_thread",
        "samples": samples,
        "total_latency_ms": sum(item["latency_ms"] for item in samples),
        "maximum_step_latency_ms": max(
            (item["latency_ms"] for item in samples), default=None
        ),
        "major_faults": major, "refaults": refault, "swapins": swapin,
        "minimum_native_major_faults": minimum_native,
        "native_fault_gate_applied": policy == "native_kernel",
        "native_fault_gate_valid": fault_gate,
    }  # lzx-note


def _jain_index(values: list[float]) -> float | None:
    positive = [max(0.0, value) for value in values]
    denominator = len(positive) * sum(value * value for value in positive)
    return (sum(positive) ** 2 / denominator) if denominator else None


def fairness_misprediction_payload(
    config: dict[str, Any], policy: str, trace_path: Path, scenario: dict[str, Any],
    reclaim: dict[str, Any], reuse: dict[str, Any], prediction_gate: dict[str, Any],
) -> dict[str, Any]:
    contract = config["fairness_misprediction"]
    participants = list(contract["participants"])
    unexpected = str(contract["unexpected_reuse_app"])
    spans = parse_visual_ready_spans(
        trace_path, scenario, prefixes=("REAL_FAIR_WARM_", "REAL_REUSE_FAIR_"),
    )
    pagein_waits = parse_fairness_pagein_waits(trace_path, scenario)
    apps: dict[str, Any] = {}
    for app in participants:
        warm = next(
            (row for row in spans if row["app"] == app and row["group"].startswith("REAL_FAIR_WARM_")),
            None,
        )
        post = next(
            (row for row in spans if row["app"] == app and row["group"].startswith("REAL_REUSE_FAIR_")),
            None,
        )
        warm_ms = float(warm["net_responsive_ms"]) if warm else None
        post_ms = float(post["net_responsive_ms"]) if post else None
        warm_pagein = pagein_waits.get(
            str(warm.get("group", "")) if warm else "", {},
        )
        post_pagein = pagein_waits.get(
            str(post.get("group", "")) if post else "", {},
        )
        warm_pagein_ms = float(warm_pagein.get("net_pagein_wait_ms", 0.0))
        post_pagein_ms = float(post_pagein.get("net_pagein_wait_ms", 0.0))
        slowdown = (
            post_ms / warm_ms if warm_ms is not None and post_ms is not None and warm_ms > 0
            else None
        )
        app_reuse = reuse.get("apps", {}).get(app, {})
        app_reclaim = reclaim.get("apps", {}).get(app, {})
        stats = app_reuse.get("stat_deltas", {})
        apps[app] = {
            "warm": warm, "post_reclaim": post,
            "normalized_slowdown": slowdown,
            "warm_pagein_recovery": warm_pagein,
            "post_reclaim_pagein_recovery": post_pagein,
            "additional_pagein_recovery_ms": max(
                0.0, post_pagein_ms - warm_pagein_ms,
            ),
            "reclaimed_before_reuse_bytes": int(
                app_reclaim.get("memory_current_drop_bytes", 0)
            ),
            "pgfault": int(stats.get("pgfault", 0)),
            "major_fault": int(stats.get("pgmajfault", 0)),
            "refault": int(stats.get("workingset_refault_file", 0))
            + int(stats.get("workingset_refault_anon", 0)),
            "swapin": int(stats.get("pswpin", 0)),
            "psi_some_us": int(
                app_reuse.get("psi_stall_us", {}).get("some_total", 0)
            ),
        }
    slowdowns = [
        float(row["normalized_slowdown"])
        for row in apps.values() if row["normalized_slowdown"] is not None
    ]
    responsiveness = [1.0 / value for value in slowdowns if value > 0]
    observed = prediction_gate.get("observed", {}).get(
        ACCEPT.LSAPP_NAME_BY_APP_KEY[unexpected], {}
    )
    probability = float(observed.get("probability", 1.0))
    maximum = float(contract.get("maximum_unexpected_probability", 0.01))
    unexpected_cost = apps.get(unexpected, {})
    unexpected_reclaimed = int(
        unexpected_cost.get("reclaimed_before_reuse_bytes", 0)
    )
    minimum_reclaimed = int(contract["minimum_unexpected_reclaimed_mib"]) * MIB
    unexpected_faults = int(unexpected_cost.get("major_fault", 0)) + int(
        unexpected_cost.get("refault", 0)
    )
    minimum_native_faults = int(contract["minimum_native_unexpected_faults"])
    native_fault_gate = policy != "native_kernel" or (
        unexpected_faults >= minimum_native_faults
    )
    complete = all(
        row.get("warm") is not None and row.get("post_reclaim") is not None
        for row in apps.values()
    )
    return {
        "schema_version": 1,
        "valid": (
            complete and probability <= maximum
            and unexpected_reclaimed >= minimum_reclaimed
            and native_fault_gate
        ),
        "participants": participants, "apps": apps,
        "normalized_responsiveness_jain_index": _jain_index(responsiveness),
        "maximum_normalized_slowdown": max(slowdowns, default=None),
        "unexpected_reuse_app": unexpected,
        "unexpected_prediction_rank": observed.get("rank"),
        "unexpected_prediction_probability": probability,
        "maximum_unexpected_probability": maximum,
        "misprediction_by_design": probability <= maximum,
        "unexpected_app_cost": unexpected_cost,
        "minimum_unexpected_reclaimed_bytes": minimum_reclaimed,
        "unexpected_reclaim_gate_valid": unexpected_reclaimed >= minimum_reclaimed,
        "unexpected_major_plus_refault": unexpected_faults,
        "minimum_native_unexpected_faults": minimum_native_faults,
        "native_unexpected_fault_gate_applied": policy == "native_kernel",
        "native_unexpected_fault_gate_valid": native_fault_gate,
        "interpretation": (
            "compare Jain index and each normalized slowdown for fairness; "
            "compare unexpected_app_cost faults and additional_pagein_recovery_ms "
            "Native versus APPLY for wrong-prediction cost"
        ),
    }  # lzx-note


def policy_stat_delta(before: str, after: str) -> dict[str, int]:
    def parse(value: str) -> dict[str, int]:
        result: dict[str, int] = {}
        for line in value.splitlines():
            fields = line.split()
            if len(fields) == 2:
                try:
                    result[fields[0]] = int(fields[1])
                except ValueError:
                    pass
        return result
    first, last = parse(before), parse(after)
    return {key: last.get(key, 0) - first.get(key, 0) for key in sorted(set(first) | set(last))}


def parse_exact_fixture(path: Path) -> dict[str, Any]:
    try:
        with path.open(encoding="utf-8", newline="") as stream:
            rows = list(csv.DictReader(stream))
    except (FileNotFoundError, OSError):
        rows = []
    selected = [
        {
            "command": row.get("command"),
            "latency_us": int(row.get("latency_us") or 0),
            "touched_bytes": int(row.get("touched_bytes") or 0),
            "status": row.get("status"),
        }
        for row in rows
        if row.get("command") in {"TOUCH_FILE", "TOUCH_ANON", "TOUCH_BOTH"}
    ]
    return {"samples": len(selected), "actions": selected}  # lzx-note


def fixture_detail_values(value: str) -> dict[str, int]:
    result: dict[str, int] = {}
    for field in value.split():
        if "=" not in field:
            continue
        key, raw = field.split("=", 1)
        try:
            result[key] = int(raw)
        except ValueError:
            continue
    return result  # lzx-note


def cold_dirty_payload(config: dict[str, Any], run_dir: Path) -> dict[str, Any]:
    """Measure exact per-app eviction of cold dirty file pages. lzx-note"""
    try:
        before = read_json(run_dir / "snapshot-before-pressure.json")
        after = read_json(run_dir / "snapshot-under-pressure.json")
        reclaim = comparison_payload(before, after, set(config["hot_apps"]))
    except (FileNotFoundError, OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        return {
            "schema_version": 1, "valid": False, "apps": {},
            "reason": f"{type(exc).__name__}:{exc}",
        }

    apps: dict[str, Any] = {}
    for app in config["apps"]:
        log_path = run_dir / "reuse-working-sets" / f"{app.lower().replace('_', '-')}.csv"
        try:
            with log_path.open(encoding="utf-8", newline="") as stream:
                rows = list(csv.DictReader(stream))
        except (FileNotFoundError, OSError):
            rows = []
        by_command = {
            row.get("command", ""): row for row in rows
            if row.get("command") in {
                "COLD_DIRTY_FILE", "RESIDENCY_BEFORE", "RESIDENCY_AFTER",
            }
        }
        created = by_command.get("COLD_DIRTY_FILE", {})
        resident_before = by_command.get("RESIDENCY_BEFORE", {})
        resident_after = by_command.get("RESIDENCY_AFTER", {})
        first_detail = fixture_detail_values(resident_before.get("detail", ""))
        last_detail = fixture_detail_values(resident_after.get("detail", ""))
        expected_bytes = int(config["reuse_layout"][app]["file_mib"]) * MIB
        expected_pages = expected_bytes // int(os.sysconf("SC_PAGE_SIZE"))
        first_pages = int(first_detail.get("resident_file_pages", -1))
        last_pages = int(last_detail.get("resident_file_pages", -1))
        evicted_pages = max(0, first_pages - last_pages) if first_pages >= 0 else 0
        first_stat = before["apps"].get(app, {}).get("memory_stat", {})
        last_stat = after["apps"].get(app, {}).get("memory_stat", {})
        dirty_before = int(first_stat.get("file_dirty", 0))
        disk_file_before = max(
            0, int(first_stat.get("file", 0)) - int(first_stat.get("shmem", 0)),
        )
        disk_file_after = max(
            0, int(last_stat.get("file", 0)) - int(last_stat.get("shmem", 0)),
        )
        row_valid = (
            created.get("status") == "OK"
            and resident_before.get("status") == "OK"
            and resident_after.get("status") == "OK"
            and first_pages >= int(expected_pages * 0.95)
            and 0 <= last_pages <= first_pages
            and dirty_before >= int(expected_bytes * 0.80)
            and disk_file_before >= int(expected_bytes * 0.95)
        )
        apps[app] = {
            "valid": row_valid,
            "class": "hot" if app in config["hot_apps"] else "cold",
            "expected_file_bytes": expected_bytes,
            "dirty_bytes_before": dirty_before,
            "file_writeback_bytes_before": int(first_stat.get("file_writeback", 0)),
            "file_writeback_bytes_after": int(last_stat.get("file_writeback", 0)),
            "disk_file_bytes_before": disk_file_before,
            "disk_file_bytes_after": disk_file_after,
            "resident_pages_before": first_pages,
            "resident_pages_after": last_pages,
            "evicted_pages": evicted_pages,
            "evicted_bytes": evicted_pages * int(os.sysconf("SC_PAGE_SIZE")),
            "eviction_rate_percent": (
                100.0 * evicted_pages / first_pages if first_pages > 0 else None
            ),
            "io_write_bytes": int(reclaim["apps"][app]["io_deltas"].get("wbytes", 0)),
            "pgscan": int(reclaim["apps"][app]["stat_deltas"].get("pgscan", 0)),
            "pgsteal": int(reclaim["apps"][app]["stat_deltas"].get("pgsteal", 0)),
        }

    hot_rows = [row for row in apps.values() if row["class"] == "hot"]
    cold_rows = [row for row in apps.values() if row["class"] == "cold"]

    def class_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
        resident = sum(max(0, int(row["resident_pages_before"])) for row in rows)
        evicted = sum(int(row["evicted_pages"]) for row in rows)
        scanned = sum(int(row["pgscan"]) for row in rows)
        stolen = sum(int(row["pgsteal"]) for row in rows)
        return {
            "resident_pages_before": resident,
            "evicted_pages": evicted,
            "evicted_bytes": evicted * int(os.sysconf("SC_PAGE_SIZE")),
            "eviction_rate_percent": 100.0 * evicted / resident if resident else None,
            "io_write_bytes": sum(int(row["io_write_bytes"]) for row in rows),
            "pgscan": scanned,
            "pgsteal": stolen,
            "scan_efficiency_percent": 100.0 * stolen / scanned if scanned else None,
        }

    hot_summary = class_summary(hot_rows)
    cold_summary = class_summary(cold_rows)
    total_evicted = hot_summary["evicted_pages"] + cold_summary["evicted_pages"]
    reclaim_target = int(config["reclaim_target_mib"]) * MIB
    cold_capacity = sum(
        int(config["reuse_layout"][app]["file_mib"]) * MIB
        for app in config["cold_apps"]
    )
    return {
        "schema_version": 1,
        "valid": (
            all(row["valid"] for row in apps.values())
            and cold_capacity >= reclaim_target
        ),
        "apps": apps,
        "hot": hot_summary,
        "cold": cold_summary,
        "cold_eviction_source_percent": (
            100.0 * cold_summary["evicted_pages"] / total_evicted
            if total_evicted else None
        ),
        "cold_dirty_capacity_bytes": cold_capacity,
        "reclaim_target_bytes": reclaim_target,
        "cold_capacity_ratio": cold_capacity / reclaim_target if reclaim_target else None,
        "parent_io_write_bytes": int(reclaim["parent_io_deltas"].get("wbytes", 0)),
        "parent_psi_stall_us": reclaim["psi_stall_us"],
        "contract": {
            "page_state": "materialized+mmap-dirty+MADV_COLD+resident",
            "residency_metric": "mincore",
            "cold_goal": "maximize predicted-cold dirty eviction",
            "hot_goal": "minimize predicted-hot dirty eviction",
        },
    }  # lzx-note


def substitution_payload(config: dict[str, Any], run_dir: Path) -> dict[str, Any]:
    """Attribute clean/dirty eviction and the exact hot clean-page replay. lzx-note"""
    try:
        before = read_json(run_dir / "snapshot-before-pressure.json")
        after = read_json(run_dir / "snapshot-under-pressure.json")
        reclaim = comparison_payload(before, after, set(config["hot_apps"]))
    except (FileNotFoundError, OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        return {
            "schema_version": 1, "valid": False, "apps": {}, "reuse_targets": {},
            "reason": f"{type(exc).__name__}:{exc}",
        }

    layout = config["substitution_layout"]
    required_dirty_apps = set(config.get("required_dirty_apps", config["apps"]))
    page_size = int(os.sysconf("SC_PAGE_SIZE"))
    apps: dict[str, Any] = {}
    for app in config["apps"]:
        slug = app.lower().replace("_", "-")
        log_path = run_dir / "reuse-working-sets" / f"{slug}.csv"
        try:
            with log_path.open(encoding="utf-8", newline="") as stream:
                rows = list(csv.DictReader(stream))
        except (FileNotFoundError, OSError):
            rows = []
        selected = {row.get("command", ""): row for row in rows}
        coldify = selected.get("COLDIFY", {})
        resident_before = selected.get("RESIDENCY_BEFORE", {})
        resident_after = selected.get("RESIDENCY_AFTER", {})
        first = fixture_detail_values(resident_before.get("detail", ""))
        last = fixture_detail_values(resident_after.get("detail", ""))
        sizes = layout[app]
        expected = {
            key: int(sizes[f"{key}_mib"]) * MIB for key in ("clean", "dirty", "hot")
        }
        expected_pages = {key: value // page_size for key, value in expected.items()}
        before_pages = {
            key: int(first.get(f"{key}_resident_pages", -1))
            for key in ("clean", "dirty", "hot")
        }
        after_pages = {
            key: int(last.get(f"{key}_resident_pages", -1))
            for key in ("clean", "dirty", "hot")
        }
        evicted_pages = {
            key: max(0, before_pages[key] - after_pages[key])
            if before_pages[key] >= 0 and after_pages[key] >= 0 else 0
            for key in ("clean", "dirty", "hot")
        }
        first_stat = before["apps"].get(app, {}).get("memory_stat", {})
        dirty_before = int(first_stat.get("file_dirty", 0))
        disk_file_before = max(
            0, int(first_stat.get("file", 0)) - int(first_stat.get("shmem", 0)),
        )
        row_valid = (
            coldify.get("status") == "OK"
            and resident_before.get("status") == "OK"
            and resident_after.get("status") == "OK"
            and all(
                before_pages[key] >= int(expected_pages[key] * 0.95)
                and 0 <= after_pages[key] <= before_pages[key]
                for key in ("clean", "dirty", "hot")
            )
            and (
                app not in required_dirty_apps
                or dirty_before >= int(expected["dirty"] * 0.80)
            )
            and disk_file_before >= int(sum(expected.values()) * 0.95)
        )
        apps[app] = {
            "valid": row_valid,
            "class": "hot" if app in config["hot_apps"] else "cold",
            "expected_bytes": expected,
            "resident_pages_before": before_pages,
            "resident_pages_after": after_pages,
            "evicted_pages": evicted_pages,
            "evicted_bytes": {
                key: value * page_size for key, value in evicted_pages.items()
            },
            "dirty_bytes_before": dirty_before,
            "disk_file_bytes_before": disk_file_before,
            "io_write_bytes": int(reclaim["apps"][app]["io_deltas"].get("wbytes", 0)),
            "pgscan": int(reclaim["apps"][app]["stat_deltas"].get("pgscan", 0)),
            "pgsteal": int(reclaim["apps"][app]["stat_deltas"].get("pgsteal", 0)),
            "psi_stall_us": reclaim["apps"][app]["psi_stall_us"],
        }

    def class_summary(class_name: str) -> dict[str, Any]:
        selected_apps = [row for row in apps.values() if row["class"] == class_name]
        resident = {
            key: sum(max(0, int(row["resident_pages_before"][key])) for row in selected_apps)
            for key in ("clean", "dirty", "hot")
        }
        evicted = {
            key: sum(int(row["evicted_pages"][key]) for row in selected_apps)
            for key in ("clean", "dirty", "hot")
        }
        return {
            "apps": len(selected_apps),
            "resident_pages_before": resident,
            "resident_bytes_before": {key: value * page_size for key, value in resident.items()},
            "evicted_pages": evicted,
            "evicted_bytes": {key: value * page_size for key, value in evicted.items()},
            "total_evicted_bytes": sum(evicted.values()) * page_size,
            "clean_preservation_percent": (
                100.0 * (resident["clean"] - evicted["clean"]) / resident["clean"]
                if resident["clean"] else None
            ),
            "io_write_bytes": sum(int(row["io_write_bytes"]) for row in selected_apps),
            "pgscan": sum(int(row["pgscan"]) for row in selected_apps),
            "pgsteal": sum(int(row["pgsteal"]) for row in selected_apps),
        }

    hot_summary = class_summary("hot")
    cold_summary = class_summary("cold")
    reclaim_target = int(config["reclaim_target_mib"]) * MIB
    cold_clean_capacity = sum(
        int(layout[app]["clean_mib"]) * MIB for app in config["cold_apps"]
    )
    cold_dirty_capacity = sum(
        int(layout[app]["dirty_mib"]) * MIB for app in config["cold_apps"]
    )
    hot_clean_capacity = sum(
        int(layout[app]["clean_mib"]) * MIB for app in config["hot_apps"]
    )
    capacity_contract = (
        cold_clean_capacity < reclaim_target <= cold_clean_capacity + cold_dirty_capacity
        and cold_clean_capacity + hot_clean_capacity >= reclaim_target
    )

    reuse_targets: dict[str, Any] = {}
    for index, app in enumerate(config.get("reuse_targets", []), start=1):
        slug = app.lower().replace("_", "-")
        prefix = f"substitution-{index:02d}-{slug}"
        try:
            phase_before = read_json(run_dir / f"{prefix}-before.json")
            phase_after = read_json(run_dir / f"{prefix}-after-clean.json")
            phase_warm = read_json(run_dir / f"{prefix}-after-warm.json")
            first_phase = comparison_payload(
                phase_before, phase_after, set(config["hot_apps"]),
            )
            warm_phase = comparison_payload(
                phase_after, phase_warm, set(config["hot_apps"]),
            )
            with (run_dir / "reuse-working-sets" / f"{slug}.csv").open(
                encoding="utf-8", newline="",
            ) as stream:
                fixture_rows = list(csv.DictReader(stream))
            actions = {
                row.get("command", ""): {
                    "status": row.get("status"),
                    "touched_bytes": int(row.get("touched_bytes") or 0),
                    "latency_us": int(row.get("latency_us") or 0),
                }
                for row in fixture_rows
                if row.get("command") in {"TOUCH_CLEAN", "TOUCH_CLEAN_WARM"}
            }
            expected_clean = int(layout[app]["clean_mib"]) * MIB
            row_valid = (
                app in config["hot_apps"]
                and first_phase["valid"] and warm_phase["valid"]
                and actions.get("TOUCH_CLEAN", {}).get("status") == "OK"
                and actions.get("TOUCH_CLEAN", {}).get("touched_bytes") == expected_clean
                and actions.get("TOUCH_CLEAN_WARM", {}).get("status") == "OK"
                and actions.get("TOUCH_CLEAN_WARM", {}).get("touched_bytes") == expected_clean
            )
            reuse_targets[app] = {
                "valid": row_valid,
                "expected_clean_bytes": expected_clean,
                "first_replay": {
                    "fixture": actions.get("TOUCH_CLEAN", {}),
                    "app": first_phase["apps"][app],
                    "parent_deltas": first_phase["parent_deltas"],
                    "psi_stall_us": first_phase["psi_stall_us"],
                },
                "warm_control": {
                    "fixture": actions.get("TOUCH_CLEAN_WARM", {}),
                    "app": warm_phase["apps"][app],
                    "parent_deltas": warm_phase["parent_deltas"],
                    "psi_stall_us": warm_phase["psi_stall_us"],
                },
            }
        except (FileNotFoundError, OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
            reuse_targets[app] = {
                "valid": False, "reason": f"{type(exc).__name__}:{exc}",
            }

    measured_evicted = hot_summary["total_evicted_bytes"] + cold_summary["total_evicted_bytes"]
    cold_measured_evicted = (
        int(cold_summary["evicted_bytes"]["clean"])
        + int(cold_summary["evicted_bytes"]["dirty"])
    )
    return {
        "schema_version": 1,
        "valid": (
            all(row["valid"] for row in apps.values())
            and bool(reuse_targets)
            and all(row.get("valid") for row in reuse_targets.values())
            and capacity_contract
        ),
        "apps": apps,
        "hot": hot_summary,
        "cold": cold_summary,
        "reuse_targets": reuse_targets,
        "reclaim_target_bytes": reclaim_target,
        "cold_clean_capacity_bytes": cold_clean_capacity,
        "cold_dirty_capacity_bytes": cold_dirty_capacity,
        "hot_clean_capacity_bytes": hot_clean_capacity,
        "required_dirty_substitution_bytes": max(0, reclaim_target - cold_clean_capacity),
        "capacity_contract_valid": capacity_contract,
        "cold_clean_plus_dirty_covers_target": (
            cold_clean_capacity + cold_dirty_capacity >= reclaim_target
        ),
        "clean_only_baseline_covers_target": (
            cold_clean_capacity + hot_clean_capacity >= reclaim_target
        ),
        "cold_measured_source_percent": (
            100.0 * cold_measured_evicted / measured_evicted if measured_evicted else None
        ),
        "hot_clean_preservation_percent": hot_summary["clean_preservation_percent"],
        "parent_io_write_bytes": int(reclaim["parent_io_deltas"].get("wbytes", 0)),
        "parent_psi_stall_us": reclaim["psi_stall_us"],
        "contract": {
            "hypothesis": "cold clean+dirty eviction substitutes for hot clean-cold eviction",
            "clean_state": "materialized+fsync+MADV_COLD+resident",
            "dirty_state": "materialized+fsync+rewrite+MADV_COLD+resident",
            "exact_eviction_metric": "per-file mincore resident-page loss",
            "benefit_metric": "exact hot clean replay refaults+major_faults+latency+PSI",
            "validity_not_success": "a valid round may falsify the mechanism hypothesis",
        },
    }  # lzx-note


def workload_matrix_payload(config: dict[str, Any], run_dir: Path) -> dict[str, Any]:
    """Prove per-cgroup anon/file selection with exact mincore evidence.  lzx-note"""
    layout = controlled_layout(config, "workload_matrix_reclaim")
    expected = {str(key): str(value) for key, value in config.get("workload_expected_classes", {}).items()}
    dirty_apps = set(config.get("workload_dirty_apps", ()))
    try:
        before = read_json(run_dir / "snapshot-before-pressure.json")
        after = read_json(run_dir / "snapshot-under-pressure.json")
        reclaim = comparison_payload(before, after, set(config["hot_apps"]))
    except (FileNotFoundError, OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        return {"schema_version": 1, "valid": False, "reason": f"{type(exc).__name__}:{exc}"}

    page_size = int(os.sysconf("SC_PAGE_SIZE"))
    apps: dict[str, Any] = {}
    by_workload: dict[str, list[dict[str, Any]]] = {}
    for app in config["apps"]:
        sizes = layout.get(app, {})
        file_bytes = int(sizes.get("file_mib", 0)) * MIB
        anon_bytes = int(sizes.get("anon_mib", 0)) * MIB
        file_pages = file_bytes // page_size
        anon_pages = anon_bytes // page_size
        log_path = run_dir / "reuse-working-sets" / f"{app.lower().replace('_', '-')}.csv"
        try:
            with log_path.open(encoding="utf-8", newline="") as stream:
                log_rows = list(csv.DictReader(stream))
        except (FileNotFoundError, OSError):
            log_rows = []
        selected = {row.get("command", ""): row for row in log_rows}
        before_detail = fixture_detail_values(selected.get("RESIDENCY_BEFORE", {}).get("detail", ""))
        after_detail = fixture_detail_values(selected.get("RESIDENCY_AFTER", {}).get("detail", ""))
        before_file = int(before_detail.get("resident_file_pages", -1))
        after_file = int(after_detail.get("resident_file_pages", -1))
        before_anon = int(before_detail.get("resident_anon_pages", -1))
        after_anon = int(after_detail.get("resident_anon_pages", -1))
        evicted_file = max(0, before_file - after_file) if before_file >= 0 else 0
        evicted_anon = max(0, before_anon - after_anon) if before_anon >= 0 else 0
        scope_before = before["apps"].get(app, {}).get("memory_stat", {})
        observed_anon = int(scope_before.get("anon", 0))
        observed_file = max(0, int(scope_before.get("file", 0)) - int(scope_before.get("shmem", 0)))
        observed_dirty = int(scope_before.get("file_dirty", 0))
        workload = expected.get(app, "")
        observed_total = max(1, observed_file + observed_anon)
        observed_anon_ratio = observed_anon / observed_total
        observed_file_ratio = observed_file / observed_total
        state_valid = (
            workload == "ANON_HEAVY" and observed_anon >= int(anon_bytes * .95)
            and observed_anon_ratio >= .65
        ) or (
            workload == "FILE_CLEAN" and observed_file >= int(file_bytes * .95)
            and observed_file_ratio >= .65
            and observed_dirty < int(file_bytes * .25)
        ) or (
            workload == "FILE_DIRTY" and observed_file >= int(file_bytes * .95)
            and observed_dirty >= int(file_bytes * .80)
        ) or (
            workload == "MIXED" and observed_file >= int(file_bytes * .95)
            and observed_anon >= int(anon_bytes * .95)
            # Mirror reclaim_workload.classify_memory_stat(): GUI overhead is
            # part of the bound cgroup, so a valid MIXED profile is one where
            # neither resident class reaches the 65% dominant threshold, not
            # one whose fixture bytes happen to be exactly 50/50. lzx-note
            and observed_anon_ratio < .65 and observed_file_ratio < .65
        )
        residency_valid = (
            before_file >= int(file_pages * .95) and 0 <= after_file <= before_file
            and before_anon >= int(anon_pages * .95) and 0 <= after_anon <= before_anon
        )
        dirty_valid = app not in dirty_apps or selected.get("COLD_DIRTY_FILE", {}).get("status") == "OK"
        row = {
            "valid": ((state_valid and dirty_valid) if workload else True) and residency_valid,
            "class": "hot" if app in config["hot_apps"] else "cold",
            "expected_workload": workload,
            "expected_file_bytes": file_bytes,
            "expected_anon_bytes": anon_bytes,
            "observed_file_bytes": observed_file,
            "observed_anon_bytes": observed_anon,
            "observed_file_dirty_bytes": observed_dirty,
            "observed_file_ratio": observed_file_ratio,
            "observed_anon_ratio": observed_anon_ratio,
            "resident_file_pages_before": before_file,
            "resident_file_pages_after": after_file,
            "resident_anon_pages_before": before_anon,
            "resident_anon_pages_after": after_anon,
            "evicted_file_bytes": evicted_file * page_size,
            "evicted_anon_bytes": evicted_anon * page_size,
            "file_eviction_rate_percent": 100.0 * evicted_file / before_file if before_file else None,
            "anon_eviction_rate_percent": 100.0 * evicted_anon / before_anon if before_anon else None,
            "pgscan": int(reclaim["apps"][app]["stat_deltas"].get("pgscan", 0)),
            "pgsteal": int(reclaim["apps"][app]["stat_deltas"].get("pgsteal", 0)),
            "pswpin": int(reclaim["apps"][app]["stat_deltas"].get("pswpin", 0)),
            "pswpout": int(reclaim["apps"][app]["stat_deltas"].get("pswpout", 0)),
            "refault_file": int(reclaim["apps"][app]["stat_deltas"].get("workingset_refault_file", 0)),
            "refault_anon": int(reclaim["apps"][app]["stat_deltas"].get("workingset_refault_anon", 0)),
        }
        apps[app] = row
        if workload:
            by_workload.setdefault(workload, []).append(row)

    summaries: dict[str, Any] = {}
    for workload, rows in by_workload.items():
        file_evicted = sum(int(row["evicted_file_bytes"]) for row in rows)
        anon_evicted = sum(int(row["evicted_anon_bytes"]) for row in rows)
        primary = anon_evicted if workload == "ANON_HEAVY" else file_evicted
        total = file_evicted + anon_evicted
        summaries[workload] = {
            "apps": len(rows), "file_evicted_bytes": file_evicted,
            "anon_evicted_bytes": anon_evicted,
            "primary_evicted_bytes": primary,
            "primary_share_percent": 100.0 * primary / total if total else None,
        }
    cold_capacity = sum(
        (int(layout[app]["file_mib"]) + int(layout[app]["anon_mib"])) * MIB
        for app in config["cold_apps"]
    )
    return {
        "schema_version": 1,
        "valid": (
            set(expected) == set(config["cold_apps"])
            and all(row["valid"] for row in apps.values())
            and {"ANON_HEAVY", "FILE_CLEAN", "FILE_DIRTY", "MIXED"}.issubset(by_workload)
            and cold_capacity >= int(config["reclaim_target_mib"]) * MIB
        ),
        "apps": apps,
        "workloads": summaries,
        "cold_capacity_bytes": cold_capacity,
        "reclaim_target_bytes": int(config["reclaim_target_mib"]) * MIB,
        "psi_stall_us": reclaim["psi_stall_us"],
        "contract": {
            "per_cgroup_input": "memory.stat anon/file/file_dirty",
            "exact_eviction_metric": "fixture mincore resident pages",
            "selection_metric": "anonymous/file evicted bytes by workload class",
            "safety_metrics": ["workingset_refault_file", "workingset_refault_anon", "pswpin", "PSI"],
        },
    }  # lzx-note


def exact_reuse_payload(config: dict[str, Any], run_dir: Path) -> dict[str, Any]:
    targets: dict[str, Any] = {}
    for index, app in enumerate(config.get("reuse_targets", []), start=1):
        prefix = f"exact-{index:02d}-{app.lower().replace('_', '-')}"
        paths = {
            "before": run_dir / f"{prefix}-before.json",
            "after_file": run_dir / f"{prefix}-after-file.json",
            "after_anon": run_dir / f"{prefix}-after-anon.json",
            "after_warm": run_dir / f"{prefix}-after-warm.json",
        }
        try:
            snapshots = {key: read_json(path) for key, path in paths.items()}
            file_phase = comparison_payload(
                snapshots["before"], snapshots["after_file"], set(config["hot_apps"]),
            )
            anon_phase = comparison_payload(
                snapshots["after_file"], snapshots["after_anon"], set(config["hot_apps"]),
            )
            warm_phase = comparison_payload(
                snapshots["after_anon"], snapshots["after_warm"], set(config["hot_apps"]),
            )
            targets[app] = {
                "valid": all(item["valid"] for item in (file_phase, anon_phase, warm_phase)),
                "file_phase": {
                    "app": file_phase["apps"][app],
                    "parent_deltas": file_phase["parent_deltas"],
                    "psi_stall_us": file_phase["psi_stall_us"],
                },
                "anon_phase": {
                    "app": anon_phase["apps"][app],
                    "parent_deltas": anon_phase["parent_deltas"],
                    "psi_stall_us": anon_phase["psi_stall_us"],
                },
                "warm_control": {
                    "app": warm_phase["apps"][app],
                    "parent_deltas": warm_phase["parent_deltas"],
                    "psi_stall_us": warm_phase["psi_stall_us"],
                },
                "fixture": parse_exact_fixture(
                    run_dir / "reuse-working-sets" / f"{app.lower().replace('_', '-')}.csv"
                ),
            }
        except (FileNotFoundError, OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
            targets[app] = {"valid": False, "reason": f"{type(exc).__name__}:{exc}"}
    charge_evidence: dict[str, Any] = {"valid": False, "apps": {}}
    try:
        before_pressure = read_json(run_dir / "snapshot-before-pressure.json")
        charge_evidence["valid"] = True
        for app, sizes in config["reuse_layout"].items():
            stat = before_pressure["apps"][app]["memory_stat"]
            expected_file = int(sizes["file_mib"]) * MIB
            expected_anon = int(sizes["anon_mib"]) * MIB
            observed_anon = int(stat.get("anon", 0))
            # cgroup-v2's `file` includes shmem.  Subtract it so a shared
            # anonymous mapping cannot masquerade as the materialized file.
            # lzx-note
            observed_file = max(0, int(stat.get("file", 0)) - int(stat.get("shmem", 0)))
            row_valid = (
                observed_file >= int(expected_file * 0.95)
                and observed_anon >= int(expected_anon * 0.95)
            )
            charge_evidence["apps"][app] = {
                "valid": row_valid,
                "expected_file_bytes": expected_file,
                "observed_disk_file_bytes": observed_file,
                "expected_private_anon_bytes": expected_anon,
                "observed_anon_bytes": observed_anon,
                "observed_shmem_bytes": int(stat.get("shmem", 0)),
            }
            charge_evidence["valid"] = charge_evidence["valid"] and row_valid
    except (FileNotFoundError, OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        charge_evidence = {
            "valid": False, "apps": {},
            "reason": f"{type(exc).__name__}:{exc}",
        }
    cold_capacity = sum(
        (int(config["reuse_layout"][app]["file_mib"])
         + int(config["reuse_layout"][app]["anon_mib"])) * MIB
        for app in config["cold_apps"]
    )
    reclaim_target = int(config["reclaim_target_mib"]) * MIB
    return {
        "schema_version": 1,
        "valid": bool(targets) and all(row.get("valid") for row in targets.values())
        and bool(charge_evidence.get("valid")) and cold_capacity >= reclaim_target,
        "targets": targets,
        "charge_evidence": charge_evidence,
        "cold_capacity_bytes": cold_capacity,
        "reclaim_target_bytes": reclaim_target,
        "cold_capacity_ratio": cold_capacity / reclaim_target if reclaim_target else None,
        "contract": {
            "file_refault_metric": "workingset_refault_file",
            "anonymous_refault_metric": "workingset_refault_anon",
            "anonymous_swapin_metric": "pswpin",
            "anonymous_mapping": "MAP_PRIVATE|MAP_ANONYMOUS",
            "stall_metrics": ["some_total", "full_total"],
            "warm_control_expected": "near-zero new faults/refaults after exact replay",
        },
    }  # lzx-note


def run_one(
    config: dict[str, Any], scenario_name: str, policy: str,
    seed: int, run_dir: Path, expected_plan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    run_dir.mkdir(parents=True, exist_ok=False)
    asset_manifest = prepare_assets(config, run_dir)
    write_json(run_dir / "asset-manifest.json", asset_manifest)
    preflight = TRAINED.preflight(config, policy)
    if scenario_name == SERIAL_REUSE_SCENARIO:
        preflight.setdefault("checks", {})["serial_reuse_asset_exists"] = (
            run_dir / "fixtures/serial-reuse.html"
        ).is_file()
        preflight["status"] = (
            "READY" if all(preflight["checks"].values()) else "BLOCKED"
        )  # lzx-note
    if scenario_name in APP_NATIVE_SCENARIOS - {
        "r1_app_cold_retire", SERIAL_REUSE_SCENARIO,
    }:
        preflight.setdefault("checks", {})["visual_stability_capture_exists"] = all(
            shutil.which(tool) is not None for tool in ("import", "convert")
        )
        preflight["status"] = (
            "READY" if all(preflight["checks"].values()) else "BLOCKED"
        )  # lzx-note
    if scenario_name in SUBSTITUTION_SCENARIOS:
        preflight.setdefault("checks", {})["substitution_fixture_exists"] = (
            SUBSTITUTION_FIXTURE.is_file()
        )
        preflight["checks"]["substitution_launcher_exists"] = (
            SUBSTITUTION_LAUNCHER.is_file()
        )
        preflight["status"] = (
            "READY" if all(preflight["checks"].values()) else "BLOCKED"
        )
    write_json(run_dir / "preflight.json", preflight)
    if preflight["status"] != "READY":
        return {"status": "BLOCKED", "valid": False, "preflight": preflight}

    variant = {
        "parp_off": "native", "bin_lstm": "bin_apply",
        "bin_cold_lstm": "bin_cold_apply",
        "bin_workload_lstm": "bin_workload_apply",  # lzx-note
    }.get(policy)
    original_policy: dict[str, Any] | None = None
    cgroup: Path | None = None
    automation: subprocess.Popen[Any] | None = None
    automation_rc = 1
    abort_reason = ""
    trace_instance = f"parp-accept-real-pc-{os.getpid()}-{seed}"
    trace_stream: subprocess.Popen[Any] | None = None
    trace_output: Any = None
    trace_error: Any = None
    policy_before: dict[str, Any] = {}
    policy_after: dict[str, Any] = {}
    action_plan: dict[str, Any] = {}
    scenario: dict[str, Any] = {}
    monitor: list[dict[str, Any]] = []
    dirty_sysctl_original: dict[str, int] = {}
    writeback_gate_state: dict[str, int] = {}
    total = int(ACCEPT.meminfo()["MemTotal"])
    setup = {
        "slice": config["slice"],
        "safety": {"memory_high_ratio": 0.90, "memory_max_ratio": 0.95},
        "peak": {"oom_threshold": {
            "enabled": True,
            "memory_high": "infinity",
            "memory_max_bytes": int(total * 0.95),
            "memory_swap_max_bytes": int(config.get("memory_swap_max_mib", 1024)) * MIB,
        }},
    }
    try:
        if scenario_name in {
            "cold_dirty_reclaim", "workload_matrix_reclaim",
            *SUBSTITUTION_SCENARIOS, *APP_NATIVE_DIRTY_SCENARIOS,
        }:
            dirty_sysctl_original = apply_dirty_writeback_control(config)
        if scenario_name in {"cold_writeback_gate_hot_reuse", "r5_app_writeback_gate"}:
            writeback_gate_state = apply_reclaim_writeback_gate(config)
        if variant:
            original_policy = ACCEPT.apply_global_policy(variant)
        cgroup = ACCEPT.setup_slice(setup, variant or "native")
        if scenario_name in {
            "cold_dirty_reclaim", "workload_matrix_reclaim",
            *SUBSTITUTION_SCENARIOS, *APP_NATIVE_DIRTY_SCENARIOS,
        }:
            # setup_slice() exposes io.stat on the experiment slice itself.
            # Enable +io once more on that slice so its per-application scopes
            # expose writeback accounting as well. lzx-note
            ACCEPT.enable_controller_chain(cgroup, cgroup, "io")
        policy_before = ACCEPT.policy_state(cgroup)
        write_json(run_dir / "policy-before.json", policy_before)
        scenario = generate_scenario(
            config, scenario_name, run_dir, cgroup, seed,
            require_myfs=policy != "native_kernel",
            minimum_myfs_abi=3 if policy == "bin_workload_lstm" else 2,
            trace_instance=trace_instance,
            require_workload_profiles=policy == "bin_workload_lstm",
        )
        write_json(run_dir / "scenario.json", scenario)
        action_plan = action_plan_payload(
            config, scenario_name, seed, scenario, asset_manifest,
        )
        write_json(run_dir / "action-plan.json", action_plan)
        if expected_plan is not None and action_plan.get("sha256") != expected_plan.get("sha256"):
            return {
                "status": "BLOCKED", "valid": False,
                "invalid_reasons": ["action-plan hash differs from replay baseline"],
                "scenario": scenario_name, "policy": policy, "seed": seed,
                "expected_action_plan_sha256": expected_plan.get("sha256"),
                "observed_action_plan_sha256": action_plan.get("sha256"),
                "run_dir": str(run_dir),
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
            "--trace-output", str(run_dir / "automation-trace.csv"),
            "--session-id", run_dir.name, "--scenario-id", f"real_{scenario_name}",
            "--test-slice", str(config["slice"]),
            "--screenshot-output-dir", str(run_dir / "screenshots"),
        ]
        log = (run_dir / "automation.log").open("w", encoding="utf-8")
        automation = subprocess.Popen(
            command, stdout=log, stderr=subprocess.STDOUT, text=True,
            env=env, start_new_session=True,
        )
        initial_oom = ACCEPT.vmstat().get("oom_kill", 0)
        low_count = psi_count = 0
        started = time.monotonic()
        safety = config["safety"]
        while automation.poll() is None:
            sample = ACCEPT.snapshot(cgroup)
            monitor.append(sample)
            low = sample["memavailable"] < int(safety["min_memavailable_mib"]) * MIB
            low_count = low_count + 1 if low else 0
            high_psi = float(sample["psi"].get("full_avg10", 0)) > float(safety["psi_full_avg10_abort"])
            psi_count = psi_count + 1 if high_psi and low else 0
            if sample["vmstat"].get("oom_kill", 0) > initial_oom:
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
        log.close()
        policy_after = ACCEPT.policy_state(cgroup)
        write_json(run_dir / "policy-after.json", policy_after)
    except Exception as exc:
        abort_reason = abort_reason or f"HARNESS_ERROR:{type(exc).__name__}:{exc}"
    finally:
        try:
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
        finally:
            try:
                if original_policy is not None:
                    ACCEPT.restore_global_policy(original_policy)
            finally:
                try:
                    # Both global controls must be restored even if cgroup,
                    # trace, or policy cleanup itself fails. lzx-note
                    restore_reclaim_writeback_gate(writeback_gate_state)
                finally:
                    restore_dirty_writeback_control(dirty_sysctl_original)
    write_json(run_dir / "monitor.json", monitor)

    reasons: list[str] = []
    artifacts: dict[str, Any] = {}
    for filename in ("prediction-gate.json", "pressure-holding-state.json", "reclaim-source.json"):
        try:
            artifacts[filename] = read_json(run_dir / filename)
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            artifacts[filename] = {}
            reasons.append(f"{filename} missing")
    app_native_gate: dict[str, Any] = {}
    if scenario_name in APP_NATIVE_SCENARIOS:
        try:
            app_native_gate = read_json(run_dir / "app-native-gate.json")
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            reasons.append("app-native-gate.json missing")
        if not app_native_gate.get("valid"):
            reasons.append("application UI working-set gate invalid")
    writeback_gate_evidence: dict[str, Any] = {}
    if scenario_name in {"cold_writeback_gate_hot_reuse", "r5_app_writeback_gate"}:
        try:
            writeback_gate_evidence = read_json(run_dir / "writeback-gate-evidence.json")
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            reasons.append("writeback-gate-evidence.json missing")
        if not writeback_gate_evidence.get("valid"):
            reasons.append("may_writepage=0 precondition was not active before pressure")
    if not artifacts["prediction-gate.json"].get("valid"):
        reasons.extend(artifacts["prediction-gate.json"].get("reasons", ["prediction gate invalid"]))
    workload_profile_gate: dict[str, Any] = {}
    if policy == "bin_workload_lstm" and scenario_name in APP_NATIVE_DIRTY_SCENARIOS:
        try:
            workload_profile_gate = read_json(run_dir / "workload-profile-gate.json")
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            reasons.append("workload-profile-gate.json missing")
        if not workload_profile_gate.get("valid"):
            reasons.extend(
                workload_profile_gate.get(
                    "reasons", ["application workload profile gate invalid"]
                )
            )
    if artifacts["pressure-holding-state.json"].get("status") != "HOLDING":
        reasons.append(f"pressure status={artifacts['pressure-holding-state.json'].get('status')}")
    if not artifacts["reclaim-source.json"].get("valid"):
        reasons.append("real application cgroup comparison invalid")
    if automation_rc != 0:
        reasons.append(f"automation returned {automation_rc}")
    if abort_reason:
        reasons.append(abort_reason)

    reuse: dict[str, Any] = {}
    try:
        reuse = comparison_payload(
            read_json(run_dir / "snapshot-under-pressure.json"),
            read_json(run_dir / "snapshot-after-reuse.json"),
            set(config["hot_apps"]),
        )
    except (FileNotFoundError, OSError, KeyError, TypeError):
        if scenario_name not in {"cold_retire", "r1_app_cold_retire"}:
            reasons.append("post-reuse snapshot invalid")
    bin_delta = policy_stat_delta(
        str(policy_before.get("reclaim_bin_stats", "")),
        str(policy_after.get("reclaim_bin_stats", "")),
    )
    cold_delta = policy_stat_delta(
        str(policy_before.get("reclaim_cold_stats", "")),
        str(policy_after.get("reclaim_cold_stats", "")),
    )
    if policy == "bin_lstm":
        if int(bin_delta.get("policy_hits", 0)) <= 0:
            reasons.append("bin policy_hits did not increase")
        if int(bin_delta.get("subtree_selected", 0)) <= 0:
            reasons.append("bin subtree_selected did not increase")
    if policy in {"bin_cold_lstm", "bin_workload_lstm"}:
        if int(bin_delta.get("policy_hits", 0)) <= 0:
            reasons.append("bin policy_hits did not increase")
        if int(cold_delta.get("passes", 0)) <= 0:
            reasons.append("prediction-cold pressure did not execute")
        if int(cold_delta.get("scanned", 0)) <= 0:
            reasons.append("prediction-cold pressure scanned no pages")
    if policy == "bin_workload_lstm":
        if int(cold_delta.get("workload_profile_hits", 0)) <= 0:
            reasons.append("workload-aware reclaim had no valid cgroup profile")
        miss_budget = int(config.get("workload_profile_miss_budget", 0))
        if int(cold_delta.get("workload_profile_misses", 0)) > miss_budget:
            reasons.append(
                "workload-aware reclaim exceeded safe profile fallback budget"
            )
        # The scenario deliberately contains all four workload classes.  A
        # profile hit alone is insufficient evidence: require each intended
        # page-type policy to have reached a prediction-cold reclaim pass.
        # lzx-note
        class_counters = {
            "ANON_HEAVY": "anon_heavy_passes",
            "FILE_CLEAN": "file_clean_passes",
            "FILE_DIRTY": "file_dirty_passes",
            "MIXED": "mixed_passes",
        }
        expected_classes = {
            str(value) for value in config.get("workload_expected_classes", {}).values()
        }
        for workload_class in sorted(expected_classes):
            counter = class_counters.get(workload_class)
            if counter and int(cold_delta.get(counter, 0)) <= 0:
                reasons.append(
                    f"workload-aware reclaim did not execute {workload_class}"
                )
        class_passes = {
            workload_class: int(cold_delta.get(counter, 0))
            for workload_class, counter in class_counters.items()
        }
        unexpected_passes = (
            0 if "ANY" in expected_classes else sum(
                count for workload_class, count in class_passes.items()
                if workload_class not in expected_classes
            )
        )
        if unexpected_passes > int(config.get("unexpected_workload_pass_budget", 0)):
            reasons.append("workload-aware reclaim exceeded unexpected class pass budget")
        if (
            scenario_name in {"cold_writeback_gate_hot_reuse", "r5_app_writeback_gate"}
            and bool(config.get("require_writepage_promotion", True))
            and int(cold_delta.get("writepage_promotions", 0)) <= 0
        ):
            reasons.append(
                "cold-aggressive did not promote may_writepage under the writeback gate"
            )
    exact_reuse: dict[str, Any] = {}
    if scenario_name == "page_reuse_cold_only":
        exact_reuse = exact_reuse_payload(config, run_dir)
        write_json(run_dir / "exact-reuse-result.json", exact_reuse)
        if not exact_reuse.get("valid"):
            reasons.append("exact page reuse evidence invalid")
    cold_dirty: dict[str, Any] = {}
    if scenario_name == "cold_dirty_reclaim":
        cold_dirty = cold_dirty_payload(config, run_dir)
        write_json(run_dir / "cold-dirty-result.json", cold_dirty)
        if not cold_dirty.get("valid"):
            reasons.append("cold dirty residency evidence invalid")
    workload_matrix: dict[str, Any] = {}
    if scenario_name == "workload_matrix_reclaim":
        workload_matrix = workload_matrix_payload(config, run_dir)
        write_json(run_dir / "workload-matrix-result.json", workload_matrix)
        if not workload_matrix.get("valid"):
            reasons.append("workload matrix residency evidence invalid")
    substitution_result: dict[str, Any] = {}
    if scenario_name in SUBSTITUTION_SCENARIOS:
        substitution_result = substitution_payload(config, run_dir)
        result_filename = (
            "cold-writeback-gate-hot-reuse-result.json"
            if scenario_name == "cold_writeback_gate_hot_reuse"
            else "cold-dirty-preserve-hot-clean-result.json"
        )
        write_json(run_dir / result_filename, substitution_result)
        if not substitution_result.get("valid"):
            reasons.append("cold dirty substitution or hot clean reuse evidence invalid")
    serial_major_reuse: dict[str, Any] = {}
    if scenario_name == SERIAL_REUSE_SCENARIO:
        serial_major_reuse = serial_major_reuse_payload(
            config, policy, run_dir / "automation-trace.csv", reuse,
        )
        write_json(run_dir / "serial-major-reuse-result.json", serial_major_reuse)
        if not serial_major_reuse.get("valid"):
            reasons.append("serial major-fault reuse evidence invalid")
    fairness_misprediction: dict[str, Any] = {}
    if scenario_name == FAIRNESS_SCENARIO:
        fairness_misprediction = fairness_misprediction_payload(
            config, policy, run_dir / "automation-trace.csv", scenario,
            artifacts["reclaim-source.json"], reuse,
            artifacts["prediction-gate.json"],
        )
        write_json(
            run_dir / "fairness-misprediction-result.json",
            fairness_misprediction,
        )
        if not fairness_misprediction.get("valid"):
            reasons.append("fairness or designed-misprediction evidence invalid")
    result = {
        "status": "VALID" if not reasons else "INVALID",
        "valid": not reasons,
        "invalid_reasons": list(dict.fromkeys(reasons)),
        "scenario": scenario_name, "policy": policy, "seed": seed,
        "kernel_release": os.uname().release, "run_dir": str(run_dir),
        "action_plan_sha256": action_plan.get("sha256"),
        "replay_expected_sha256": expected_plan.get("sha256") if expected_plan else None,
        "replayed_from_baseline": expected_plan is not None,
        "workload_kind": (
            "real_gui_application_native_working_sets"
            if scenario_name in APP_NATIVE_SCENARIOS
            else
            "real_gui_plus_controlled_exact_page_reuse"
            if scenario_name == "page_reuse_cold_only"
            else "real_gui_plus_controlled_cold_dirty_file"
            if scenario_name == "cold_dirty_reclaim"
            else "real_gui_plus_clean_dirty_substitution_and_hot_clean_reuse"
            if scenario_name in SUBSTITUTION_SCENARIOS
            else "real_gui_plus_controlled_workload_matrix"
            if scenario_name == "workload_matrix_reclaim"
            else "real_gui_applications"
        ),
        "synthetic_app_working_set": scenario_name in {
            "page_reuse_cold_only", "cold_dirty_reclaim", "workload_matrix_reclaim",
            *SUBSTITUTION_SCENARIOS,
        },
        "prediction_gate": artifacts["prediction-gate.json"],
        "workload_profile_gate": workload_profile_gate,
        "app_native_gate": app_native_gate,
        "pressure": artifacts["pressure-holding-state.json"],
        "reclaim": artifacts["reclaim-source.json"],
        "post_reuse": reuse,
        "interaction_latency": parse_action_latencies(
            run_dir / "automation-trace.csv", scenario,
        ),
        "serial_major_reuse": serial_major_reuse,
        "fairness_misprediction": fairness_misprediction,
        "reclaim_bin_delta": bin_delta,
        "reclaim_cold_delta": cold_delta,
        "exact_reuse": exact_reuse,
        "cold_dirty": cold_dirty,
        "cold_dirty_preserve_hot_clean": substitution_result,
        "workload_matrix": workload_matrix,  # lzx-note
        "writeback_gate": {
            "setup": writeback_gate_state,
            "before_pressure": writeback_gate_evidence,
            "restored_laptop_mode": read_int(LAPTOP_MODE_SYSCTL),
        } if scenario_name in {
            "cold_writeback_gate_hot_reuse", "r5_app_writeback_gate",
        } else {},  # lzx-note
        "dirty_writeback_control": {
            "original": dirty_sysctl_original,
            "restored": {
                key: int(path.read_text(encoding="utf-8").strip())
                for key, path in DIRTY_SYSCTLS.items()
            } if dirty_sysctl_original else {},
        },  # lzx-note
        "monitor": {
            "samples": len(monitor),
            "min_memavailable_bytes": min((item["memavailable"] for item in monitor), default=None),
            "max_psi_full_avg10": max((float(item["psi"].get("full_avg10", 0)) for item in monitor), default=None),
        },
    }
    write_json(run_dir / "run-result.json", result)
    return result


def command_run(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    if "r8_multi_app_oom_survival" in config.get("scenarios", []):
        return R8.command_run(args)
    scenarios = config["scenarios"] if args.scenario == "all" else [args.scenario]
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
                f"real-PC policy={args.policy} scenario={scenario_name} "
                f"round={round_index}/{args.rounds} seed={seed}", flush=True,
            )
            expected_plan: dict[str, Any] | None = None
            if args.replay_from is not None:
                expected_path = (
                    args.replay_from
                    / f"round-{round_index:02d}-{scenario_name}"
                    / "action-plan.json"
                )
                try:
                    expected_plan = read_json(expected_path)
                except (FileNotFoundError, OSError, json.JSONDecodeError) as exc:
                    result = {
                        "status": "BLOCKED", "valid": False,
                        "invalid_reasons": [f"replay plan unavailable: {type(exc).__name__}:{exc}"],
                        "scenario": scenario_name, "policy": args.policy, "seed": seed,
                        "run_dir": str(run_dir), "expected_plan": str(expected_path),
                    }
                    results.append(result)
                    print(f"status={result['status']} output={run_dir}", flush=True)
                    break
                if (
                    str(expected_plan.get("scenario")) != scenario_name
                    or int(expected_plan.get("seed", -1)) != seed
                ):
                    result = {
                        "status": "BLOCKED", "valid": False,
                        "invalid_reasons": ["replay scenario/seed differs from requested run"],
                        "scenario": scenario_name, "policy": args.policy, "seed": seed,
                        "run_dir": str(run_dir), "expected_plan": str(expected_path),
                    }
                    results.append(result)
                    print(f"status={result['status']} output={run_dir}", flush=True)
                    break
            result = run_one(
                config, scenario_name, args.policy, seed, run_dir,
                expected_plan=expected_plan,
            )
            results.append(result)
            print(f"status={result['status']} output={run_dir}", flush=True)
            if not result.get("valid") and not args.keep_going:
                break
        if results and not results[-1].get("valid") and not args.keep_going:
            break
    summary = {
        "schema_version": 1,
        "status": "COMPLETE" if all(item.get("valid") for item in results) else "INCOMPLETE",
        "policy": args.policy, "kernel_release": os.uname().release,
        "rounds_requested": args.rounds, "scenarios_requested": scenarios,
        "replay_from": str(args.replay_from) if args.replay_from else None,
        "workload_kind": "real_gui_applications", "runs": results,
    }
    write_json(session / "summary.json", summary)
    print(session)
    return 0 if summary["status"] == "COMPLETE" else 1


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    sub = root.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("--config", type=Path, default=CONFIG_DEFAULT)
    run.add_argument("--policy", choices=("native_kernel", "parp_off", "bin_lstm", "bin_cold_lstm", "bin_workload_lstm"), required=True)
    run.add_argument(
        "--scenario",
        choices=(
            "all", "cold_retire", "predicted_return", "mixed_multitask",
            "page_reuse_cold_only", "cold_dirty_reclaim", "workload_matrix_reclaim",
            "cold_dirty_preserve_hot_clean", "cold_writeback_gate_hot_reuse",
            "r1_app_cold_retire", "r2_app_predicted_return",
            "r3_app_source_distribution", "r4_app_dirty_substitution",
            "r5_app_writeback_gate", "r6_app_serial_major_reuse",
            "r7_app_fairness_misprediction",
            "r8_multi_app_oom_survival",
        ),
        default="all",
    )
    run.add_argument("--rounds", type=int, default=1)
    run.add_argument("--seed", type=int, default=20260828)
    run.add_argument("--keep-going", action="store_true")
    run.add_argument(
        "--replay-from", type=Path,
        help="Native session root whose per-round action-plan.json must match",
    )
    run.set_defaults(func=command_run)

    calibrate_r8 = sub.add_parser("calibrate-r8")
    calibrate_r8.add_argument("--config", type=Path, required=True)
    calibrate_r8.add_argument("--policy", choices=("native_kernel",), required=True)
    calibrate_r8.add_argument("--output", type=Path, required=True)
    calibrate_r8.add_argument("--baseline-from", type=Path)
    calibrate_r8.set_defaults(func=R8.command_calibrate)

    report_r8 = sub.add_parser("report-r8")
    report_r8.add_argument("--native", type=Path, required=True)
    report_r8.add_argument("--bin", type=Path, required=True)
    report_r8.add_argument("--output", type=Path, required=True)
    report_r8.set_defaults(func=R8.command_report)

    oom_exec = sub.add_parser("oom-score-exec")
    oom_exec.add_argument("--score", type=int, required=True)
    oom_exec.add_argument("command", nargs=argparse.REMAINDER)
    oom_exec.set_defaults(func=R8.command_oom_score_exec)

    r8_snapshot = sub.add_parser("r8-snapshot")
    r8_snapshot.add_argument("--cgroup", type=Path, required=True)
    r8_snapshot.add_argument("--apps", required=True)
    r8_snapshot.add_argument("--label", required=True)
    r8_snapshot.add_argument("--output", type=Path, required=True)
    r8_snapshot.set_defaults(func=R8.command_snapshot)

    r8_gate = sub.add_parser("r8-workset-gate")
    r8_gate.add_argument("--config", type=Path, required=True)
    r8_gate.add_argument("--cgroup", type=Path, required=True)
    r8_gate.add_argument("--apps", required=True)
    r8_gate.add_argument("--label", required=True)
    r8_gate.add_argument("--output", type=Path, required=True)
    r8_gate.add_argument("--gate-output", type=Path, required=True)
    r8_gate.set_defaults(func=R8.command_workset_gate)

    r8_scores = sub.add_parser("r8-enforce-oom-scores")
    r8_scores.add_argument("--config", type=Path, required=True)
    r8_scores.add_argument("--cgroup", type=Path, required=True)
    r8_scores.add_argument("--apps", required=True)
    r8_scores.add_argument("--output", type=Path, required=True)
    r8_scores.set_defaults(func=R8.command_enforce_oom_scores)

    r8_pressure = sub.add_parser("r8-pressure-record")
    r8_pressure.add_argument("--requested-mib", type=int, required=True)
    r8_pressure.add_argument("--committed-mib", type=int, required=True)
    r8_pressure.add_argument("--output", type=Path, required=True)
    r8_pressure.set_defaults(func=R8.command_pressure_record)

    snap = sub.add_parser("snapshot")
    snap.add_argument("--cgroup", type=Path, required=True)
    snap.add_argument("--apps", required=True)
    snap.add_argument("--label", required=True)
    snap.add_argument("--output", type=Path, required=True)
    snap.set_defaults(func=command_snapshot)

    compare = sub.add_parser("compare")
    compare.add_argument("--before", type=Path, required=True)
    compare.add_argument("--after", type=Path, required=True)
    compare.add_argument("--hot", required=True)
    compare.add_argument("--output", type=Path, required=True)
    compare.set_defaults(func=command_compare)

    boundary = sub.add_parser("boundary")
    boundary.add_argument("--slice", required=True)
    boundary.add_argument("--cgroup", type=Path, required=True)
    boundary.add_argument("--pressure-mib", type=int, required=True)
    boundary.add_argument("--reclaim-mib", type=int, required=True)
    boundary.add_argument("--swap-max-mib", type=int, required=True)
    boundary.add_argument("--output", type=Path, required=True)
    boundary.set_defaults(func=command_boundary)

    native_gate = sub.add_parser("app-native-gate")
    native_gate.add_argument("--cgroup", type=Path, required=True)
    native_gate.add_argument("--apps", required=True)
    native_gate.add_argument("--cold", required=True)
    native_gate.add_argument("--minimum-total-mib", type=int, required=True)
    native_gate.add_argument("--minimum-cold-dirty-mib", type=int, default=0)
    native_gate.add_argument("--minimum-cold-costly-mib", type=int, default=0)
    native_gate.add_argument("--require-dirty", action="store_true")
    native_gate.add_argument("--output", type=Path, required=True)
    native_gate.set_defaults(func=command_app_native_gate)

    native_reclaim = sub.add_parser("app-native-reclaim")
    native_reclaim.add_argument("--cgroup", type=Path, required=True)
    native_reclaim.add_argument("--target-mib", type=int, required=True)
    native_reclaim.add_argument("--minimum-achieved-ratio", type=float, required=True)
    native_reclaim.add_argument("--timeout", type=float, default=120.0)
    native_reclaim.add_argument("--settle-timeout", type=float, default=5.0)
    native_reclaim.add_argument(
        "--swappiness", choices=(*map(str, range(201)), "max"), default="",
    )
    native_reclaim.add_argument("--state", type=Path, required=True)
    native_reclaim.set_defaults(func=command_app_native_reclaim)

    writeback_gate = sub.add_parser("writeback-gate")
    writeback_gate.add_argument("--expected-laptop-mode", type=int, required=True)
    writeback_gate.add_argument("--output", type=Path, required=True)
    writeback_gate.set_defaults(func=command_writeback_gate)
    return root


def main() -> int:
    args = parser().parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
