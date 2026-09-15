#!/usr/bin/env python3
"""在自动化开始前计算 WPS 已知输入文件的逻辑 ID 与内容哈希。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sample_root", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    root = args.sample_root.resolve()
    catalog: dict[str, dict[str, str | int]] = {}
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        normalized = os.path.realpath(path)
        relative = path.relative_to(root).as_posix()
        values = path.stat()
        catalog[normalized] = {
            "logical_id": f"wps-input:{relative}",
            "content_sha256": sha256_file(path),
            "size_bytes": int(values.st_size),
        }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(catalog, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.chmod(args.output, 0o600)
    print(f"cataloged {len(catalog)} fixture files: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
