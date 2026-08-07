#!/usr/bin/env python3
"""Verify that the nested test1 run observed every scripted lifecycle action."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
from pathlib import Path
from typing import Any


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def iso_ns(value: str) -> int:
    if not value:
        return 0
    try:
        parsed = dt.datetime.fromisoformat(value)
        return int(parsed.timestamp() * 1_000_000_000)
    except ValueError:
        return 0


def row_ns(row: dict[str, str]) -> int:
    try:
        return int(row.get("ts_ns", "0") or 0)
    except ValueError:
        return iso_ns(row.get("ts_iso", "") or row.get("timestamp", ""))


def nearby(rows: list[dict[str, str]], ts_ns: int, *, seconds: float = 5.0) -> list[dict[str, str]]:
    margin = int(seconds * 1_000_000_000)
    return [row for row in rows if abs(row_ns(row) - ts_ns) <= margin]


def in_action_window(
    rows: list[dict[str, str]], start_ns: int, end_ns: int, *, before_s: float = 1.0, after_s: float = 5.0
) -> list[dict[str, str]]:
    lower = start_ns - int(before_s * 1_000_000_000)
    upper = end_ns + int(after_s * 1_000_000_000)
    return [row for row in rows if lower <= row_ns(row) <= upper]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--session-dir", type=Path, required=True)
    args = parser.parse_args()
    model = args.session_dir / "model"
    review = args.session_dir / "review"

    trace = read_csv(model / "automation_trace.csv")
    process = read_csv(model / "process_events.csv")
    foreground = read_csv(model / "foreground_events.csv")
    lifecycle = read_csv(model / "app_lifecycle_events.csv")

    actions = [
        row for row in trace
        if row.get("event_type") == "OP_DONE" and row.get("status") == "success"
    ]
    starts = {
        (row.get("step_id", ""), row.get("label", "")): row
        for row in trace
        if row.get("event_type") == "OP_START"
    }
    pid_to_app = {
        item.get("pid", ""): item.get("app", "")
        for item in process
        if item.get("pid") and item.get("app")
    }
    checks: list[dict[str, Any]] = []

    for row in actions:
        action = row.get("action", "")
        app = row.get("app", "")
        label = row.get("label", "")
        if not app or app == "UNKNOWN":
            continue
        end_ns = iso_ns(row.get("ts_iso", ""))
        start_row = starts.get((row.get("step_id", ""), label), row)
        start_ns = iso_ns(start_row.get("ts_iso", ""))
        # Desktop applications can create their first mapped window well
        # after the launcher command returns (GIMP is a representative case).
        # This is still an event-driven observation, not a polling fallback.
        launch_after_s = 20.0 if action in {"launch", "shell"} else 5.0
        window = in_action_window(lifecycle, start_ns, end_ns, after_s=launch_after_s)
        process_window = in_action_window(process, start_ns, end_ns, after_s=launch_after_s)
        foreground_window = in_action_window(foreground, start_ns, end_ns)

        if action in {"launch", "shell"}:
            observed = any(item.get("event_type") == "APP_OPEN" and item.get("app") == app for item in window)
            process_start = any(item.get("event_type") == "PROCESS_START" and item.get("app") == app for item in process_window)
            checks.append({"label": label, "kind": "launch", "app": app, "observed": observed and process_start,
                           "app_open": observed, "process_start": process_start})
        elif action in {"switch", "verify_foreground"} and "SWITCH" in label:
            observed = any(item.get("event_type") == "APP_SWITCH" and item.get("new_app") == app for item in foreground_window)
            checks.append({"label": label, "kind": "switch", "app": app, "observed": observed})
        elif action == "window_state":
            state = "MINIMIZE" if "MINIMIZE" in label.upper() else "RESTORE"
            observed = any(
                item.get("event_type") == f"APP_{state}"
                and (
                    item.get("foreground_app") == app
                    or item.get("old_app") == app
                    or item.get("new_app") == app
                    or pid_to_app.get(item.get("pid", "")) == app
                )
                for item in foreground_window
            )
            checks.append({"label": label, "kind": state.lower(), "app": app, "observed": observed})
        elif action == "close":
            observed = any(item.get("event_type") == "APP_CLOSE" and item.get("app") == app for item in window)
            checks.append({"label": label, "kind": "close", "app": app, "observed": observed})

    missing = [item for item in checks if not item["observed"]]
    counts: dict[str, int] = {}
    for row in process + foreground + lifecycle:
        event_type = row.get("event_type", "")
        counts[event_type] = counts.get(event_type, 0) + 1

    result = {
        "session_dir": str(args.session_dir),
        "automation_actions_checked": len(checks),
        "observed_actions": len(checks) - len(missing),
        "missing_actions": missing,
        "event_counts": counts,
        "status": "PASS" if not missing else "FAIL",
    }
    review.mkdir(parents=True, exist_ok=True)
    (review / "event_coverage.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if not missing else 1


if __name__ == "__main__":
    raise SystemExit(main())
