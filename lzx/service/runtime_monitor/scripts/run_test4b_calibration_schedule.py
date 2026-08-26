#!/usr/bin/env python3
"""Foreground/AppBind/cgroup guarded 4 MiB calibration allocations."""
from __future__ import annotations

import argparse
import csv
import json
import socket
import subprocess
import time
from pathlib import Path
from typing import Any

MIB = 1024 * 1024
FIELDS = ["timestamp_ns", "app_key", "worker_id", "stage", "stage_index", "sample_point", "decision", "reason", "response", "requested_bytes", "global_allocated_bytes", "dynamic_budget_bytes", "parent_current", "parent_max", "anon", "file", "memory_swap_current", "mem_available", "system_psi_some", "system_psi_full", "system_psi_full_avg10", "parent_psi_some", "parent_psi_full", "parent_psi_full_avg10", "operation_latency_us"]


def command(path: str, value: str) -> tuple[bool, str]:
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as stream:
            stream.settimeout(2); stream.connect(path); stream.sendall((value + "\n").encode()); response = stream.recv(2048).decode().strip()
        return response.startswith("OK "), response
    except OSError as exc:
        return False, f"SOCKET_{type(exc).__name__}"


def fields(text: str) -> dict[str, str]:
    return {key: value for key, value in (item.split("=", 1) for item in text.split() if "=" in item)}


def psi(path: Path) -> tuple[str, str, float]:
    try: content = path.read_text(encoding="utf-8")
    except OSError: return "", "", 0.0
    some = next((line for line in content.splitlines() if line.startswith("some ")), "")
    full = next((line for line in content.splitlines() if line.startswith("full ")), "")
    value = next((float(token.split("=", 1)[1]) for token in full.split() if token.startswith("avg10=")), 0.0)
    return some, full, value


def stat(parent: Path) -> dict[str, Any]:
    values: dict[str, int] = {}
    try: values["parent_current"] = int((parent / "memory.current").read_text().strip())
    except (OSError, ValueError): values["parent_current"] = 0
    try: values["parent_max"] = int((parent / "memory.max").read_text().strip())
    except (OSError, ValueError): values["parent_max"] = 0
    try:
        for line in (parent / "memory.stat").read_text().splitlines():
            key, value = line.split(); values[key] = int(value)
    except (OSError, ValueError): pass
    try: values["memory_swap_current"] = int((parent / "memory.swap.current").read_text().strip())
    except (OSError, ValueError): values["memory_swap_current"] = 0
    memavailable = 0
    for line in Path("/proc/meminfo").read_text().splitlines():
        if line.startswith("MemAvailable:"): memavailable = int(line.split()[1]) * 1024
    sys_some, sys_full, sys_value = psi(Path("/proc/pressure/memory")); par_some, par_full, par_value = psi(parent / "memory.pressure")
    return {**values, "mem_available": memavailable, "system_psi_some": sys_some, "system_psi_full": sys_full, "system_psi_full_avg10": sys_value, "parent_psi_some": par_some, "parent_psi_full": par_full, "parent_psi_full_avg10": par_value}


def current_foreground(events: Path) -> str:
    try:
        with events.open(encoding="utf-8", newline="") as stream: rows = list(csv.DictReader(stream))
    except OSError: return ""
    for row in reversed(rows):
        if row.get("event_type") in {"APP_OPEN", "APP_SWITCH"}:
            value = row.get("foreground_app") or row.get("new_app") or row.get("app") or ""
            if value and value != "UNKNOWN": return value
    return ""


def appbind_ready(path: Path, app: str) -> bool:
    try:
        with path.open(encoding="utf-8", newline="") as stream:
            return any(row.get("event_type") == "app_bind" and row.get("current_app") == app and row.get("write_success") == "true" for row in csv.DictReader(stream))
    except OSError: return False


def expected_cgroup(parent: Path, scope_name: str, pid: int) -> bool:
    expected = parent / scope_name
    try:
        actual = next(line.split("::", 1)[1].strip() for line in (Path("/proc") / str(pid) / "cgroup").read_text().splitlines() if line.startswith("0::"))
    except (OSError, StopIteration): return False
    return expected.is_dir() and str(expected) == str(Path("/sys/fs/cgroup") / actual.lstrip("/"))


def write_row(writer: csv.DictWriter, **values: Any) -> None:
    """Write a complete sample row while keeping absent counters explicit."""
    writer.writerow({field: values.get(field, "") for field in FIELDS})


