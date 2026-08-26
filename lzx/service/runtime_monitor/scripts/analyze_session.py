#!/usr/bin/env python3
"""Print a compact analysis report for one runtime monitor session directory."""

from __future__ import annotations

import argparse
import csv
from collections import Counter, defaultdict
from pathlib import Path
from statistics import mean


FILES = [
    "features_1s.csv",
    "features_1s.labeled.csv",
    "app_features_1s.csv",
    "app_features_1s.labeled.csv",
    "automation_trace.csv",
    "labels.csv",
]


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def as_int(value: str) -> int:
    try:
        return int(float(value or 0))
    except ValueError:
        return 0


def print_counts(title: str, counts: Counter[str]) -> None:
    print(title)
    if not counts:
        print("  <none>")
        return
    for key, value in counts.most_common():
        print(f"  {key or '<empty>'}: {value}")


def open_apps_changes(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    changes: list[dict[str, str]] = []
    previous: tuple[str, str] | None = None
    for row in rows:
        current = (row.get("open_apps", ""), row.get("closed_apps", ""))
        if current != previous:
            changes.append(row)
            previous = current
    return changes


def group_by_app(rows: list[dict[str, str]]) -> dict[str, list[dict[str, str]]]:
    groups: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        groups[row.get("app_id", "")].append(row)
    return groups


def average(values: list[int]) -> float:
    return mean(values) if values else 0.0


def sum_field(rows: list[dict[str, str]], field: str) -> int:
    return sum(as_int(row.get(field, "")) for row in rows)


def resource_summary(rows: list[dict[str, str]]) -> str:
    if not rows:
        return "rows=0"
    return (
        f"rows={len(rows)} "
        f"read_sum={sum_field(rows, 'read_bytes_1s')} "
        f"write_sum={sum_field(rows, 'write_bytes_1s')} "
        f"mem_avg={average([as_int(row.get('mem_current', '')) for row in rows]):.1f} "
        f"mem_max={max(as_int(row.get('mem_current', '')) for row in rows)}"
    )


def pivot_app_state(rows: list[dict[str, str]]) -> dict[str, Counter[str]]:
    pivot: dict[str, Counter[str]] = defaultdict(Counter)
    for row in rows:
        pivot[row.get("app_id", "")][row.get("state_label", "")] += 1
    return pivot


def check_switch_foreground(features_labeled: list[dict[str, str]], state_label: str, expected_app: str) -> tuple[int, int]:
    rows = [row for row in features_labeled if row.get("state_label") == state_label]
    ok = sum(1 for row in rows if row.get("foreground_app") == expected_app)
    return ok, len(rows)


def check_close_removed(features: list[dict[str, str]], app_id: str) -> str:
    close_seen = False
    for row in features:
        closed = set(filter(None, row.get("closed_apps", "").split("|")))
        open_apps = set(filter(None, row.get("open_apps", "").split("|")))
        if app_id in closed:
            close_seen = True
            if app_id in open_apps:
                return "FAIL"
    return "OK" if close_seen else "NO_CLOSE_SEEN"


def analyze(session_dir: Path) -> None:
    data = {name: read_csv(session_dir / name) for name in FILES}

    print(f"session_dir={session_dir}")
    print("\nFILES")
    for name in FILES:
        path = session_dir / name
        print(f"  {name}: exists={path.exists()} rows={len(data[name])}")

    features = data["features_1s.csv"]
    features_labeled = data["features_1s.labeled.csv"]
    app_features = data["app_features_1s.csv"]
    app_labeled = data["app_features_1s.labeled.csv"]

    print_counts("\nfeatures_1s foreground_app", Counter(row.get("foreground_app", "") for row in features))

    print("\nfeatures_1s open_apps changes")
    for row in open_apps_changes(features):
        print(
            f"  {row.get('timestamp')} foreground={row.get('foreground_app')} "
            f"open={row.get('open_apps')} closed={row.get('closed_apps')}"
        )

    print_counts("\nfeatures_1s.labeled manual_label", Counter(row.get("manual_label", "") for row in features_labeled))
    print_counts("\nfeatures_1s.labeled state_label", Counter(row.get("state_label", "") for row in features_labeled))
    print_counts("\napp_features_1s app_id", Counter(row.get("app_id", "") for row in app_features))

    print("\napp_features_1s.labeled app_id x state_label")
    for app_id, counts in sorted(pivot_app_state(app_labeled).items()):
        pairs = ", ".join(f"{label or '<empty>'}={count}" for label, count in counts.most_common())
        print(f"  {app_id}: {pairs}")

    print("\nmem_current by app")
    for app_id, rows in sorted(group_by_app(app_features).items()):
        mem = [as_int(row.get("mem_current", "")) for row in rows]
        print(f"  {app_id}: avg={average(mem):.1f} max={max(mem) if mem else 0}")

    print("\nmem_current foreground/background by app")
    for app_id, rows in sorted(group_by_app(app_labeled).items()):
        fg = [as_int(row.get("mem_current", "")) for row in rows if row.get("is_foreground") == "1"]
        bg = [as_int(row.get("mem_current", "")) for row in rows if row.get("is_foreground") != "1"]
        print(f"  {app_id}: foreground_avg={average(fg):.1f} background_avg={average(bg):.1f}")

    print("\nread/write totals by app")
    for app_id, rows in sorted(group_by_app(app_features).items()):
        print(
            f"  {app_id}: read_sum={sum_field(rows, 'read_bytes_1s')} "
            f"write_sum={sum_field(rows, 'write_bytes_1s')}"
        )

    qq_rows = [row for row in app_labeled if row.get("state_label") == "APP_SWITCH_QQ" and row.get("app_id") == "QQ"]
    files_rows = [
        row for row in app_labeled if row.get("state_label") == "APP_SWITCH_FILES" and row.get("app_id") == "FILES"
    ]
    print(f"\nAPP_SWITCH_QQ QQ resources: {resource_summary(qq_rows)}")
    print(f"APP_SWITCH_FILES FILES resources: {resource_summary(files_rows)}")

    qq_ok, qq_total = check_switch_foreground(features_labeled, "APP_SWITCH_QQ", "QQ")
    files_ok, files_total = check_switch_foreground(features_labeled, "APP_SWITCH_FILES", "FILES")
    print("\nforeground checks")
    print(f"  APP_SWITCH_QQ foreground_app=QQ: {qq_ok}/{qq_total}")
    print(f"  APP_SWITCH_FILES foreground_app=FILES: {files_ok}/{files_total}")

    print("\nclose removal checks")
    for app_id in ("FILES", "QQ", "WPS"):
        print(f"  {app_id}: {check_close_removed(features, app_id)}")

    print("\nNo prefetch, eviction, swap, MGLRU, or memory scheduling action is performed by this analysis.")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Analyze runtime monitor session CSV outputs.")
    parser.add_argument("--session-dir", required=True, type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    analyze(args.session_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
