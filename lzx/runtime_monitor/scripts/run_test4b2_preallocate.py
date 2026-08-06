#!/usr/bin/env python3
"""Construct Test4B-2 ballast in audited four-MiB foreground-only chunks.

This helper is run by the automation scenario.  It is deliberately separate
from the monitor's event handler: the monitor can only observe preallocated
ballast in this Test4B-2 mode and cannot allocate it as a side effect of a
prediction or a periodic tick.
"""
from __future__ import annotations

import argparse
import csv
import json
import socket
import time
from pathlib import Path
from typing import Any


MIB = 1024 * 1024
APPS = ("FIREFOX", "THUNDERBIRD", "TELEGRAM")
FIELDS = [
    "timestamp_ns", "app_key", "worker_id", "stage", "stage_index", "sample_point", "decision", "reason", "response",
    "requested_bytes", "global_allocated_bytes", "dynamic_budget_bytes", "parent_current", "parent_max", "anon", "file",
    "memory_swap_current", "mem_available", "system_psi_some", "system_psi_full", "system_psi_some_avg10", "system_psi_full_avg10",
    "system_psi_some_total", "system_psi_full_total", "parent_psi_some", "parent_psi_full", "parent_psi_some_avg10", "parent_psi_full_avg10",
    "parent_psi_some_total", "parent_psi_full_total", "system_psi_some_total_delta", "system_psi_full_total_delta",
    "parent_psi_some_total_delta", "parent_psi_full_total_delta", "operation_latency_us",
]


def request(path: str, value: str) -> tuple[bool, str]:
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as stream:
            stream.settimeout(2); stream.connect(path); stream.sendall((value + "\n").encode())
            response = stream.recv(2048).decode("utf-8", errors="replace").strip()
        return response.startswith("OK "), response
    except OSError as exc:
        return False, f"SOCKET_{type(exc).__name__}"


def response_fields(text: str) -> dict[str, str]:
    return {key: value for key, value in (part.split("=", 1) for part in text.split() if "=" in part)}


def pressure(path: Path) -> dict[str, Any]:
    try: lines = path.read_text(encoding="utf-8").splitlines()
    except OSError: lines = []
    answer: dict[str, Any] = {}
    for kind in ("some", "full"):
        line = next((item for item in lines if item.startswith(kind + " ")), "")
        values = {key: value for key, value in (part.split("=", 1) for part in line.split()[1:] if "=" in part)}
        try: avg10 = float(values.get("avg10", 0))
        except ValueError: avg10 = 0.0
        try: total = int(values.get("total", 0))
        except ValueError: total = 0
        answer[kind] = line; answer[f"{kind}_avg10"] = avg10; answer[f"{kind}_total"] = total
    return answer


def sample(parent: Path) -> dict[str, Any]:
    values: dict[str, Any] = {"parent_current": 0, "parent_max": 0, "anon": 0, "file": 0, "memory_swap_current": 0, "mem_available": 0}
    for name, key in (("memory.current", "parent_current"), ("memory.max", "parent_max"), ("memory.swap.current", "memory_swap_current")):
        try: values[key] = int((parent / name).read_text().strip())
        except (OSError, ValueError): pass
    try:
        for line in (parent / "memory.stat").read_text().splitlines():
            key, value = line.split();
            if key in {"anon", "file"}: values[key] = int(value)
    except (OSError, ValueError): pass
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"): values["mem_available"] = int(line.split()[1]) * 1024
    except OSError: pass
    system, local = pressure(Path("/proc/pressure/memory")), pressure(parent / "memory.pressure")
    for prefix, raw in (("system_psi", system), ("parent_psi", local)):
        for kind in ("some", "full"):
            values[f"{prefix}_{kind}"] = raw[kind]
            values[f"{prefix}_{kind}_avg10"] = raw[f"{kind}_avg10"]
            values[f"{prefix}_{kind}_total"] = raw[f"{kind}_total"]
    return values


def current_foreground(events: Path) -> str:
    try:
        with events.open(encoding="utf-8", newline="") as stream: data = list(csv.DictReader(stream))
    except OSError: return ""
    for row in reversed(data):
        if row.get("event_type") in {"APP_OPEN", "APP_SWITCH"}:
            app = row.get("foreground_app") or row.get("new_app") or row.get("app") or ""
            if app in APPS: return app
    return ""


