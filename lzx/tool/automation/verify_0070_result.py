#!/usr/bin/env python3
"""Verify the PDF fixture, visible navigation, window states, and trace for 0070."""

from __future__ import annotations

import argparse
import csv
import itertools
import re
import subprocess
from pathlib import Path


def pdf_page_count(path: Path) -> int:
    result = subprocess.run(
        ["pdfinfo", str(path)],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    match = re.search(r"^Pages:\s+(\d+)\s*$", result.stdout, re.MULTILINE)
    if match is None:
        raise RuntimeError("pdfinfo did not report a page count")
    return int(match.group(1))


def change_ratio(first: Path, second: Path) -> float:
    left = first.read_bytes()
    right = second.read_bytes()
    length = max(len(left), len(right), 1)
    changed = sum(
        a != b
        for a, b in itertools.zip_longest(left, right, fillvalue=-1)
    )
    return changed / length


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pdf", required=True, type=Path)
    parser.add_argument("--trace", required=True, type=Path)
    parser.add_argument("--first-page", required=True, type=Path)
    parser.add_argument("--last-page", required=True, type=Path)
    parser.add_argument("--scrolled-down", required=True, type=Path)
    parser.add_argument("--scrolled-up", required=True, type=Path)
    parser.add_argument("--minimized", required=True, type=Path)
    parser.add_argument("--maximized", required=True, type=Path)
    parser.add_argument("--tab-closed", required=True, type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()

    required = (
        args.pdf,
        args.trace,
        args.first_page,
        args.last_page,
        args.scrolled_down,
        args.scrolled_up,
        args.minimized,
        args.maximized,
        args.tab_closed,
    )
    for path in required:
        if not path.is_file() or path.stat().st_size == 0:
            raise FileNotFoundError(f"Required verification artifact is missing: {path}")

    pages = pdf_page_count(args.pdf)
    first_last_change = change_ratio(args.first_page, args.last_page)
    scroll_change = change_ratio(args.scrolled_down, args.scrolled_up)
    window_state_change = change_ratio(args.minimized, args.maximized)
    tab_close_change = change_ratio(args.maximized, args.tab_closed)

    with args.trace.open(encoding="utf-8", newline="") as source:
        completed = [row for row in csv.DictReader(source) if row["phase"] == "end"]
    failures = [row for row in completed if row["status"] != "success"]
    labels = {row["label"] for row in completed}

    required_labels = {
        "WPS_PDF_S1_LAUNCH",
        "WPS_PDF_S2_OPEN",
        "WPS_PDF_S3_SCROLL_DOWN_3",
        "WPS_PDF_S3_SCROLL_UP_3",
        "WPS_PDF_S4_LAST_PAGE",
        "WPS_PDF_S5_FIRST_PAGE",
        "WPS_PDF_S6_MINIMIZE",
        "WPS_PDF_S7_MAXIMIZE",
        "WPS_PDF_S7_WAIT_10_SECONDS",
        "WPS_PDF_S8_TAB_CLOSE",
        "WPS_CLOSE",
    }
    checks = {
        "PDF_HAS_MULTIPLE_PAGES": pages >= 5,
        "NO_FAILED_ACTIONS": not failures,
        "ALL_S1_TO_S9_ACTIONS_COMPLETED": required_labels.issubset(labels),
        "THREE_DOWN_AND_UP_SCROLLS_VISIBLY_DIFFER": scroll_change >= 0.01,
        "FIRST_AND_LAST_PAGE_VISIBLY_DIFFER": first_last_change >= 0.02,
        "MINIMIZE_AND_MAXIMIZE_VISIBLY_DIFFER": window_state_change >= 0.02,
        "PDF_TAB_CLOSE_VISIBLY_CHANGED_UI": tab_close_change >= 0.02,
    }

    lines = [
        f"PDF_PAGE_COUNT={pages}",
        f"SCROLL_CHANGE_RATIO={scroll_change:.6f}",
        f"FIRST_LAST_CHANGE_RATIO={first_last_change:.6f}",
        f"WINDOW_STATE_CHANGE_RATIO={window_state_change:.6f}",
        f"TAB_CLOSE_CHANGE_RATIO={tab_close_change:.6f}",
        f"COMPLETED_ACTIONS={len(completed)}",
    ]
    lines.extend(f"{name}={str(passed).lower()}" for name, passed in checks.items())
    print("\n".join(lines))
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
