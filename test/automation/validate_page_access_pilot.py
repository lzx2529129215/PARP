#!/usr/bin/env python3
"""验证 WPS page-access-window 试采门槛；失败时阻止正式采集。"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import sys
from pathlib import Path
from typing import Any


SCENARIOS = ("0010", "0020", "0030", "0040", "0050", "0060", "0070")


def percentile(values: list[float], quantile: float) -> float:
    if not values:
        return float("inf")
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * quantile) - 1)]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def validate_session(session_dir: Path) -> dict[str, Any]:
    errors: list[str] = []
    dataset = session_dir / "dataset"
    partials = sorted(str(path.relative_to(session_dir)) for path in session_dir.rglob("*.partial"))
    if partials:
        errors.append("unfinished partial files: " + ",".join(partials))
    manifest_path = dataset / "manifest.json"
    manifest: dict[str, Any] = {}
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        errors.append(f"manifest unreadable: {exc}")
    if manifest.get("capture_status") != "COMPLETE":
        errors.append("capture_status is not COMPLETE")
    for name, expected in manifest.get("sha256", {}).items():
        candidate = dataset / name
        if not candidate.exists():
            candidate = session_dir / name
        if not candidate.exists():
            errors.append(f"checksum target missing: {name}")
        elif sha256_file(candidate) != str(expected):
            errors.append(f"checksum mismatch: {name}")

    rows: list[dict[str, str]] = []
    try:
        with (dataset / "window_summary.csv").open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
    except OSError as exc:
        errors.append(f"window summary unreadable: {exc}")
    # 捕获停止时按协议保留不足一秒的末窗并标 INVALID；它用于审计，不进入
    # “完整 1 秒窗口有效率”分母。
    eligible = [
        row for row in rows
        if "INCOMPLETE_FINAL_WINDOW" not in row.get("invalid_reason", "")
    ]
    valid_rows = [row for row in eligible if row.get("valid", "").lower() == "true"]
    valid_ratio = len(valid_rows) / len(eligible) if eligible else 0.0
    if not eligible:
        errors.append("no complete one-second window")
    if valid_ratio < 0.99:
        errors.append(f"valid complete-window ratio {valid_ratio:.4f} < 0.99")
    overflows = sum(int(row.get("bpf_map_overflows", 0) or 0) for row in rows)
    if overflows or int(manifest.get("result", {}).get("bpf_map_overflows", 0) or 0):
        errors.append(f"BPF map overflow count is {overflows}")
    source_delta = manifest.get("source_integrity", {}).get("counter_delta", {})
    for counter in ("perf_lost", "file_perf_lost", "cache_perf_lost"):
        value = int(source_delta.get(counter, 0) or 0)
        if value:
            errors.append(f"source integrity {counter} is {value}")
    latencies = [
        float(row.get("reset_latency_ms", 0) or 0)
        + float(row.get("scan_latency_ms", 0) or 0)
        for row in eligible
    ]
    p99_ms = percentile(latencies, 0.99)
    if p99_ms > 200.0:
        errors.append(f"reset+scan p99 {p99_ms:.3f} ms > 200 ms")

    source_path = session_dir / "model" / "file_event_source.csv"
    bad_statuses: list[str] = []
    try:
        with source_path.open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                status = str(row.get("status", "")).upper()
                if any(token in status for token in (
                    "LOST", "OVERFLOW", "RESTART", "FAILED", "STALE", "GAP"
                )):
                    bad_statuses.append(status)
    except OSError as exc:
        errors.append(f"file source audit unreadable: {exc}")
    if bad_statuses:
        errors.append("file source integrity status: " + ",".join(sorted(set(bad_statuses))))

    trace_path = session_dir / "automation_trace.csv"
    trace_monotonic: list[int] = []
    trace_events: set[str] = set()
    try:
        with trace_path.open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                value = int(row.get("monotonic_ns", 0) or 0)
                if value:
                    trace_monotonic.append(value)
                trace_events.add(str(row.get("event_type", "")))
    except (OSError, ValueError) as exc:
        errors.append(f"automation trace unreadable: {exc}")
    if not {"SCENARIO_START", "SCENARIO_DONE"}.issubset(trace_events):
        errors.append("automation trace does not contain a completed scenario")
    clock = manifest.get("clock", {})
    capture_start = int(clock.get("start_monotonic_ns", 0) or 0)
    capture_end = int(clock.get("end_monotonic_ns", 0) or 0)
    if not trace_monotonic or min(trace_monotonic) < capture_start or max(trace_monotonic) > capture_end:
        errors.append("automation monotonic timestamps are outside capture clock bounds")

    return {
        "session": str(session_dir),
        "complete_windows": len(eligible),
        "valid_complete_windows": len(valid_rows),
        "valid_ratio": valid_ratio,
        "reset_scan_p99_ms": p99_ms,
        "bpf_map_overflows": overflows,
        "errors": errors,
        "valid": not errors,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pilot_root", type=Path, nargs="?")
    parser.add_argument(
        "--session",
        type=Path,
        help="只校验一个已完成 session，供安全续跑逻辑使用",
    )
    parser.add_argument("--expected-repetitions", type=int, default=3)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    if args.session is not None:
        result = validate_session(args.session)
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if result["valid"] else 1
    if args.pilot_root is None:
        parser.error("pilot_root is required unless --session is used")
    results: list[dict[str, Any]] = []
    errors: list[str] = []
    for scenario in SCENARIOS:
        sessions = sorted((args.pilot_root / scenario).glob("*"))
        sessions = [path for path in sessions if path.is_dir()]
        if len(sessions) != args.expected_repetitions:
            errors.append(
                f"scenario {scenario}: expected {args.expected_repetitions} repetitions, got {len(sessions)}"
            )
        results.extend(validate_session(path) for path in sessions)
    errors.extend(
        f"{row['session']}: {message}"
        for row in results for message in row["errors"]
    )
    report = {
        "schema": "parp-page-access-pilot-validation-v1",
        "pilot_root": str(args.pilot_root.resolve()),
        "valid": not errors,
        "sessions": results,
        "errors": errors,
    }
    output = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(output, encoding="utf-8")
    print(output, end="")
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
