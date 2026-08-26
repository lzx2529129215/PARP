#!/usr/bin/env python3
"""验证本地素材 manifest；不创建大文件，也不读取敏感内容。"""

from __future__ import annotations

import argparse
import csv
import json
import os
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description="自动化素材清单验证")
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    fields = ["asset", "exists", "file_type", "size_bytes", "readable", "writable_output_dir", "safe_size", "notes"]
    rows = []
    for name, value in manifest.items():
        raw_path = value.get("path", "") if isinstance(value, dict) else str(value)
        notes = value.get("notes", "") if isinstance(value, dict) else ""
        path = Path(raw_path).expanduser() if raw_path else None
        exists = bool(path and path.exists())
        is_dir = bool(exists and path and path.is_dir())
        size = path.stat().st_size if exists and path and path.is_file() else 0
        rows.append({"asset": name, "exists": str(exists).lower(), "file_type": "directory" if is_dir else (path.suffix if exists and path else ""), "size_bytes": size, "readable": str(bool(exists and path and os.access(path, os.R_OK))).lower(), "writable_output_dir": str(bool(is_dir and path and os.access(path, os.W_OK))).lower(), "safe_size": str(size <= 100 * 1024 * 1024).lower(), "notes": notes})
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader(); writer.writerows(rows)
    print(f"asset_validation={args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
