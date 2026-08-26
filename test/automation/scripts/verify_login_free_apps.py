#!/usr/bin/env python3
"""Verify that every login-free application was controlled successfully."""

# lzx-note: Convert GUI action trace into a strict, machine-readable verdict.
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path


APPS = ("CALENDAR", "RHYTHMBOX", "IMAGE_VIEWER", "SHOTWELL", "SYSTEM_MONITOR", "SOLITAIRE")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("trace", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    with args.trace.open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))

    failed = [row for row in rows if row.get("phase") == "end" and row.get("status") == "failed"]
    done = any(row.get("event_type") == "SCENARIO_DONE" and row.get("status") == "success" for row in rows)
    apps: dict[str, dict[str, object]] = {}
    for app in APPS:
        launch = any(row.get("app_key") == app and row.get("event_type") == "APP_LAUNCH" and row.get("status") == "success" for row in rows)
        focus_count = sum(row.get("app_key") == app and row.get("event_type") == "APP_FOCUS" and row.get("status") == "success" for row in rows)
        verified = any(row.get("app_key") == app and row.get("action") == "verify_foreground" and row.get("phase") == "end" and row.get("status") == "success" for row in rows)
        apps[app] = {"launch": launch, "focus_count": focus_count, "foreground_verified": verified, "pass": launch and focus_count > 0 and verified}

    passed = done and not failed and all(bool(item["pass"]) for item in apps.values())
    result = {
        "status": "PASS" if passed else "FAIL",
        "scenario_done": done,
        "failed_action_count": len(failed),
        "apps": apps,
        "trace": str(args.trace.resolve()),
    }
    payload = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")
    print(payload, end="")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
