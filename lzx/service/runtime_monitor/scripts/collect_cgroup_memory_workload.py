#!/usr/bin/env python3
"""Collect lightweight cgroup v2 memory workload metrics for app scopes.

This collector is intentionally read-only. It reads memory.current,
memory.stat, and memory.events under a user systemd slice and does not use
eBPF, cache_ext, smaps/VMA scanning, prefetch, eviction, swap, MGLRU, debugfs,
or page-cache control actions.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


STAT_FIELDS = [
    "anon",
    "file",
    "kernel",
    "slab",
    "pagetables",
    "pgfault",
    "pgmajfault",
    "workingset_refault_file",
    "workingset_refault_anon",
]

EVENT_FIELDS = ["max", "oom", "oom_kill"]

RAW_FIELDS = [
    "session_id",
    "timestamp",
    "scope_name",
    "cgroup_path",
    "memory_current",
    *STAT_FIELDS,
    "memory_events_max",
    "memory_events_oom",
    "memory_events_oom_kill",
    "status",
    "error",
]

DELTA_FIELDS = [
    "session_id",
    "timestamp",
    "scope_name",
    "memory_current_delta",
    "anon_delta",
    "file_delta",
    "kernel_delta",
    "slab_delta",
    "pagetables_delta",
    "pgfault_delta",
    "pgmajfault_delta",
    "workingset_refault_file_delta",
    "workingset_refault_anon_delta",
    "memory_events_max_delta",
    "memory_events_oom_delta",
    "memory_events_oom_kill_delta",
    "major_fault_ratio",
    "status",
    "error",
]

COUNTER_FIELDS = {
    "pgfault",
    "pgmajfault",
    "workingset_refault_file",
    "workingset_refault_anon",
    "memory_events_max",
    "memory_events_oom",
    "memory_events_oom_kill",
}

STOP_REQUESTED = False


def request_stop(_signum: int, _frame: Any) -> None:
    global STOP_REQUESTED
    STOP_REQUESTED = True


@dataclass
class ScopeSample:
    session_id: str
    timestamp: str
    scope_name: str
    cgroup_path: str
    values: dict[str, int]
    status: str
    error: str


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Collect lightweight cgroup v2 memory workload metrics for app scopes."
    )
    parser.add_argument("--session-dir", required=True, help="Runtime Monitor session directory.")
    parser.add_argument("--slice", default="huawei-test.slice", help="User systemd slice name.")
    parser.add_argument("--interval-s", type=float, default=1.0, help="Sampling interval in seconds.")
    parser.add_argument("--duration-s", type=float, default=0.0, help="Optional total duration in seconds; 0 means run until interrupted.")
    parser.add_argument(
        "--scopes",
        default="automation-wps.scope,automation-qq.scope,automation-files.scope",
        help="Comma-separated app scope names under the slice.",
    )
    return parser.parse_args(argv)


def resolve_user_slice_path(slice_name: str) -> tuple[str, Path, str]:
    try:
        result = subprocess.run(
            ["systemctl", "--user", "show", slice_name, "-p", "ControlGroup", "--value"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return "", Path("/sys/fs/cgroup"), f"failed to run systemctl --user show: {exc}"

    control_group = result.stdout.strip()
    if result.returncode != 0:
        error = result.stderr.strip() or f"systemctl returned {result.returncode}"
        return "", Path("/sys/fs/cgroup"), error
    if not control_group:
        return "", Path("/sys/fs/cgroup"), f"empty ControlGroup for {slice_name}"
    return control_group, Path("/sys/fs/cgroup") / control_group.lstrip("/"), ""


def read_int_file(path: Path) -> tuple[int, str]:
    try:
        return int(path.read_text(encoding="utf-8").strip()), ""
    except (OSError, ValueError) as exc:
        return 0, f"{path}: {exc}"


def read_kv_file(path: Path, keys: list[str]) -> tuple[dict[str, int], str]:
    values = {key: 0 for key in keys}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            parts = line.split()
            if len(parts) != 2:
                continue
            key, value = parts
            if key in values:
                try:
                    values[key] = int(value)
                except ValueError:
                    values[key] = 0
        return values, ""
    except OSError as exc:
        return values, f"{path}: {exc}"


def sample_scope(session_id: str, timestamp: str, parent_path: Path, scope_name: str, parent_error: str) -> ScopeSample:
    scope_path = parent_path / scope_name
    values = {field: 0 for field in ["memory_current", *STAT_FIELDS, "memory_events_max", "memory_events_oom", "memory_events_oom_kill"]}

    if parent_error:
        return ScopeSample(
            session_id=session_id,
            timestamp=timestamp,
            scope_name=scope_name,
            cgroup_path=str(scope_path),
            values=values,
            status="missing_cgroup",
            error=parent_error,
        )

    if not scope_path.is_dir():
        return ScopeSample(
            session_id=session_id,
            timestamp=timestamp,
            scope_name=scope_name,
            cgroup_path=str(scope_path),
            values=values,
            status="missing_cgroup",
            error=f"scope cgroup does not exist: {scope_path}",
        )

    errors: list[str] = []
    memory_current, error = read_int_file(scope_path / "memory.current")
    values["memory_current"] = memory_current
    if error:
        errors.append(error)

    stat_values, error = read_kv_file(scope_path / "memory.stat", STAT_FIELDS)
    values.update(stat_values)
    if error:
        errors.append(error)

    event_values, error = read_kv_file(scope_path / "memory.events", EVENT_FIELDS)
    for key in EVENT_FIELDS:
        values[f"memory_events_{key}"] = event_values.get(key, 0)
    if error:
        errors.append(error)

    if errors:
        return ScopeSample(
            session_id=session_id,
            timestamp=timestamp,
            scope_name=scope_name,
            cgroup_path=str(scope_path),
            values=values,
            status="read_error",
            error="; ".join(errors),
        )

    return ScopeSample(
        session_id=session_id,
        timestamp=timestamp,
        scope_name=scope_name,
        cgroup_path=str(scope_path),
        values=values,
        status="ok",
        error="",
    )


def raw_row(sample: ScopeSample) -> dict[str, Any]:
    row: dict[str, Any] = {
        "session_id": sample.session_id,
        "timestamp": sample.timestamp,
        "scope_name": sample.scope_name,
        "cgroup_path": sample.cgroup_path,
        "memory_current": sample.values["memory_current"],
        "status": sample.status,
        "error": sample.error,
    }
    for field in STAT_FIELDS:
        row[field] = sample.values[field]
    for field in EVENT_FIELDS:
        row[f"memory_events_{field}"] = sample.values[f"memory_events_{field}"]
    return row


def delta_value(field: str, current: int, previous: int) -> int:
    delta = current - previous
    if field in COUNTER_FIELDS:
        return max(0, delta)
    return delta


def delta_row(sample: ScopeSample, previous: ScopeSample | None) -> dict[str, Any]:
    row: dict[str, Any] = {
        "session_id": sample.session_id,
        "timestamp": sample.timestamp,
        "scope_name": sample.scope_name,
        "status": sample.status,
        "error": sample.error,
    }

    if sample.status != "ok" or previous is None or previous.status != "ok" or previous.cgroup_path != sample.cgroup_path:
        for field in ["memory_current", *STAT_FIELDS, "memory_events_max", "memory_events_oom", "memory_events_oom_kill"]:
            row[f"{field}_delta"] = 0
    else:
        for field in ["memory_current", *STAT_FIELDS, "memory_events_max", "memory_events_oom", "memory_events_oom_kill"]:
            row[f"{field}_delta"] = delta_value(field, sample.values[field], previous.values[field])

    pgfault_delta = int(row["pgfault_delta"])
    pgmajfault_delta = int(row["pgmajfault_delta"])
    row["major_fault_ratio"] = (pgmajfault_delta / pgfault_delta) if pgfault_delta else 0
    return row


def percentile(values: list[int], pct: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    index = int(round((len(ordered) - 1) * pct))
    index = max(0, min(index, len(ordered) - 1))
    return ordered[index]


def summarize(raw_path: Path, delta_path: Path, summary_path: Path, session_dir: Path, slice_name: str, scopes: list[str]) -> str:
    raw_rows: list[dict[str, str]] = []
    delta_rows: list[dict[str, str]] = []

    if raw_path.exists():
        with raw_path.open("r", encoding="utf-8", newline="") as f:
            raw_rows = list(csv.DictReader(f))
    if delta_path.exists():
        with delta_path.open("r", encoding="utf-8", newline="") as f:
            delta_rows = list(csv.DictReader(f))

    ok_count = sum(1 for row in raw_rows if row.get("status") == "ok")
    final_result = "PASS" if raw_path.exists() and delta_path.exists() and summary_path.parent.exists() and ok_count > 0 else "FAIL"

    lines: list[str] = [
        "# Cgroup memory workload summary",
        "",
        f"- session_dir: `{session_dir}`",
        f"- slice: `{slice_name}`",
        f"- scopes: `{', '.join(scopes)}`",
        f"- raw_csv: `{raw_path}`",
        f"- delta_csv: `{delta_path}`",
        "",
        "说明：该脚本只读取 cgroup v2 的 `memory.current`、`memory.stat`、`memory.events`，不使用 eBPF/cache_ext/smaps/VMA，也不执行预取、驱逐、swap、MGLRU、debugfs 或 page cache 调度动作。",
        "",
        "## Scope summary",
        "",
        "| scope | rows | ok | missing_cgroup | read_error | memory_current_min | memory_current_p50 | memory_current_p90 | memory_current_max | anon_delta_sum | file_delta_sum | pgfault_delta_sum | pgmajfault_delta_sum | workingset_refault_file_delta_sum | workingset_refault_anon_delta_sum | events_max_seen | events_oom_seen | events_oom_kill_seen |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|---|---|",
    ]

    for scope in scopes:
        scope_raw = [row for row in raw_rows if row.get("scope_name") == scope]
        scope_delta = [row for row in delta_rows if row.get("scope_name") == scope]
        ok_rows = [row for row in scope_raw if row.get("status") == "ok"]
        currents = [int(row.get("memory_current") or 0) for row in ok_rows]

        def delta_sum(field: str) -> int:
            total = 0
            for row in scope_delta:
                try:
                    total += int(float(row.get(field, "0") or 0))
                except ValueError:
                    pass
            return total

        events_max_seen = delta_sum("memory_events_max_delta") > 0
        events_oom_seen = delta_sum("memory_events_oom_delta") > 0
        events_oom_kill_seen = delta_sum("memory_events_oom_kill_delta") > 0

        lines.append(
            "| {scope} | {rows} | {ok} | {missing} | {read_error} | {min_v} | {p50} | {p90} | {max_v} | {anon} | {file} | {pgfault} | {pgmajfault} | {refault_file} | {refault_anon} | {events_max} | {events_oom} | {events_oom_kill} |".format(
                scope=scope,
                rows=len(scope_raw),
                ok=len(ok_rows),
                missing=sum(1 for row in scope_raw if row.get("status") == "missing_cgroup"),
                read_error=sum(1 for row in scope_raw if row.get("status") == "read_error"),
                min_v=min(currents) if currents else 0,
                p50=percentile(currents, 0.5),
                p90=percentile(currents, 0.9),
                max_v=max(currents) if currents else 0,
                anon=delta_sum("anon_delta"),
                file=delta_sum("file_delta"),
                pgfault=delta_sum("pgfault_delta"),
                pgmajfault=delta_sum("pgmajfault_delta"),
                refault_file=delta_sum("workingset_refault_file_delta"),
                refault_anon=delta_sum("workingset_refault_anon_delta"),
                events_max="yes" if events_max_seen else "no",
                events_oom="yes" if events_oom_seen else "no",
                events_oom_kill="yes" if events_oom_kill_seen else "no",
            )
        )

    lines.extend(
        [
            "",
            "## Usage note",
            "",
            "`huawei-test.slice` 是用户级 systemd slice，应用级 scope 通常只在 automation 运行期间存在。请与 automation 同时运行，或先启动 automation 后再启动该采集脚本。",
            "",
            f"- final_result: {final_result}",
        ]
    )
    summary_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return final_result


def run(args: argparse.Namespace) -> dict[str, Any]:
    session_dir = Path(args.session_dir)
    model_dir = session_dir / "model"
    review_dir = session_dir / "review"
    model_dir.mkdir(parents=True, exist_ok=True)
    review_dir.mkdir(parents=True, exist_ok=True)

    session_id = session_dir.name
    scopes = [scope.strip() for scope in args.scopes.split(",") if scope.strip()]
    raw_path = model_dir / "cgroup_memory_workload_1s.csv"
    delta_path = model_dir / "cgroup_memory_workload_delta_1s.csv"
    summary_path = review_dir / "cgroup_memory_workload_summary.md"

    previous_by_scope: dict[str, ScopeSample] = {}
    started = time.monotonic()
    stop_at = started + args.duration_s if args.duration_s and args.duration_s > 0 else None
    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    with raw_path.open("w", encoding="utf-8", newline="") as raw_f, delta_path.open("w", encoding="utf-8", newline="") as delta_f:
        raw_writer = csv.DictWriter(raw_f, fieldnames=RAW_FIELDS)
        delta_writer = csv.DictWriter(delta_f, fieldnames=DELTA_FIELDS)
        raw_writer.writeheader()
        delta_writer.writeheader()
        raw_f.flush()
        delta_f.flush()

        try:
            while not STOP_REQUESTED:
                loop_start = time.monotonic()
                timestamp = dt.datetime.now().isoformat(timespec="seconds")
                _control_group, parent_path, parent_error = resolve_user_slice_path(args.slice)

                for scope in scopes:
                    sample = sample_scope(session_id, timestamp, parent_path, scope, parent_error)
                    raw_writer.writerow(raw_row(sample))
                    delta_writer.writerow(delta_row(sample, previous_by_scope.get(scope)))
                    if sample.status == "ok":
                        previous_by_scope[scope] = sample

                raw_f.flush()
                delta_f.flush()

                if stop_at is not None and time.monotonic() >= stop_at:
                    break

                sleep_s = max(0.0, args.interval_s - (time.monotonic() - loop_start))
                if stop_at is not None:
                    sleep_s = min(sleep_s, max(0.0, stop_at - time.monotonic()))
                if sleep_s <= 0 and args.interval_s > 0:
                    continue
                time.sleep(sleep_s)
        except KeyboardInterrupt:
            pass

    final_result = summarize(raw_path, delta_path, summary_path, session_dir, args.slice, scopes)
    return {
        "final_result": final_result,
        "session_dir": str(session_dir),
        "raw_csv": str(raw_path),
        "delta_csv": str(delta_path),
        "summary": str(summary_path),
    }


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.interval_s <= 0:
        print("--interval-s must be > 0", file=sys.stderr)
        return 2
    result = run(args)
    print(f"final_result={result['final_result']}")
    print(f"session_dir={result['session_dir']}")
    print(f"raw_csv={result['raw_csv']}")
    print(f"delta_csv={result['delta_csv']}")
    print(f"summary={result['summary']}")
    return 0 if result["final_result"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