def bind_ready(bridge: Path, app: str) -> bool:
    try:
        with bridge.open(encoding="utf-8", newline="") as stream:
            return any(row.get("event_type") == "app_bind" and row.get("current_app") == app and row.get("write_success") == "true" for row in csv.DictReader(stream))
    except OSError: return False


def pid_in_expected_cgroup(parent: Path, scope_name: str, pid: int) -> bool:
    try:
        relative = next(line.split("::", 1)[1].strip() for line in (Path("/proc") / str(pid) / "cgroup").read_text().splitlines() if line.startswith("0::"))
    except (OSError, StopIteration): return False
    return (parent / scope_name).is_dir() and parent / scope_name == Path("/sys/fs/cgroup") / relative.lstrip("/")


def load_json(path: Path, fallback: dict[str, Any]) -> dict[str, Any]:
    try: return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError): return fallback


def app_workers(config: dict[str, Any], app: str) -> list[dict[str, Any]]:
    item = dict(config.get("apps", {})).get(app, {})
    return list(item.get("workers", [])) if isinstance(item, dict) else []


def allocated_bytes(summary: dict[str, Any]) -> int:
    return sum(int(item.get("allocated_bytes", 0)) for item in dict(summary.get("apps", {})).values())


def add_deltas(values: dict[str, Any], before: dict[str, Any] | None) -> dict[str, Any]:
    for field in ("system_psi_some_total", "system_psi_full_total", "parent_psi_some_total", "parent_psi_full_total"):
        values[field + "_delta"] = int(values.get(field, 0)) - int((before or {}).get(field, values.get(field, 0)))
    return values


def write(writer: csv.DictWriter, values: dict[str, Any], before: dict[str, Any] | None = None, **extra: Any) -> None:
    row = add_deltas(dict(values), before); row.update(extra)
    writer.writerow({field: row.get(field, "") for field in FIELDS})


def unsafe(values: dict[str, Any], threshold: float) -> bool:
    return float(values["system_psi_full_avg10"]) >= threshold or float(values["parent_psi_full_avg10"]) >= threshold


def update_summary(path: Path, summary: dict[str, Any], *, app: str | None = None, **data: Any) -> None:
    if app is not None:
        summary.setdefault("apps", {})[app] = data
    summary["global_allocated_bytes"] = allocated_bytes(summary)
    summary["no_memory_reclaim"] = True
    summary["updated_at_ns"] = time.time_ns()
    path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def stabilize(session: Path, config: dict[str, Any], parent: Path, writer: csv.DictWriter, stream: Any, summary_path: Path, summary: dict[str, Any]) -> int:
    events, bridge = session / "model/direct_app_events.csv", session / "parp/parp_bridge_events.csv"
    expected_scopes = {item["app_key"]: item["scope_name"] for item in load_json(session / "configs/test4b2_runtime_app_scope.json", {}).get("apps", [])}
    deadline = time.monotonic() + float(config.get("startup_ready_timeout_seconds", 60)); ready_at: float | None = None; safe_count = 0
    last_reason = "WAIT_FOR_APPS_APBBIND_OR_SIDECARS"
    while time.monotonic() < deadline:
        values = sample(parent); sidecars_ok = True
        for app in APPS:
            for worker in app_workers(config, app):
                ok, response = request(worker["socket_path"], "STATUS"); pid = int(response_fields(response).get("pid", "0") or 0)
                if not ok or not pid_in_expected_cgroup(parent, expected_scopes.get(app, ""), pid): sidecars_ok = False
        binds_ok = all(bind_ready(bridge, app) for app in APPS)
        apps_ok = all((parent / expected_scopes.get(app, "")).is_dir() for app in APPS)
        prerequisites = sidecars_ok and binds_ok and apps_ok
        if prerequisites and ready_at is None: ready_at = time.monotonic()
        elapsed = time.monotonic() - ready_at if ready_at is not None else 0.0
        too_high = unsafe(values, float(config["psi_full_abort_avg10"]))
        # This is deliberately stricter than the 0.20 abort threshold.  It
        # absorbs avg10's trailing history before *any* synthetic page is
        # made resident; every allocation still uses the unchanged 0.20 gate.
        start_target = float(config.get("startup_psi_full_start_avg10", config["psi_full_abort_avg10"]))
        steady = float(values["system_psi_full_avg10"]) < start_target and float(values["parent_psi_full_avg10"]) < start_target
        safe_count = safe_count + 1 if prerequisites and elapsed >= float(config["startup_stabilization_seconds"]) and not too_high and steady else 0
        ready = safe_count >= int(config["startup_stable_required_samples"])
        last_reason = "PSI_SAFETY_STALL" if too_high else ("WAIT_15_SECONDS" if prerequisites and elapsed < float(config["startup_stabilization_seconds"]) else ("" if ready else ("WAIT_FOR_STEADY_PSI" if prerequisites else "WAIT_FOR_APPS_APBBIND_OR_SIDECARS")))
        write(writer, values, None, timestamp_ns=time.time_ns(), app_key="", worker_id="", stage="STARTUP_STABILIZATION", stage_index=-1,
              sample_point="STABILITY_SAMPLE", decision="READY" if ready else "WAIT", reason=last_reason, response="", requested_bytes=0,
              global_allocated_bytes=allocated_bytes(summary), dynamic_budget_bytes=0, operation_latency_us=0)
        stream.flush()
        if ready:
            summary["startup_stabilization"] = {"status": "READY", "safe_samples": safe_count, "waited_seconds": elapsed, "timestamp_ns": time.time_ns()}
            update_summary(summary_path, summary); return 0
        time.sleep(1)
    summary["startup_stabilization"] = {"status": "PARTIAL_READY", "reason": last_reason, "timestamp_ns": time.time_ns()}
    update_summary(summary_path, summary); return 3


