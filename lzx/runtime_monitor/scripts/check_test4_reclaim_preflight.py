#!/usr/bin/env python3
"""Read-only safety preflight for Test4 bounded cgroup reclaim.

The script intentionally never creates a scope, changes a memory controller,
or writes debugfs.  A READY result is necessary but not sufficient for APPLY:
the monitor still checks foreground, binding, activity, pressure, cooldown and
budget immediately before each write.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]


def _run(*command: str) -> tuple[int, str, str]:
    try:
        result = subprocess.run(command, check=False, capture_output=True, text=True, timeout=4)
        return result.returncode, result.stdout.strip(), result.stderr.strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, "", str(exc)


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


def _is_dir(path: Path) -> bool:
    try:
        return path.is_dir()
    except OSError:
        return False


def _is_file(path: Path) -> bool:
    try:
        return path.is_file()
    except OSError:
        return False


def _key_values(path: Path) -> dict[str, int]:
    values: dict[str, int] = {}
    for line in _read(path).splitlines():
        parts = line.split()
        if len(parts) == 2:
            try:
                values[parts[0]] = int(parts[1])
            except ValueError:
                pass
    return values


def _psi_line(kind: str) -> tuple[str, float]:
    line = next((item for item in _read(Path("/proc/pressure/memory")).splitlines() if item.startswith(kind + " ")), "")
    avg10 = 0.0
    for item in line.split():
        if item.startswith("avg10="):
            try:
                avg10 = float(item.split("=", 1)[1])
            except ValueError:
                pass
    return line, avg10


def _mem_available_bytes() -> int:
    for line in _read(Path("/proc/meminfo")).splitlines():
        if line.startswith("MemAvailable:"):
            try:
                return int(line.split()[1]) * 1024
            except (IndexError, ValueError):
                break
    return 0


def _slice_cgroup(slice_name: str) -> tuple[Path | None, str]:
    rc, output, error = _run("systemctl", "--user", "show", slice_name, "-p", "ControlGroup", "--value")
    if rc != 0 or not output:
        return None, error or "unable to resolve user slice"
    return Path("/sys/fs/cgroup") / output.lstrip("/"), ""


def _load_scope(path: Path) -> list[dict[str, Any]]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return [item for item in raw.get("apps", []) if isinstance(item, dict) and item.get("app_key")]


def _snapshot_bindings(path: Path) -> dict[str, Any]:
    text = _read(path)
    return {
        "path": str(path),
        "readable": bool(text),
        "contains_bindings_field": "binding" in text.lower(),
        "excerpt": text[:512],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--slice", default="huawei-test.slice")
    parser.add_argument("--mode", default="shadow", choices=["shadow", "apply-bounded"])
    parser.add_argument("--app-scope-config", type=Path, default=ROOT / "configs/runtime/test1_runtime_app_scope.json")
    parser.add_argument("--parp-debugfs-root", type=Path, default=Path("/sys/kernel/debug/parp"))
    parser.add_argument("--hard-min-available-bytes", type=int, default=512 * 1024 * 1024)
    parser.add_argument("--psi-full-abort-avg10", type=float, default=0.20)
    parser.add_argument("--min-free-disk-bytes", type=int, default=512 * 1024 * 1024)
    parser.add_argument("--output", default="")
    args = parser.parse_args()

    errors: list[str] = []
    warnings: list[str] = []
    slice_path, slice_error = _slice_cgroup(args.slice)
    if slice_path is None:
        errors.append("TEST_SLICE_UNRESOLVED")
        slice_path = Path("/sys/fs/cgroup") / args.slice
    elif not slice_path.is_dir():
        errors.append("TEST_CGROUP_MISSING")

    cgroup_v2 = _is_file(Path("/sys/fs/cgroup/cgroup.controllers"))
    controllers = _read(Path("/sys/fs/cgroup/cgroup.controllers")).split()
    if not cgroup_v2 or "memory" not in controllers:
        errors.append("CGROUP_V2_MEMORY_CONTROLLER_UNAVAILABLE")

    memory_max = _read(slice_path / "memory.max")
    memory_reclaim_exists = _is_file(slice_path / "memory.reclaim")
    if not memory_reclaim_exists:
        errors.append("MEMORY_RECLAIM_UNAVAILABLE")

    scope_checks: list[dict[str, Any]] = []
    for app in _load_scope(args.app_scope_config):
        scope_name = str(app.get("scope_name", ""))
        path = slice_path / scope_name
        exists = _is_dir(path)
        safe = exists and args.slice in str(path) and path not in {
            Path("/sys/fs/cgroup"), Path("/sys/fs/cgroup/user.slice"), Path("/sys/fs/cgroup/system.slice"),
        }
        item = {
            "app_key": app["app_key"], "scope_name": scope_name, "path": str(path),
            "exists": exists, "safe_test_cgroup": safe,
            "memory_reclaim_exists": _is_file(path / "memory.reclaim") if exists else False,
            "cgroup_procs_readable": bool(_read(path / "cgroup.procs")) if exists else False,
        }
        scope_checks.append(item)
        if args.mode == "apply-bounded" and (not exists or not safe or not item["memory_reclaim_exists"]):
            errors.append(f"TEST_APP_CGROUP_NOT_READY:{app['app_key']}")

    mem_available = _mem_available_bytes()
    psi_some, psi_some_avg10 = _psi_line("some")
    psi_full, psi_full_avg10 = _psi_line("full")
    root_events = _key_values(Path("/sys/fs/cgroup/memory.events"))
    if args.mode == "apply-bounded":
        if not memory_max or memory_max == "max":
            errors.append("FINITE_MEMORY_MAX_REQUIRED")
        if mem_available < args.hard_min_available_bytes:
            errors.append("MEM_AVAILABLE_BELOW_HARD_FLOOR")
        if psi_full_avg10 >= args.psi_full_abort_avg10:
            errors.append("MEMORY_PSI_FULL_ABORT_THRESHOLD")
        if root_events.get("oom", 0) or root_events.get("oom_kill", 0):
            errors.append("EXISTING_OOM_EVENT")

    swap_rc, swap_output, swap_error = _run("swapon", "--show", "--bytes", "--noheadings")
    if swap_rc != 0:
        warnings.append("SWAP_STATUS_UNAVAILABLE:" + swap_error)
    debugfs_root = args.parp_debugfs_root
    debugfs = {
        "root_exists": _is_dir(debugfs_root),
        "app_bind": _is_file(debugfs_root / "app_bind"),
        "app_prior": _is_file(debugfs_root / "app_prior"),
        "snapshot": _snapshot_bindings(debugfs_root / "snapshot"),
        "tracefs_mounted": _is_dir(Path("/sys/kernel/tracing")) or _is_dir(Path("/sys/kernel/debug/tracing")),
    }
    if args.mode == "apply-bounded" and not (debugfs["root_exists"] and debugfs["app_bind"] and debugfs["app_prior"]):
        errors.append("PARP_DEBUGFS_INTERFACE_UNAVAILABLE")

    disk_free = shutil.disk_usage(ROOT).free
    if disk_free < args.min_free_disk_bytes:
        errors.append("INSUFFICIENT_DISK_FOR_AUDIT")
    foreground_known = bool(os.environ.get("DISPLAY"))
    if not foreground_known:
        warnings.append("FOREGROUND_NOT_YET_OBSERVABLE_IN_PREFLIGHT")
        if args.mode == "apply-bounded":
            errors.append("FOREGROUND_NOT_OBSERVABLE")
    cleanup_script = ROOT / "automation/run_automation.sh"
    if not cleanup_script.is_file():
        errors.append("AUTOMATION_CLEANUP_SCRIPT_MISSING")

    status = "READY" if not errors else "BLOCKED"
    result = {
        "status": status,
        "mode": args.mode,
        "safety": "READ_ONLY_PREFLIGHT",
        "kernel": _read(Path("/proc/sys/kernel/osrelease")),
        "errors": errors,
        "warnings": warnings,
        "checks": {
            "cgroup_v2": {"mounted": cgroup_v2, "controllers": controllers, "slice_path": str(slice_path), "slice_resolution_error": slice_error},
            "memory_boundary": {"memory_max": memory_max, "slice_memory_reclaim_exists": memory_reclaim_exists},
            "testing_scopes": scope_checks,
            "app_bind": debugfs,
            "pressure": {"mem_available_bytes": mem_available, "psi_some": psi_some, "psi_some_avg10": psi_some_avg10, "psi_full": psi_full, "psi_full_avg10": psi_full_avg10, "root_memory_events": root_events, "swap": swap_output},
            "disk_free_bytes": disk_free,
            "foreground_observable": foreground_known,
            "cleanup_script": {"path": str(cleanup_script), "exists": cleanup_script.is_file()},
        },
    }
    text = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
    else:
        print(text, end="")
    return 0 if status == "READY" else 1


if __name__ == "__main__":
    raise SystemExit(main())
