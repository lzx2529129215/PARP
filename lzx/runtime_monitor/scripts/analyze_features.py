#!/usr/bin/env python3
"""Print a compact summary of features_1s.csv."""

from __future__ import annotations

import csv
import sys
from pathlib import Path


def main() -> int:
    path = Path(sys.argv[1] if len(sys.argv) > 1 else "output/features_1s.csv")
    if not path.exists():
        print(f"missing file: {path}", file=sys.stderr)
        return 2
    with path.open("r", encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        print("no rows")
        return 0
    fields = [
        "wps_open_cnt_1s",
        "wps_read_bytes_1s",
        "wps_write_bytes_1s",
        "wps_mmap_cnt_1s",
        "wps_fsync_cnt_1s",
        "wps_rename_cnt_1s",
        "wps_docx_open_cnt_1s",
        "wps_tmp_open_cnt_1s",
        "wps_so_open_cnt_1s",
        "wps_pdf_open_cnt_1s",
        "global_pgmajfault_delta",
        "global_pswpin_delta",
        "global_pswpout_delta",
    ]
    print(f"rows: {len(rows)}")
    for field in fields:
        total = sum(int(float(row.get(field) or 0)) for row in rows)
        print(f"{field}: {total}")
    labels = sorted({row.get("manual_label", "") for row in rows if row.get("manual_label")})
    if labels:
        print("labels:", ", ".join(labels))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

