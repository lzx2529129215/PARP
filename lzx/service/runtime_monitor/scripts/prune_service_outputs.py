#!/usr/bin/env python3
"""Bound disk use of resident Runtime Monitor sessions."""

from __future__ import annotations

import argparse
import re
import shutil
from dataclasses import dataclass
from pathlib import Path


SERVICE_SESSION_RE = re.compile(r"service_[0-9a-f]{12}_[0-9]{8}_[0-9]{6}")


@dataclass(frozen=True)
class Session:
    path: Path
    size: int
    mtime_ns: int


def _directory_size(path: Path) -> int:
    total = 0
    for entry in path.rglob("*"):
        try:
            if entry.is_file() and not entry.is_symlink():
                total += entry.stat().st_size
        except FileNotFoundError:
            continue
    return total


def discover_sessions(output_root: Path) -> list[Session]:
    """Return only validated immediate service-session directories, oldest first."""
    root = output_root.resolve()
    sessions: list[Session] = []
    for path in root.iterdir():
        if path.is_symlink() or not path.is_dir() or not SERVICE_SESSION_RE.fullmatch(path.name):
            continue
        resolved = path.resolve()
        if resolved.parent != root:
            continue
        stat = resolved.stat()
        sessions.append(Session(resolved, _directory_size(resolved), stat.st_mtime_ns))
    return sorted(sessions, key=lambda item: (item.mtime_ns, item.path.name))


def prune(
    output_root: Path,
    *,
    max_sessions: int,
    reserve_sessions: int,
    max_bytes: int,
    min_free_bytes: int,
    dry_run: bool = False,
) -> list[Path]:
    if min(max_sessions, reserve_sessions, max_bytes, min_free_bytes) < 0:
        raise ValueError("retention values must be non-negative")
    root = output_root.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    sessions = discover_sessions(root)
    allowed_completed = max(0, max_sessions - reserve_sessions)
    total_bytes = sum(item.size for item in sessions)
    free_bytes = shutil.disk_usage(root).free
    removed: list[Path] = []

    while sessions and (
        len(sessions) > allowed_completed
        or total_bytes > max_bytes
        or free_bytes < min_free_bytes
    ):
        victim = sessions.pop(0)
        # lzx-note: Refuse broad or redirected targets immediately before the
        # destructive operation; only one validated child can ever be removed.
        if victim.path.parent != root or not SERVICE_SESSION_RE.fullmatch(victim.path.name):
            raise RuntimeError(f"unsafe retention target: {victim.path}")
        removed.append(victim.path)
        total_bytes -= victim.size
        free_bytes += victim.size
        if not dry_run:
            shutil.rmtree(victim.path)

    return removed


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--max-sessions", type=int, default=7)
    parser.add_argument("--reserve-sessions", type=int, default=1)
    parser.add_argument("--max-bytes", type=int, default=4 * 1024**3)
    parser.add_argument("--min-free-bytes", type=int, default=5 * 1024**3)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    removed = prune(
        args.output_root,
        max_sessions=args.max_sessions,
        reserve_sessions=args.reserve_sessions,
        max_bytes=args.max_bytes,
        min_free_bytes=args.min_free_bytes,
        dry_run=args.dry_run,
    )
    for path in removed:
        action = "would_remove" if args.dry_run else "removed"
        print(f"{action}={path}")
    print(f"retention_removed={len(removed)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
