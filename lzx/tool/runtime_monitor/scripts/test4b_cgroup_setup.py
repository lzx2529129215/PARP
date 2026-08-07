#!/usr/bin/env python3
"""Plan/create/tear down the sole Test4B finite cgroup tree safely."""
from __future__ import annotations

import argparse
import csv
import glob
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
MIB = 1024 * 1024
SLICE = "test4b-experiment.slice"


def run(*command: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, check=False, capture_output=True, text=True, timeout=10)


def meminfo() -> dict[str, int]:
    data: dict[str, int] = {}
    for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0].endswith(":"):
            data[parts[0][:-1]] = int(parts[1]) * 1024
    return data


def historic_baseline() -> tuple[int, list[dict[str, Any]]]:
    peaks: list[dict[str, Any]] = []
    pattern = str(ROOT / "outputs/runtime_monitor/test4_probability_activity_reclaim_shadow_*_r*/activity/app_memory_activity.csv")
    for name in sorted(glob.glob(pattern)):
        by_time: dict[str, dict[str, int]] = {}
        try:
            with Path(name).open(encoding="utf-8", newline="") as stream:
                for row in csv.DictReader(stream):
                    app = row.get("app_key", "")
                    if app not in {"FIREFOX", "THUNDERBIRD", "TELEGRAM"} or not row.get("memcg_path"):
                        continue
                    by_time.setdefault(row.get("timestamp_ns", ""), {})[app] = int(row.get("memcg_current_bytes") or 0)
        except (OSError, ValueError):
            continue
        values = [sum(sample.values()) for sample in by_time.values() if len(sample) == 3]
        if values:
            peaks.append({"source": name, "peak_concurrent_bytes": max(values), "sample_count": len(values)})
    return max((int(item["peak_concurrent_bytes"]) for item in peaks), default=0), peaks


