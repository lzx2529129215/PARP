#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import hashlib
import random
import time
import zlib
from pathlib import Path


def create(path: Path, size_gb: float) -> None:
    size = int(size_gb * 1024**3)
    path.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    generator = random.Random(2024)
    chunk_size = 1024 * 1024
    remaining = size
    with path.open("wb") as stream:
        while remaining:
            data = generator.randbytes(min(chunk_size, remaining))
            stream.write(data)
            digest.update(data)
            remaining -= len(data)
        stream.flush()
        os.fsync(stream.fileno())
    metadata = {
        "format": 1,
        "generator": "python-random-seed-2024",
        "fully_written": True,
        "bytes": size,
        "sha256": digest.hexdigest(),
    }
    metadata_path(path).write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"event": "created", "path": str(path), **metadata}))


def metadata_path(path: Path) -> Path:
    return path.with_name(path.name + ".meta.json")


def verify(path: Path, size_gb: float) -> bool:
    expected_size = int(size_gb * 1024**3)
    try:
        metadata = json.loads(metadata_path(path).read_text(encoding="utf-8"))
        sample_nonzero = False
        with path.open("rb", buffering=0) as stream:
            for offset in (0, max(0, expected_size // 2 - 2048), max(0, expected_size - 4096)):
                stream.seek(offset)
                if any(stream.read(min(4096, expected_size - offset))):
                    sample_nonzero = True
        valid = (
            path.stat().st_size == expected_size
            and metadata.get("bytes") == expected_size
            and metadata.get("fully_written") is True
            and metadata.get("generator") == "python-random-seed-2024"
            and isinstance(metadata.get("sha256"), str)
            and sample_nonzero
        )
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        valid = False
    print(json.dumps({"event": "verify", "path": str(path), "bytes": expected_size, "valid": valid}))
    return valid


def read(path: Path, block_kb: int) -> None:
    block = block_kb * 1024
    total = 0
    started = time.monotonic_ns()
    first_byte_ns = None
    checksum = 0
    with path.open("rb", buffering=0) as stream:
        while True:
            data = stream.read(block)
            if not data:
                break
            if first_byte_ns is None:
                first_byte_ns = time.monotonic_ns()
            checksum = zlib.crc32(data, checksum)
            total += len(data)
    ended = time.monotonic_ns()
    elapsed_s = (ended - started) / 1e9
    print(json.dumps({
        "event": "cold_launch_read",
        "path": str(path),
        "bytes": total,
        "block_kb": block_kb,
        "first_byte_ms": (first_byte_ns - started) / 1e6 if first_byte_ns else None,
        "elapsed_ms": elapsed_s * 1000,
        "throughput_mb_s": total / 1024**2 / elapsed_s,
        "crc32": f"{checksum:08x}",
    }))


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    create_p = sub.add_parser("create")
    create_p.add_argument("--path", required=True)
    create_p.add_argument("--size-gb", type=float, default=1.2)
    verify_p = sub.add_parser("verify")
    verify_p.add_argument("--path", required=True)
    verify_p.add_argument("--size-gb", type=float, default=1.2)
    read_p = sub.add_parser("read")
    read_p.add_argument("--path", required=True)
    read_p.add_argument("--block-kb", type=int, default=128)
    args = parser.parse_args()
    if args.command == "create":
        create(Path(args.path), args.size_gb)
    elif args.command == "read":
        read(Path(args.path), args.block_kb)
    else:
        return 0 if verify(Path(args.path), args.size_gb) else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
