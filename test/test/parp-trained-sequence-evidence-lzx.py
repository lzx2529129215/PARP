#!/usr/bin/env python3
"""Evidence helpers for LSAPP-trained PARP reclaim experiments.  lzx-note"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import subprocess
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

from memsched_exp.cache_state import file_residency


MIB = 1024 * 1024
SERVICE_OUTPUT_ROOT = Path(
    "/home/lzx/Desktop/PARP/lzx/service/outputs/runtime_monitor"
)


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def read_csv(path: Path) -> list[dict[str, str]]:
    try:
        with path.open(encoding="utf-8", newline="") as stream:
            return list(csv.DictReader(stream))
    except (FileNotFoundError, PermissionError, OSError):
        return []


def read_kv(path: Path) -> dict[str, int]:
    values: dict[str, int] = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            fields = line.split()
            if len(fields) == 2:
                try:
                    values[fields[0]] = int(fields[1])
                except ValueError:
                    continue
    except (FileNotFoundError, PermissionError, OSError):
        pass
    return values


def read_int(path: Path) -> int | None:
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (FileNotFoundError, PermissionError, OSError, ValueError):
        return None


def active_service_session(output_root: Path = SERVICE_OUTPUT_ROOT) -> Path:
    """Resolve the output directory owned by the currently running service. lzx-note"""
    show = subprocess.run(
        ["systemctl", "--user", "show", "parp-runtime-monitor.service", "-p", "MainPID", "--value"],
        text=True,
        capture_output=True,
        check=False,
        timeout=5,
    )
    try:
        pid = int(show.stdout.strip())
        argv = (Path("/proc") / str(pid) / "cmdline").read_bytes().split(b"\0")
        text = [item.decode("utf-8", errors="replace") for item in argv if item]
        if "--session-id" in text:
            session_id = text[text.index("--session-id") + 1]
            candidate = output_root / session_id
            if candidate.is_dir():
                return candidate
    except (FileNotFoundError, PermissionError, OSError, ValueError, IndexError):
        pass
    candidates = sorted(
        (path for path in output_root.glob("service_*") if path.is_dir()),
        key=lambda path: path.stat().st_mtime,
    )
    if not candidates:
        raise RuntimeError(f"no runtime service session below {output_root}")
    return candidates[-1]


def parse_iso_ns(value: str) -> int:
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return int(parsed.timestamp() * 1_000_000_000)


def mark(args: argparse.Namespace) -> int:
    payload = {
        "schema_version": 1,
        "realtime_ns": time.time_ns(),
        "monotonic_ns": time.monotonic_ns(),
        "created_at": dt.datetime.now().astimezone().isoformat(),
    }
    write_json(args.output, payload)
    print(args.output)
    return 0


def capture_json(args: argparse.Namespace) -> int:
    """Freeze transient worker state before cleanup changes it. lzx-note"""
    payload = read_json(args.input)
    payload["captured_realtime_ns"] = time.time_ns()
    write_json(args.output, payload)
    print(args.output)
    return 0


def global_pressure_plan(args: argparse.Namespace) -> int:
    """Freeze the host-memory boundary used by global bin experiments. lzx-note"""
    values: dict[str, int] = {}
    for line in Path("/proc/meminfo").read_text(encoding="ascii").splitlines():
        fields = line.split()
        if len(fields) >= 2 and fields[0] in {"MemFree:", "MemAvailable:", "Cached:"}:
            values[fields[0].rstrip(":")] = int(fields[1]) * 1024
    free_bytes = values.get("MemFree", 0)
    available = values.get("MemAvailable", 0)
    target = int(args.target_memfree_mib) * MIB
    probe = int(args.reclaim_probe_mib) * MIB
    maximum = int(args.max_allocate_mib) * MIB
    payload = {
        "schema_version": 1,
        "valid": bool(free_bytes > target > 0 and probe > 0 and maximum > 0),
        "mode": "global_memfree_reclaim_probe",
        "memfree_before_bytes": free_bytes,
        "memavailable_before_bytes": available,
        "cached_before_bytes": values.get("Cached", 0),
        "target_memfree_bytes": target,
        "reclaim_probe_bytes": probe,
        "planned_ramp_bytes": max(0, free_bytes - target),
        "planned_total_allocation_bytes": max(0, free_bytes - target) + probe,
        "max_allocate_bytes": maximum,
        "created_realtime_ns": time.time_ns(),
    }
    write_json(args.output, payload)
    print(args.output)
    return 0 if payload["valid"] else 8


def prediction_groups(path: Path) -> list[list[dict[str, str]]]:
    groups: dict[str, list[dict[str, str]]] = defaultdict(list)
    order: list[str] = []
    for row in read_csv(path):
        key = row.get("feature_window_id", "")
        if key not in groups:
            order.append(key)
        groups[key].append(row)
    return [groups[key] for key in order]


def evaluate_prediction_gate(args: argparse.Namespace) -> dict[str, Any]:
    marker = read_json(args.after_mark)
    session = active_service_session(args.service_output_root)
    prediction_path = session / "model/online_lstm_predictions.csv"
    myfs_path = session / "parp/myfs_events.csv"
    expected_history = args.history.split("|")
    expected_opened = set(args.opened.split("|"))
    expected_ranks = {
        app: index + 1 for index, app in enumerate(args.expected_next.split("|"))
    }
    cold_apps = set(args.cold.split("|"))
    candidates: list[tuple[int, list[dict[str, str]]]] = []
    for group in prediction_groups(prediction_path):
        if not group:
            continue
        first = group[0]
        try:
            timestamp_ns = parse_iso_ns(first.get("timestamp", ""))
        except (ValueError, TypeError):
            continue
        history = first.get("history_apps", "").split("|")
        opened = set(filter(None, first.get("mapped_opened_apps", "").split("|")))
        if (
            timestamp_ns >= int(marker["realtime_ns"])
            and first.get("mapped_foreground_app") == args.current
            and history == expected_history
            and expected_opened.issubset(opened)
        ):
            candidates.append((timestamp_ns, group))
    reasons: list[str] = []
    selected: list[dict[str, str]] = []
    if candidates:
        _, selected = max(candidates, key=lambda item: item[0])
    else:
        reasons.append("no post-marker prediction matches current/history/opened-app contract")
    rows = {row.get("app", ""): row for row in selected}
    observed: dict[str, Any] = {}
    for app, expected_rank in expected_ranks.items():
        row = rows.get(app)
        if row is None:
            reasons.append(f"missing prediction for {app}")
            continue
        try:
            rank = int(row.get("rank", "0"))
            probability = float(row.get("probability", "nan"))
        except ValueError:
            reasons.append(f"invalid rank/probability for {app}")
            continue
        observed[app] = {"rank": rank, "probability": probability}
        if rank != expected_rank:
            reasons.append(f"{app} rank {rank}, expected {expected_rank}")
        if probability < args.minimum_hot_probability:
            reasons.append(
                f"{app} probability {probability:.6f} below {args.minimum_hot_probability:.6f}"
            )
    for app in sorted(cold_apps):
        row = rows.get(app)
        if row is None:
            reasons.append(f"missing cold prediction for {app}")
            continue
        try:
            rank = int(row.get("rank", "0"))
            probability = float(row.get("probability", "nan"))
        except ValueError:
            reasons.append(f"invalid cold rank/probability for {app}")
            continue
        observed[app] = {"rank": rank, "probability": probability}
        if probability > args.maximum_cold_probability:
            reasons.append(
                f"{app} probability {probability:.6f} exceeds {args.maximum_cold_probability:.6f}"
            )

    myfs_candidates: list[dict[str, str]] = []
    for row in read_csv(myfs_path):
        try:
            timestamp_ns = int(row.get("timestamp_ns", "0"))
        except ValueError:
            continue
        if (
            timestamp_ns >= int(marker["monotonic_ns"])
            and row.get("current_app") == args.current_key
        ):
            myfs_candidates.append(row)
    myfs = myfs_candidates[-1] if myfs_candidates else {}
    expected_workload: dict[str, str] = {}
    for item in filter(None, str(args.expected_workload_profiles).split("|")):
        try:
            app_key, workload_class = item.split(":", 1)
        except ValueError:
            reasons.append(f"invalid expected workload profile: {item}")
            continue
        expected_workload[app_key] = workload_class
    observed_workload: dict[str, list[dict[str, Any]]] = {}
    if args.require_myfs:
        if not myfs:
            reasons.append("no post-marker /dev/myfs event for current app")
        else:
            if myfs.get("status") != "APPLIED" or myfs.get("ioctl_success") != "true":
                reasons.append(
                    f"/dev/myfs status={myfs.get('status')} success={myfs.get('ioctl_success')}"
                )
            try:
                bindings = int(myfs.get("nr_bindings", "0"))
            except ValueError:
                bindings = 0
            if bindings < args.minimum_bindings:
                reasons.append(
                    f"/dev/myfs bindings {bindings}, expected at least {args.minimum_bindings}"
                )
            if myfs.get("ambiguous_domains") not in {"0", 0}:
                reasons.append(f"ambiguous /dev/myfs domains={myfs.get('ambiguous_domains')}")
            try:
                kernel_abi = int(myfs.get("kernel_abi_version", "0"))
            except (TypeError, ValueError):
                kernel_abi = 0
            if kernel_abi < int(args.minimum_myfs_abi):
                reasons.append(
                    f"kernel myfs ABI={myfs.get('kernel_abi_version')}, "
                    f"expected >= {args.minimum_myfs_abi}"
                )  # lzx-note
            if expected_workload:
                try:
                    workload_details = json.loads(myfs.get("workload_binding_details", "[]"))
                except (TypeError, json.JSONDecodeError):
                    workload_details = []
                if not isinstance(workload_details, list):
                    workload_details = []
                for detail in workload_details:
                    if not isinstance(detail, dict):
                        continue
                    app_key = str(detail.get("app_key", ""))
                    if app_key:
                        observed_workload.setdefault(app_key, []).append(detail)
                for app_key, workload_class in sorted(expected_workload.items()):
                    matched = any(
                        item.get("class") == workload_class and item.get("valid") is True
                        for item in observed_workload.get(app_key, [])
                    )
                    if not matched:
                        reasons.append(
                            f"/dev/myfs workload profile mismatch for {app_key}: "
                            f"expected {workload_class}, got {observed_workload.get(app_key, [])}"
                        )  # lzx-note
    return {
        "schema_version": 1,
        "valid": not reasons,
        "reasons": reasons,
        "service_session": str(session),
        "prediction_file": str(prediction_path),
        "myfs_file": str(myfs_path),
        "history": expected_history,
        "opened_apps": sorted(expected_opened),
        "current_app": args.current,
        "expected_next_ranks": expected_ranks,
        "observed": observed,
        "prediction_feature_window_id": selected[0].get("feature_window_id") if selected else None,
        "prediction_trigger": selected[0].get("trigger_type") if selected else None,
        "myfs": myfs,
        "expected_workload_profiles": expected_workload,
        "observed_workload_profiles": observed_workload,  # lzx-note
    }


def prediction_gate(args: argparse.Namespace) -> int:
    deadline = time.monotonic() + args.timeout
    result: dict[str, Any] = {"valid": False, "reasons": ["gate not evaluated"]}
    while True:
        result = evaluate_prediction_gate(args)
        if result["valid"] or time.monotonic() >= deadline:
            break
        time.sleep(args.poll_seconds)
    write_json(args.output, result)
    print(args.output)
    if not result["valid"]:
        print("prediction gate failed: " + "; ".join(result["reasons"]), flush=True)
    return 0 if result["valid"] else 4


def resolve_scope(root: Path, name: str) -> Path | None:
    matches = [path for path in root.rglob(name) if path.is_dir()]
    return matches[0] if len(matches) == 1 else None


def scope_snapshot(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {"valid": False, "reason": "scope missing"}
    try:
        stat = path.stat()
    except OSError as exc:
        return {"valid": False, "reason": f"scope stat failed: {exc}"}
    return {
        "valid": True,
        "path": str(path),
        "device": stat.st_dev,
        "inode": stat.st_ino,
        "memory_current": read_int(path / "memory.current"),
        "memory_peak": read_int(path / "memory.peak"),
        "memory_stat": read_kv(path / "memory.stat"),
        "memory_events": read_kv(path / "memory.events"),
    }


def host_snapshot() -> dict[str, Any]:
    return {
        "vmstat": read_kv(Path("/proc/vmstat")),
        "meminfo": read_kv(Path("/proc/meminfo")),
        "pressure_memory": Path("/proc/pressure/memory").read_text(encoding="utf-8").strip(),
    }


def snapshot(args: argparse.Namespace) -> int:
    apps: dict[str, Any] = {}
    for app in args.apps.split("|"):
        slug = app.lower().replace("_", "-")
        gui = resolve_scope(args.cgroup, f"automation-{slug}.scope")
        fixture = resolve_scope(args.cgroup, f"automation-fixture-{slug}.scope")
        sparse = args.ballast / f"{slug}.sparse"  # lzx-note
        apps[app] = {
            "gui": scope_snapshot(gui),
            "fixture": scope_snapshot(fixture),
            "file_residency": file_residency(sparse),
        }
    payload = {
        "schema_version": 1,
        "label": args.label,
        "timestamp_ns": time.time_ns(),
        "monotonic_ns": time.monotonic_ns(),
        "cgroup": scope_snapshot(args.cgroup),
        "host": host_snapshot(),
        "apps": apps,
    }
    write_json(args.output, payload)
    invalid = [app for app, value in apps.items() if not value["fixture"].get("valid")]
    invalid += [app for app, value in apps.items() if not value["file_residency"].get("valid")]
    print(args.output)
    return 0 if not invalid else 5


def set_boundary(args: argparse.Namespace) -> int:
    current = read_int(args.cgroup / "memory.current")
    if current is None:
        raise RuntimeError(f"cannot read {args.cgroup / 'memory.current'}")
    headroom = (args.pressure_mib - args.reclaim_mib) * MIB
    if headroom <= 0:
        raise ValueError("pressure-mib must exceed reclaim-mib")
    maximum = current + headroom
    outcome = subprocess.run(
        [
            "systemctl", "--user", "set-property", "--runtime", args.slice,
            f"MemoryMax={maximum}", "MemoryHigh=infinity", "MemorySwapMax=0",
        ],
        text=True,
        capture_output=True,
        check=False,
        timeout=15,
    )
    observed = read_int(args.cgroup / "memory.max")
    payload = {
        "schema_version": 1,
        "valid": outcome.returncode == 0 and observed == maximum,
        "slice": args.slice,
        "cgroup": str(args.cgroup),
        "memory_current_before": current,
        "pressure_bytes": args.pressure_mib * MIB,
        "reclaim_target_bytes": args.reclaim_mib * MIB,
        "headroom_bytes": headroom,
        "requested_memory_max": maximum,
        "observed_memory_max": observed,
        "stderr": outcome.stderr.strip(),
    }
    write_json(args.output, payload)
    print(args.output)
    return 0 if payload["valid"] else 6


def delta(after: dict[str, int], before: dict[str, int], key: str) -> int:
    return int(after.get(key, 0)) - int(before.get(key, 0))


def compare(args: argparse.Namespace) -> int:
    before = read_json(args.before)
    after = read_json(args.after)
    hot = set(args.hot.split("|"))
    cold = set(args.cold.split("|"))
    app_rows: dict[str, Any] = {}
    total_reclaimed = 0
    for app in sorted(hot | cold):
        first = before["apps"][app]
        last = after["apps"][app]
        first_resident = int(first["file_residency"].get("resident_pages") or 0)
        last_resident = int(last["file_residency"].get("resident_pages") or 0)
        page_size = int(first["file_residency"].get("page_size") or os.sysconf("SC_PAGE_SIZE"))
        reclaimed = max(0, first_resident - last_resident) * page_size
        total_reclaimed += reclaimed
        first_stat = first["fixture"].get("memory_stat", {})
        last_stat = last["fixture"].get("memory_stat", {})
        app_rows[app] = {
            "class": "hot" if app in hot else "cold",
            "resident_bytes_before": first_resident * page_size,
            "resident_bytes_after": last_resident * page_size,
            "resident_reclaimed_bytes": reclaimed,
            "resident_reclaimed_percent": (
                100.0 * reclaimed / (first_resident * page_size)
                if first_resident else None
            ),
            "fixture_deltas": {
                key: delta(last_stat, first_stat, key)
                for key in (
                    "pgfault", "pgmajfault", "workingset_refault_file",
                    "pgscan", "pgsteal", "pgscan_direct", "pgsteal_direct",
                    "pgscan_kswapd", "pgsteal_kswapd",
                )
            },
        }
    cold_reclaimed = sum(
        row["resident_reclaimed_bytes"] for row in app_rows.values() if row["class"] == "cold"
    )
    hot_reclaimed = sum(
        row["resident_reclaimed_bytes"] for row in app_rows.values() if row["class"] == "hot"
    )
    first_parent = before["cgroup"].get("memory_stat", {})
    last_parent = after["cgroup"].get("memory_stat", {})
    payload = {
        "schema_version": 1,
        "valid": total_reclaimed > 0,
        "before": str(args.before),
        "after": str(args.after),
        "apps": app_rows,
        "source_distribution": {
            "total_file_resident_reclaimed_bytes": total_reclaimed,
            "hot_reclaimed_bytes": hot_reclaimed,
            "cold_reclaimed_bytes": cold_reclaimed,
            "hot_share_percent": 100.0 * hot_reclaimed / total_reclaimed if total_reclaimed else None,
            "cold_share_percent": 100.0 * cold_reclaimed / total_reclaimed if total_reclaimed else None,
        },
        "parent_deltas": {
            key: delta(last_parent, first_parent, key)
            for key in (
                "pgfault", "pgmajfault", "workingset_refault_file",
                "pgscan", "pgsteal", "pgscan_direct", "pgsteal_direct",
                "pgscan_kswapd", "pgsteal_kswapd",
            )
        },
    }
    write_json(args.output, payload)
    print(args.output)
    return 0 if payload["valid"] else 7


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser()
    sub = root.add_subparsers(dest="command", required=True)

    mark_parser = sub.add_parser("mark")
    mark_parser.add_argument("--output", type=Path, required=True)
    mark_parser.set_defaults(func=mark)

    capture = sub.add_parser("capture-json")
    capture.add_argument("--input", type=Path, required=True)
    capture.add_argument("--output", type=Path, required=True)
    capture.set_defaults(func=capture_json)

    global_plan = sub.add_parser("global-pressure-plan")
    global_plan.add_argument("--target-memfree-mib", type=int, required=True)
    global_plan.add_argument("--reclaim-probe-mib", type=int, required=True)
    global_plan.add_argument("--max-allocate-mib", type=int, required=True)
    global_plan.add_argument("--output", type=Path, required=True)
    global_plan.set_defaults(func=global_pressure_plan)

    gate = sub.add_parser("prediction-gate")
    gate.add_argument("--after-mark", type=Path, required=True)
    gate.add_argument("--output", type=Path, required=True)
    gate.add_argument("--service-output-root", type=Path, default=SERVICE_OUTPUT_ROOT)
    gate.add_argument("--history", required=True)
    gate.add_argument("--opened", required=True)
    gate.add_argument("--current", required=True)
    gate.add_argument("--current-key", required=True)
    gate.add_argument("--expected-next", required=True)
    gate.add_argument("--cold", required=True)
    gate.add_argument("--minimum-hot-probability", type=float, default=0.10)
    gate.add_argument("--maximum-cold-probability", type=float, default=0.01)
    gate.add_argument("--minimum-bindings", type=int, default=16)
    gate.add_argument("--minimum-myfs-abi", type=int, default=2)  # lzx-note
    gate.add_argument("--expected-workload-profiles", default="")  # APP_KEY:CLASS|... lzx-note
    gate.add_argument("--require-myfs", action=argparse.BooleanOptionalAction, default=True)
    gate.add_argument("--timeout", type=float, default=20.0)
    gate.add_argument("--poll-seconds", type=float, default=0.25)
    gate.set_defaults(func=prediction_gate)

    snap = sub.add_parser("snapshot")
    snap.add_argument("--cgroup", type=Path, required=True)
    snap.add_argument("--ballast", type=Path, required=True)
    snap.add_argument("--apps", required=True)
    snap.add_argument("--label", required=True)
    snap.add_argument("--output", type=Path, required=True)
    snap.set_defaults(func=snapshot)

    boundary = sub.add_parser("set-boundary")
    boundary.add_argument("--slice", required=True)
    boundary.add_argument("--cgroup", type=Path, required=True)
    boundary.add_argument("--pressure-mib", type=int, required=True)
    boundary.add_argument("--reclaim-mib", type=int, required=True)
    boundary.add_argument("--output", type=Path, required=True)
    boundary.set_defaults(func=set_boundary)

    comparison = sub.add_parser("compare")
    comparison.add_argument("--before", type=Path, required=True)
    comparison.add_argument("--after", type=Path, required=True)
    comparison.add_argument("--hot", required=True)
    comparison.add_argument("--cold", required=True)
    comparison.add_argument("--output", type=Path, required=True)
    comparison.set_defaults(func=compare)
    return root


def main() -> int:
    args = parser().parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