def plan(session_dir: Path, ballast_config: Path) -> dict[str, Any]:
    config = json.loads(ballast_config.read_text(encoding="utf-8"))
    total_ballast = int(config.get("max_global_bytes", 0))
    baseline, sources = historic_baseline()
    memory = meminfo(); available = int(memory.get("MemAvailable", 0)); total = int(memory.get("MemTotal", 0))
    margin = max(64 * MIB, baseline // 40)  # >=64MiB, plus a measured-app burst allowance
    required = baseline + total_ballast + margin
    reserve = max(512 * MIB, total // 8)
    global_cap = total * 3 // 5
    expected_available = available - required
    checks = {
        "kernel_memory_controller": "memory" in (Path("/sys/fs/cgroup/cgroup.controllers").read_text().split() if Path("/sys/fs/cgroup/cgroup.controllers").exists() else []),
        "baseline_evidence_present": baseline > 0,
        "finite_max_positive": required > 0,
        "experiment_max_within_60pct_memtotal": required <= global_cap,
        "expected_memavailable_retains_hard_floor": expected_available >= reserve,
        "user_systemd_available": run("systemctl", "--user", "is-system-running").returncode in {0, 1},
        "cgroup_v2": Path("/sys/fs/cgroup/cgroup.controllers").is_file(),
    }
    ready = all(checks.values())
    result = {
        "status": "READY" if ready else "BLOCKED", "slice": SLICE, "session_dir": str(session_dir),
        "formula": "historical_concurrent_baseline + total_ballast_budget + burst_margin",
        "baseline_all_test_apps_bytes": baseline, "total_ballast_budget_bytes": total_ballast,
        "experiment_burst_margin_bytes": margin, "experiment_max_bytes": required,
        "memavailable_before_bytes": available, "expected_memavailable_after_budget_bytes": expected_available,
        "system_reserve_bytes": reserve, "global_cap_bytes": global_cap, "memtotal_bytes": total,
        "historical_sources": sources, "checks": checks,
        "swap": Path("/proc/swaps").read_text(encoding="utf-8"),
        "memory_reclaim_abi": "numeric byte request only; matching v4.1 source Documentation/admin-guide/cgroup-v2.rst confirms optional nested swappiness, which Test4B deliberately does not use",
    }
    (session_dir / "cgroup_preflight.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def cgroup_path() -> Path | None:
    value = run("systemctl", "--user", "show", SLICE, "-p", "ControlGroup", "--value")
    return Path("/sys/fs/cgroup") / value.stdout.strip().lstrip("/") if value.returncode == 0 and value.stdout.strip() else None


def create(plan_data: dict[str, Any], session_dir: Path) -> dict[str, Any]:
    if plan_data.get("status") != "READY":
        return {"status": "BLOCKED", "reason": "PREFLIGHT_NOT_READY"}
    path = cgroup_path()
    if path is not None and path.is_dir() and (path / "cgroup.procs").read_text().strip():
        return {"status": "BLOCKED", "reason": "TEST4B_SLICE_ALREADY_HAS_PROCESSES", "path": str(path)}
    keeper = f"test4b-keeper-{session_dir.name[-24:]}.service".replace("_", "-")
    created = run("systemd-run", "--user", f"--unit={keeper}", "--collect", f"--slice={SLICE}", "/bin/sleep", "900")
    if created.returncode != 0:
        return {"status": "BLOCKED", "reason": "KEEPER_CREATE_FAILED", "detail": created.stderr.strip()}
    changed = run("systemctl", "--user", "set-property", "--runtime", SLICE, f"MemoryMax={int(plan_data['experiment_max_bytes'])}")
    path = cgroup_path()
    if changed.returncode != 0 or path is None or not path.is_dir():
        run("systemctl", "--user", "stop", keeper)
        return {"status": "BLOCKED", "reason": "FINITE_MEMORY_MAX_SET_FAILED", "detail": changed.stderr.strip(), "path": str(path or "")}
    try:
        actual = (path / "memory.max").read_text().strip()
        reclaim_exists = (path / "memory.reclaim").is_file()
        swap_max = (path / "memory.swap.max").read_text().strip()
    except OSError as exc:
        run("systemctl", "--user", "stop", keeper)
        return {"status": "BLOCKED", "reason": "CGROUP_INTERFACE_READ_FAILED", "detail": str(exc)}
    if actual != str(plan_data["experiment_max_bytes"]) or not reclaim_exists:
        run("systemctl", "--user", "stop", keeper)
        return {"status": "BLOCKED", "reason": "CGROUP_INTERFACE_VERIFY_FAILED", "memory_max": actual, "reclaim_exists": reclaim_exists}
    data = {"status": "READY", "slice": SLICE, "keeper_unit": keeper, "path": str(path), "memory_max": actual,
            "memory_reclaim_exists": reclaim_exists, "memory_swap_max": swap_max, "created_at_ns": time.time_ns()}
    (session_dir / "cgroup_created.json").write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return data


def cleanup(session_dir: Path, ballast_config: Path) -> dict[str, Any]:
    created_path = session_dir / "cgroup_created.json"
    data = json.loads(created_path.read_text(encoding="utf-8")) if created_path.exists() else {}
    keeper = str(data.get("keeper_unit", ""))
    if keeper.startswith("test4b-keeper-"):
        run("systemctl", "--user", "stop", keeper)
    run("systemctl", "--user", "revert", SLICE)
    run("systemctl", "--user", "stop", SLICE)
    path = cgroup_path(); present = bool(path and path.exists())
    removed_files: list[str] = []
    removed_socket_dir = False
    try:
        config = json.loads(ballast_config.read_text(encoding="utf-8"))
        allowed_files = (session_dir / "ballast_files").resolve()
        socket_parent = Path(f"/run/user/{os.getuid()}").resolve()
        socket_dirs: set[Path] = set()
        cleanup_entries = list(dict(config.get("apps", {})).values())
        cleanup_entries.extend(item for item in list(config.get("cleanup_paths", [])) if isinstance(item, dict))
        for item in cleanup_entries:
            file_path = Path(str(item.get("file_path", ""))).resolve()
            if file_path.is_file() and file_path.is_relative_to(allowed_files):
                file_path.unlink()
                removed_files.append(str(file_path))
            socket_path = Path(str(item.get("socket_path", ""))).resolve()
            if socket_path.parent.is_relative_to(socket_parent) and socket_path.parent.name.startswith("t4b-"):
                socket_dirs.add(socket_path.parent)
        for directory in socket_dirs:
            try:
                directory.rmdir()
                removed_socket_dir = True
            except OSError:
                pass
    except (OSError, json.JSONDecodeError):
        pass
    result = {"slice": SLICE, "keeper_unit": keeper, "cgroup_present_after_cleanup": present, "path": str(path or ""), "timestamp_ns": time.time_ns(), "removed_ballast_files": removed_files, "removed_socket_dir": removed_socket_dir}
    (session_dir / "cgroup_cleanup.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--session-dir", type=Path, required=True)
    parser.add_argument("--ballast-config", type=Path, required=True)
    parser.add_argument("--create", action="store_true")
    parser.add_argument("--cleanup", action="store_true")
    args = parser.parse_args(); session = args.session_dir.resolve(); session.mkdir(parents=True, exist_ok=True)
    if args.cleanup:
        print(json.dumps(cleanup(session, args.ballast_config), ensure_ascii=False)); return
    result = plan(session, args.ballast_config)
    if args.create and result["status"] == "READY": result = create(result, session)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