def allocate(session: Path, config: dict[str, Any], parent: Path, app: str, writer: csv.DictWriter, stream: Any, summary_path: Path, summary: dict[str, Any]) -> int:
    startup = dict(summary.get("startup_stabilization", {}))
    workers = app_workers(config, app); scopes = {item["app_key"]: item["scope_name"] for item in load_json(session / "configs/test4b2_runtime_app_scope.json", {}).get("apps", [])}
    if startup.get("status") != "READY" or not workers:
        values = sample(parent); write(writer, values, None, timestamp_ns=time.time_ns(), app_key=app, worker_id="", stage="APP_READY", stage_index=-1,
                                       sample_point="BEFORE_ALLOCATION", decision="PARTIAL_READY", reason="STARTUP_STABILIZATION_NOT_READY", response="", requested_bytes=0,
                                       global_allocated_bytes=allocated_bytes(summary), dynamic_budget_bytes=0, operation_latency_us=0); stream.flush(); return 3
    preexisting = dict(summary.get("apps", {})).get(app, {})
    if preexisting.get("status") == "FULL_READY": return 0
    events, bridge = session / "model/direct_app_events.csv", session / "parp/parp_bridge_events.csv"
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline and (current_foreground(events) != app or not bind_ready(bridge, app)):
        time.sleep(.2)
    foreground_ok = current_foreground(events) == app and bind_ready(bridge, app)
    allocated = int(preexisting.get("allocated_bytes", 0)); partial_reason = ""
    for index, worker in enumerate(workers):
        if index < allocated // int(config["chunk_bytes"]): continue
        before = sample(parent); base = allocated_bytes(summary) + allocated
        dynamic = min(int(config["max_global_bytes"]) - base, int(before["mem_available"]) - int(config["system_reserve_bytes"]), int(before["parent_max"]) - int(before["parent_current"]) - int(config["cgroup_reserve_bytes"]))
        reason = ""
        if not foreground_ok: reason = "FOREGROUND_OR_APPBIND_NOT_READY"
        elif unsafe(before, float(config["psi_full_abort_avg10"])): reason = "PSI_SAFETY_STALL"
        elif dynamic < int(config["chunk_bytes"]): reason = "GLOBAL_BUDGET_EXCEEDED"
        else:
            ok, response = request(worker["socket_path"], "STATUS"); pid = int(response_fields(response).get("pid", "0") or 0)
            if not ok: reason = "SIDECAR_UNAVAILABLE"
            elif not pid_in_expected_cgroup(parent, scopes.get(app, ""), pid): reason = "BALLAST_WRONG_CGROUP"
        write(writer, before, None, timestamp_ns=time.time_ns(), app_key=app, worker_id=worker["worker_id"], stage=worker["stage"], stage_index=index,
              sample_point="BEFORE_ALLOC", decision="READY" if not reason else "PARTIAL_READY", reason=reason, response=response if not reason else "", requested_bytes=config["chunk_bytes"],
              global_allocated_bytes=base, dynamic_budget_bytes=dynamic, operation_latency_us=0); stream.flush()
        if reason: partial_reason = reason; break
        started = time.perf_counter_ns(); ok, response = request(worker["socket_path"], "ENTER_FOREGROUND")
        if not ok: reason = "ENTER_FOREGROUND_FAILED"
        if not reason:
            ok, response = request(worker["socket_path"], "ALLOCATE")
            if not ok: reason = "ALLOCATE_FAILED"
        after = sample(parent); elapsed = (time.perf_counter_ns() - started) // 1000
        write(writer, after, before, timestamp_ns=time.time_ns(), app_key=app, worker_id=worker["worker_id"], stage=worker["stage"], stage_index=index,
              sample_point="AFTER_ALLOC", decision="ALLOCATED" if not reason else "PARTIAL_READY", reason=reason, response=response, requested_bytes=config["chunk_bytes"],
              global_allocated_bytes=base + (int(config["chunk_bytes"]) if not reason else 0), dynamic_budget_bytes=dynamic, operation_latency_us=elapsed); stream.flush()
        if reason: partial_reason = reason; break
        allocated += int(config["chunk_bytes"]); time.sleep(float(config["pause_seconds"])); post = sample(parent)
        post_reason = "PSI_SAFETY_STALL_AFTER_CHUNK" if unsafe(post, float(config["psi_full_abort_avg10"])) else ""
        write(writer, post, after, timestamp_ns=time.time_ns(), app_key=app, worker_id=worker["worker_id"], stage=worker["stage"], stage_index=index,
              sample_point="AFTER_PAUSE", decision="OBSERVED" if not post_reason else "PARTIAL_READY", reason=post_reason, response="", requested_bytes=config["chunk_bytes"],
              global_allocated_bytes=base + int(config["chunk_bytes"]), dynamic_budget_bytes=dynamic, operation_latency_us=0); stream.flush()
        if post_reason: partial_reason = post_reason; break
    full = allocated == 40 * MIB
    update_summary(summary_path, summary, app=app, allocated_bytes=allocated, expected_bytes=40*MIB, cold_bytes=32*MIB,
                   status="FULL_READY" if full else "PARTIAL_READY", partial_reason=partial_reason, foreground_verified=foreground_ok,
                   background_new_allocation_bytes=0, timestamp_ns=time.time_ns())
    return 0 if full else 3


def main() -> None:
    parser = argparse.ArgumentParser(); parser.add_argument("--session-dir", type=Path, required=True)
    parser.add_argument("--phase", choices=("stabilize", "allocate"), required=True); parser.add_argument("--app", choices=APPS)
    args = parser.parse_args(); session = args.session_dir.resolve()
    config = load_json(session / "ballast/ballast_config.json", {}); created = load_json(session / "cgroup_created.json", {})
    parent = Path(str(created.get("path", "")))
    if not parent.is_dir(): raise SystemExit("Test4B-2 temporary cgroup is unavailable")
    construction = session / "ballast/construction_steps.csv"; construction.parent.mkdir(exist_ok=True)
    exists = construction.exists(); summary_path = session / "ballast/preallocation_summary.json"; summary = load_json(summary_path, {"apps": {}, "no_memory_reclaim": True})
    with construction.open("a", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=FIELDS)
        if not exists: writer.writeheader()
        rc = stabilize(session, config, parent, writer, stream, summary_path, summary) if args.phase == "stabilize" else allocate(session, config, parent, str(args.app), writer, stream, summary_path, summary)
    raise SystemExit(rc)


if __name__ == "__main__":
    main()
