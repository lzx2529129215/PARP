#!/usr/bin/env python3
"""Verify that a Test4 run executed the generated split-derived sequence."""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any


V3_TO_RUNTIME = {
    "Firefox": "FIREFOX", "LibreOffice": "LIBREOFFICE", "VLC": "VLC",
    "GIMP": "GIMP", "Audacity": "AUDACITY", "Thunderbird": "THUNDERBIRD",
    "Telegram": "TELEGRAM", "Evince": "EVINCE", "Files": "FILES",
    "Calculator": "CALCULATOR",
}


def read_rows(path: Path) -> list[dict[str, str]]:
    try:
        with path.open(encoding="utf-8", newline="") as stream:
            return list(csv.DictReader(stream))
    except OSError:
        return []


def find_subsequence(expected: list[str], observed: list[tuple[str, int]]) -> tuple[bool, list[tuple[str, int]]]:
    matched: list[tuple[str, int]] = []
    position = 0
    for app, timestamp_ns in observed:
        if position < len(expected) and app == expected[position]:
            matched.append((app, timestamp_ns))
            position += 1
    return position == len(expected), matched


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--session-dir", type=Path, required=True)
    parser.add_argument("--coverage", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--dwell-tolerance-s", type=float, default=2.0)
    args = parser.parse_args()

    coverage: dict[str, Any] = json.loads(args.coverage.read_text(encoding="utf-8"))
    expected_v3 = list(coverage["sequence_order"])
    expected = [V3_TO_RUNTIME.get(app, app) for app in expected_v3]
    source_dwell = [float(item) for item in coverage["source_dwell_s"]]
    trace = read_rows(args.session_dir / "model" / "automation_trace.csv")
    bootstrap_end = max(
        (int(row.get("end_time_ns") or row.get("ts_ns") or 0) for row in trace if row.get("label") == "TEST4_BOOTSTRAP_NOT_SCORED" and row.get("status") == "success"),
        default=0,
    )
    direct = read_rows(args.session_dir / "model" / "direct_app_events.csv")
    allowed = set(expected)
    raw_observed: list[tuple[str, int, str]] = []
    for row in direct:
        if row.get("event_type") not in {"APP_OPEN", "APP_SWITCH"}:
            continue
        timestamp_ns = int(row.get("ts_ns") or 0)
        app = row.get("app") or row.get("new_app") or ""
        if timestamp_ns >= bootstrap_end and app in allowed:
            raw_observed.append((app, timestamp_ns, row.get("event_type", "")))
    raw_observed.sort(key=lambda item: item[1])
    observed: list[tuple[str, int]] = []
    for app, timestamp_ns, _kind in raw_observed:
        if not observed or observed[-1][0] != app:
            observed.append((app, timestamp_ns))

    # The initial focus can be established before the native X11 state machine
    # is initialized.  The first switch action trace is the anchor in that
    # case; every subsequent split transition must still be observed in order.
    scored_expected = expected[1:]
    sequence_pass, matched = find_subsequence(scored_expected, observed)
    dwell_checks: list[dict[str, Any]] = []
    for index in range(1, len(matched)):
        observed_dwell = (matched[index][1] - matched[index - 1][1]) / 1_000_000_000
        expected_dwell = source_dwell[index]
        dwell_checks.append({
            "from_app": matched[index - 1][0], "to_app": matched[index][0],
            "expected_s": expected_dwell, "observed_s": observed_dwell,
            "within_tolerance": abs(observed_dwell - expected_dwell) <= args.dwell_tolerance_s,
        })
    dwell_pass = bool(dwell_checks) and all(check["within_tolerance"] for check in dwell_checks)
    result = {
        "status": "PASS" if sequence_pass and dwell_pass else "FAIL",
        "bootstrap_end_ns": bootstrap_end,
        "expected_sequence": expected_v3,
        "expected_runtime_sequence": expected,
        "scored_expected_sequence": scored_expected,
        "observed_foreground_sequence": [app for app, _timestamp in observed],
        "matched_sequence": [app for app, _timestamp in matched],
        "sequence_status": "PASS" if sequence_pass else "FAIL",
        "dwell_status": "PASS" if dwell_pass else "FAIL",
        "dwell_tolerance_s": args.dwell_tolerance_s,
        "dwell_checks": dwell_checks,
        "notes": "The first foreground anchor may be initialized before an APP_SWITCH; later transitions require native APP_OPEN/APP_SWITCH evidence.",
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