def main() -> None:
    parser = argparse.ArgumentParser(); parser.add_argument("--session-dir", type=Path, required=True); parser.add_argument("--app", required=True); args = parser.parse_args()
    session = args.session_dir.resolve(); config = json.loads((session / "calibration/calibration_config.json").read_text(encoding="utf-8"))
    created = json.loads((session / "cgroup_created.json").read_text(encoding="utf-8")); parent = Path(created["path"])
    work = [item for item in config["workers"] if item["app_key"] == args.app]
    output = session / "calibration/calibration_steps.csv"; exists = output.exists()
    stream = output.open("a", encoding="utf-8", newline=""); writer = csv.DictWriter(stream, fieldnames=FIELDS)
    if not exists: writer.writeheader()
    events = session / "model/direct_app_events.csv"; bridge = session / "parp/parp_bridge_events.csv"
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and (current_foreground(events) != args.app or not appbind_ready(bridge, args.app)):
        time.sleep(.2)
    ready = current_foreground(events) == args.app and appbind_ready(bridge, args.app)
    summary = session / "calibration/calibration_summary.json"
    prior = json.loads(summary.read_text(encoding="utf-8")) if summary.exists() else {"apps": {}}
    prior_allocated = sum(int(item.get("allocated_bytes", 0)) for item in dict(prior.get("apps", {})).values())
    allocated = 0; partial = False; partial_reason = ""
    stable_required = int(config.get("startup_stable_required_samples", 0))
    stable_seconds = float(config.get("startup_stabilization_seconds", 0))
    stable_count = 0; stabilization_passed = stable_seconds <= 0
    if ready and stable_seconds > 0:
        deadline = time.monotonic() + stable_seconds
        while time.monotonic() < deadline:
            sample = stat(parent)
            unsafe = sample["system_psi_full_avg10"] >= config["psi_full_abort_avg10"] or sample["parent_psi_full_avg10"] >= config["psi_full_abort_avg10"]
            stable_count = 0 if unsafe else stable_count + 1
            stabilization_passed = stable_count >= max(1, stable_required)
            write_row(writer, timestamp_ns=time.time_ns(), app_key=args.app, worker_id="", stage="APP_STARTUP_STABILIZATION",
                      stage_index=-1, sample_point="STABILITY_SAMPLE", decision="STABLE" if stabilization_passed else "WAIT_FOR_SAFE_PSI",
                      reason="PSI_SAFETY_STALL" if unsafe else "", response="", requested_bytes=0,
                      global_allocated_bytes=prior_allocated, dynamic_budget_bytes=0, operation_latency_us=0, **sample)
            stream.flush()
            if stabilization_passed:
                break
            time.sleep(float(config["pause_seconds"]))
    if ready and not stabilization_passed:
        partial = True; partial_reason = "APP_STARTUP_PSI_NOT_STABLE"
    baseline = stat(parent)
    baseline_reason = "" if ready and stabilization_passed else ("FOREGROUND_OR_APPBIND_NOT_READY" if not ready else "APP_STARTUP_PSI_NOT_STABLE")
    write_row(writer, timestamp_ns=time.time_ns(), app_key=args.app, worker_id="", stage="APP_READY_BASELINE",
              stage_index=-1, sample_point="BEFORE_ALLOCATION", decision="READY" if not baseline_reason else "PARTIAL_READY",
              reason=baseline_reason, response="", requested_bytes=0, global_allocated_bytes=prior_allocated,
              dynamic_budget_bytes=0, operation_latency_us=0, **baseline)
    stream.flush()
    if baseline_reason:
        partial = True; partial_reason = baseline_reason
    for index, worker in enumerate(work):
        before = stat(parent); dynamic = min(config["configured_max_bytes"] - prior_allocated - allocated, before["mem_available"] - config["system_reserve_bytes"], before["parent_max"] - before["parent_current"] - config["cgroup_reserve_bytes"])
        reason = ""
        if not ready: reason = "FOREGROUND_OR_APPBIND_NOT_READY"
        elif not stabilization_passed: reason = "APP_STARTUP_PSI_NOT_STABLE"
        elif dynamic < config["chunk_bytes"]: reason = "GLOBAL_BUDGET_EXHAUSTED"
        elif before["system_psi_full_avg10"] >= config["psi_full_abort_avg10"] or before["parent_psi_full_avg10"] >= config["psi_full_abort_avg10"]: reason = "PSI_SAFETY_STALL"
        ok, response = command(worker["socket_path"], "STATUS") if not reason else (False, "")
        pid = int(fields(response).get("pid", "0") or 0)
        if not reason and not expected_cgroup(parent, worker["scope_name"], pid): reason = "BALLAST_WRONG_CGROUP"
        write_row(writer, timestamp_ns=time.time_ns(), app_key=args.app, worker_id=worker["worker_id"],
                  stage=worker["stage"], stage_index=index, sample_point="BEFORE_ALLOC",
                  decision="READY" if not reason else "PARTIAL_READY", reason=reason, response=response,
                  requested_bytes=config["chunk_bytes"], global_allocated_bytes=prior_allocated + allocated,
                  dynamic_budget_bytes=dynamic, operation_latency_us=0, **before)
        stream.flush()
        if reason:
            partial = True; partial_reason = reason; break
        started = time.perf_counter_ns()
        ok, response = command(worker["socket_path"], "ENTER_FOREGROUND")
        if not ok: reason = "ENTER_FOREGROUND_FAILED"
        if not reason:
            ok, response = command(worker["socket_path"], "ALLOCATE")
            if not ok: reason = "ALLOCATE_FAILED"
        elapsed = int((time.perf_counter_ns() - started) / 1000)
        after = stat(parent)
        decision = "ALLOCATED" if not reason else "PARTIAL_READY"
        write_row(writer, timestamp_ns=time.time_ns(), app_key=args.app, worker_id=worker["worker_id"],
                  stage=worker["stage"], stage_index=index, sample_point="AFTER_ALLOC", decision=decision,
                  reason=reason, response=response, requested_bytes=config["chunk_bytes"],
                  global_allocated_bytes=prior_allocated + allocated + (config["chunk_bytes"] if not reason else 0),
                  dynamic_budget_bytes=dynamic, operation_latency_us=elapsed, **after)
        stream.flush()
        if reason:
            partial = True; partial_reason = reason; break
        allocated += config["chunk_bytes"]
        time.sleep(float(config["pause_seconds"]))
        post = stat(parent)
        post_reason = ""
        if post["system_psi_full_avg10"] >= config["psi_full_abort_avg10"] or post["parent_psi_full_avg10"] >= config["psi_full_abort_avg10"]:
            post_reason = "PSI_SAFETY_STALL_AFTER_CHUNK"
        write_row(writer, timestamp_ns=time.time_ns(), app_key=args.app, worker_id=worker["worker_id"],
                  stage=worker["stage"], stage_index=index, sample_point="AFTER_PAUSE",
                  decision="OBSERVED" if not post_reason else "PARTIAL_READY", reason=post_reason, response="",
                  requested_bytes=config["chunk_bytes"], global_allocated_bytes=prior_allocated + allocated,
                  dynamic_budget_bytes=dynamic, operation_latency_us=0, **post)
        stream.flush()
        if post_reason:
            partial = True; partial_reason = post_reason; break
    for worker in work:
        command(worker["socket_path"], "ENTER_BACKGROUND")
    # These two samples isolate the small, one-page background hot tick from
    # a quiet idle interval.  Neither command can allocate or touch cold pages.
    time.sleep(float(config["pause_seconds"]))
    hot = stat(parent)
    write_row(writer, timestamp_ns=time.time_ns(), app_key=args.app, worker_id="", stage="HOT_ACCESS",
              stage_index=len(work), sample_point="AFTER_BACKGROUND_HOT", decision="OBSERVED", reason="", response="",
              requested_bytes=0, global_allocated_bytes=prior_allocated + allocated,
              dynamic_budget_bytes=0, operation_latency_us=0, **hot)
    stream.flush()
    time.sleep(float(config["pause_seconds"]))
    idle = stat(parent)
    write_row(writer, timestamp_ns=time.time_ns(), app_key=args.app, worker_id="", stage="IDLE",
              stage_index=len(work), sample_point="AFTER_IDLE", decision="OBSERVED", reason="", response="",
              requested_bytes=0, global_allocated_bytes=prior_allocated + allocated,
              dynamic_budget_bytes=0, operation_latency_us=0, **idle)
    stream.flush()
    current = prior
    current.setdefault("apps", {})[args.app] = {"allocated_bytes": allocated, "status": "PARTIAL_READY" if partial else "FULL_READY", "partial_reason": partial_reason, "startup_stabilization_passed": stabilization_passed, "timestamp_ns": time.time_ns(), "no_memory_reclaim": True}
    current["global_allocated_bytes"] = prior_allocated + allocated
    summary.write_text(json.dumps(current, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    stream.close()


if __name__ == "__main__":
    main()
