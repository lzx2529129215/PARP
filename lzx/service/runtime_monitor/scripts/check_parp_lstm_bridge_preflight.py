#!/usr/bin/env python3
"""Read-only preflight for the Test2 LSTM-to-PARP prediction sink."""

from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
from pathlib import Path


def exists(path: Path) -> bool:
    try:
        return path.exists()
    except OSError:
        return False


def writable(path: Path) -> bool:
    try:
        return path.is_file() and os.access(path, os.W_OK)
    except OSError:
        return False


def command_text(command: list[str]) -> str:
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=3, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return result.stdout.strip()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parp-debugfs-root", default="/sys/kernel/debug/parp")
    parser.add_argument("--app-bind-config", default="")
    parser.add_argument("--model-version", type=int, default=401)
    parser.add_argument("--schema-version", default="parp-app-prior-v1")
    args = parser.parse_args()

    root = Path(args.parp_debugfs_root).expanduser()
    bind = root / "app_bind"
    prior = root / "app_prior"
    batch = root / "app_prior_batch"
    stats = root / "stats"
    snapshot = root / "snapshot"
    trace = Path("/sys/kernel/tracing/events/parp")
    result = {
        "status": "READY",
        "kernel_release": platform.release(),
        "cmdline": Path("/proc/cmdline").read_text(encoding="utf-8").strip() if Path("/proc/cmdline").exists() else "",
        "debugfs_root": str(root),
        "debugfs_root_exists": exists(root),
        "app_bind": {"path": str(bind), "exists": exists(bind), "writable": writable(bind)},
        "app_prior": {"path": str(prior), "exists": exists(prior), "writable": writable(prior)},
        "app_prior_batch": {"path": str(batch), "exists": exists(batch), "writable": writable(batch), "supported_by_current_patch": False},
        "stats": {"path": str(stats), "exists": exists(stats)},
        "snapshot": {"path": str(snapshot), "exists": exists(snapshot)},
        "update_trace_directory": {"path": str(trace), "exists": exists(trace)},
        "app_bind_config": {"path": str(Path(args.app_bind_config).expanduser()) if args.app_bind_config else "", "provided": bool(args.app_bind_config), "exists": exists(Path(args.app_bind_config).expanduser()) if args.app_bind_config else False},
        "model_version": args.model_version,
        "schema_version": args.schema_version,
        "debugfs_mount": "debugfs on /sys/kernel/debug" in command_text(["mount"]),
        "tracefs_mount": "tracefs on /sys/kernel/tracing" in command_text(["mount"]),
    }
    if not (result["debugfs_root_exists"] and result["app_bind"]["exists"] and result["app_prior"]["exists"]):
        result["status"] = "KERNEL_INTERFACE_UNAVAILABLE"
    elif not (result["app_bind"]["writable"] and result["app_prior"]["writable"]):
        result["status"] = "FAIL_CLOSED"

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "READY" else 2


if __name__ == "__main__":
    raise SystemExit(main())

