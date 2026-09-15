#!/usr/bin/env python3
"""只读校验/解码 page-access-window 二进制数据集。"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Iterator

SERVICE_ROOT = Path(__file__).resolve().parents[2]
if str(SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_ROOT))

from runtime_monitor.core.page_access_window import read_framed_binary


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def expand_window(record: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """按需展开无损 range；正式采集路径永远不会调用此函数。"""
    common = {
        key: value for key, value in record.items() if key != "page_ranges"
    }
    for item in record.get("page_ranges", []):
        count = int(item.get("page_count", 0) or 0)
        for delta in range(count):
            yield {
                **common,
                "file_catalog_id": int(item["file_catalog_id"]),
                "page_index": int(item["page_index_start"]) + delta,
                "pfn": int(item["pfn_start"]) + delta * int(item.get("pfn_stride", 1)),
                "source_mask": item.get("source_mask", []),
                "source_mask_raw": int(item.get("source_mask_raw", 0)),
                "attribution_mask": item.get("attribution_mask", []),
                "attribution_mask_raw": int(item.get("attribution_mask_raw", 0)),
            }


def verify_manifest(dataset_dir: Path) -> list[str]:
    manifest_path = dataset_dir / "manifest.json"
    if not manifest_path.exists():
        return ["manifest.json missing"]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    errors: list[str] = []
    for name, expected in manifest.get("sha256", {}).items():
        candidate = dataset_dir / name
        if not candidate.exists():
            # automation_trace.csv 位于 dataset 的父目录。
            candidate = dataset_dir.parent / name
        if not candidate.exists():
            errors.append(f"checksum target missing: {name}")
        elif sha256_file(candidate) != str(expected):
            errors.append(f"checksum mismatch: {name}")
    return errors


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dataset_dir", type=Path)
    parser.add_argument(
        "--kind", choices=["windows", "lifecycle", "both"], default="both"
    )
    parser.add_argument(
        "--jsonl", action="store_true", help="把帧以 JSONL 写到 stdout"
    )
    parser.add_argument(
        "--expand-ranges", action="store_true",
        help="与 --jsonl 一起把窗口 range 展开为逐页记录",
    )
    parser.add_argument(
        "--recover-truncated", action="store_true",
        help="仅恢复 .partial 末尾之前 CRC 完整的帧",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    dataset_dir = args.dataset_dir.resolve()
    selections = []
    if args.kind in {"windows", "both"}:
        selections.append(("windows", "page_access_windows.bin"))
    if args.kind in {"lifecycle", "both"}:
        selections.append(("lifecycle", "page_lifecycle.bin"))
    result: dict[str, Any] = {"dataset_dir": str(dataset_dir), "files": {}}
    errors = verify_manifest(dataset_dir)
    for kind, name in selections:
        path = dataset_dir / name
        if not path.exists() and args.recover_truncated:
            path = dataset_dir / f"{name}.partial"
        try:
            metadata, records = read_framed_binary(
                path, recover_truncated=args.recover_truncated
            )
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            errors.append(f"{name}: {exc}")
            continue
        result["files"][name] = {
            "metadata": metadata,
            "frame_count": len(records),
            "sha256": sha256_file(path),
        }
        if args.jsonl:
            for record in records:
                output_rows = (
                    expand_window(record)
                    if kind == "windows" and args.expand_ranges
                    else (record,)
                )
                for row in output_rows:
                    print(json.dumps(
                        {"record_kind": kind, **row},
                        ensure_ascii=False, sort_keys=True,
                    ))
    result["valid"] = not errors
    result["errors"] = errors
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True), file=sys.stderr if args.jsonl else sys.stdout)
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
