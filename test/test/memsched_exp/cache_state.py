from __future__ import annotations

import argparse
import ctypes
import json
import mmap
import os
import time
from pathlib import Path
from typing import Any


def _unsupported(path: Path, reason: str) -> dict[str, Any]:
    return {
        "path": str(path),
        "supported": False,
        "valid": False,
        "reason": reason,
        "bytes": path.stat().st_size if path.exists() else None,
        "page_size": mmap.PAGESIZE,
        "total_pages": None,
        "resident_pages": None,
        "resident_ratio": None,
    }


def file_residency(path: str | Path) -> dict[str, Any]:
    """Measure file-page residency with Linux mincore without faulting file data in."""
    target = Path(path)
    if os.name != "posix":
        return _unsupported(target, "mincore residency checks require Linux/POSIX")
    try:
        size = target.stat().st_size
    except OSError as error:
        return _unsupported(target, f"cannot stat file: {type(error).__name__}: {error}")
    if size == 0:
        return {
            "path": str(target),
            "supported": True,
            "valid": True,
            "reason": None,
            "bytes": 0,
            "page_size": mmap.PAGESIZE,
            "total_pages": 0,
            "resident_pages": 0,
            "resident_ratio": 0.0,
        }

    page_count = (size + mmap.PAGESIZE - 1) // mmap.PAGESIZE
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        mincore = libc.mincore
        mincore.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_ubyte)]
        mincore.restype = ctypes.c_int
        vector = (ctypes.c_ubyte * page_count)()
        with target.open("rb") as stream:
            mapping = mmap.mmap(stream.fileno(), size, access=mmap.ACCESS_COPY)
            first_byte = ctypes.c_char.from_buffer(mapping)
            try:
                result = mincore(ctypes.addressof(first_byte), size, vector)
                if result != 0:
                    errno = ctypes.get_errno()
                    raise OSError(errno, os.strerror(errno))
            finally:
                del first_byte
                mapping.close()
    except (AttributeError, BufferError, OSError, ValueError) as error:
        return _unsupported(target, f"mincore failed: {type(error).__name__}: {error}")

    resident_pages = sum(1 for value in vector if value & 1)
    return {
        "path": str(target),
        "supported": True,
        "valid": True,
        "reason": None,
        "bytes": size,
        "page_size": mmap.PAGESIZE,
        "total_pages": page_count,
        "resident_pages": resident_pages,
        "resident_ratio": resident_pages / page_count,
    }


def evict_file_cache(
    path: str | Path,
    max_resident_ratio: float = 0.01,
    retries: int = 3,
) -> dict[str, Any]:
    """Request per-file cache eviction and prove the resulting residency level."""
    target = Path(path)
    before = file_residency(target)
    if (
        os.name != "posix"
        or not hasattr(os, "posix_fadvise")
        or not hasattr(os, "POSIX_FADV_DONTNEED")
    ):
        return {
            "path": str(target),
            "supported": False,
            "valid": False,
            "reason": "POSIX_FADV_DONTNEED is unavailable",
            "max_resident_ratio": max_resident_ratio,
            "before": before,
            "after": before,
        }
    if not before.get("supported"):
        return {
            "path": str(target),
            "supported": False,
            "valid": False,
            "reason": before.get("reason"),
            "max_resident_ratio": max_resident_ratio,
            "before": before,
            "after": before,
        }

    attempts = 0
    after = before
    error_text = None
    for attempts in range(1, max(retries, 1) + 1):
        try:
            with target.open("rb", buffering=0) as stream:
                os.posix_fadvise(stream.fileno(), 0, 0, os.POSIX_FADV_DONTNEED)
        except OSError as error:
            error_text = f"posix_fadvise failed: {type(error).__name__}: {error}"
            break
        time.sleep(0.05)
        after = file_residency(target)
        ratio = after.get("resident_ratio")
        if after.get("supported") and ratio is not None and float(ratio) <= max_resident_ratio:
            break

    ratio = after.get("resident_ratio")
    valid = bool(after.get("supported")) and ratio is not None and float(ratio) <= max_resident_ratio
    reason = error_text
    if reason is None and not valid:
        reason = f"resident ratio {ratio!r} exceeds cold-cache threshold {max_resident_ratio}"
    return {
        "path": str(target),
        "supported": bool(before.get("supported") and after.get("supported")),
        "valid": valid,
        "reason": reason,
        "max_resident_ratio": max_resident_ratio,
        "attempts": attempts,
        "before": before,
        "after": after,
    }


def _write(path: Path | None, value: dict[str, Any]) -> None:
    text = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    if path is None:
        print(text, end="")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    print(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Inspect and verify Linux file page-cache state")
    subparsers = parser.add_subparsers(dest="command", required=True)
    inspect = subparsers.add_parser("inspect")
    inspect.add_argument("--path", required=True)
    inspect.add_argument("--output")
    evict = subparsers.add_parser("evict")
    evict.add_argument("--path", required=True)
    evict.add_argument("--max-resident-ratio", type=float, default=0.01)
    evict.add_argument("--retries", type=int, default=3)
    evict.add_argument("--output")
    args = parser.parse_args(argv)
    if args.command == "inspect":
        value = file_residency(args.path)
    else:
        value = evict_file_cache(args.path, args.max_resident_ratio, args.retries)
    _write(Path(args.output) if args.output else None, value)
    return 0 if value.get("valid") else 4


if __name__ == "__main__":
    raise SystemExit(main())
