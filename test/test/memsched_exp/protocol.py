from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path
from typing import Any, Iterable


PROTOCOL_VERSION = 1


class ProtocolError(RuntimeError):
    """Raised when an experiment synchronization marker is invalid."""


def marker_value(event: str, **extra: Any) -> dict[str, Any]:
    return {
        "protocol_version": PROTOCOL_VERSION,
        "event": event,
        "monotonic_ns": time.monotonic_ns(),
        "realtime_ns": time.time_ns(),
        "pid": os.getpid(),
        **extra,
    }


def write_marker(path: str | Path, event: str, **extra: Any) -> dict[str, Any]:
    """Atomically publish a timestamped synchronization marker."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    value = marker_value(event, **extra)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(temporary, target)
    return value


def read_marker(path: str | Path) -> dict[str, Any]:
    target = Path(path)
    try:
        value = json.loads(target.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ProtocolError(f"invalid marker {target}: {type(error).__name__}: {error}") from error
    if not isinstance(value, dict):
        raise ProtocolError(f"invalid marker {target}: expected a JSON object")
    try:
        monotonic_ns = int(value["monotonic_ns"])
    except (KeyError, TypeError, ValueError) as error:
        raise ProtocolError(f"invalid marker {target}: monotonic_ns is required") from error
    if monotonic_ns <= 0:
        raise ProtocolError(f"invalid marker {target}: monotonic_ns must be positive")
    return value


def wait_for_paths(
    paths: Iterable[str | Path],
    timeout_s: float,
    poll_interval_s: float = 0.01,
) -> list[Path]:
    targets = [Path(path) for path in paths]
    deadline = time.monotonic() + timeout_s
    missing = targets
    while missing and time.monotonic() < deadline:
        missing = [path for path in targets if not path.exists()]
        if missing:
            time.sleep(poll_interval_s)
    if missing:
        joined = ", ".join(str(path) for path in missing)
        raise ProtocolError(f"timed out waiting for marker(s): {joined}")
    return targets


def wait_for_markers(
    paths: Iterable[str | Path],
    timeout_s: float,
    minimum_monotonic_ns: int | None = None,
) -> list[dict[str, Any]]:
    targets = wait_for_paths(paths, timeout_s)
    values = [read_marker(path) for path in targets]
    if minimum_monotonic_ns is not None:
        stale = [
            str(path)
            for path, value in zip(targets, values)
            if int(value["monotonic_ns"]) < minimum_monotonic_ns
        ]
        if stale:
            raise ProtocolError(f"stale marker(s) predate the required boundary: {', '.join(stale)}")
    return values


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create or wait for experiment protocol markers")
    subparsers = parser.add_subparsers(dest="command", required=True)

    mark = subparsers.add_parser("mark")
    mark.add_argument("--path", required=True)
    mark.add_argument("--event", required=True)

    wait = subparsers.add_parser("wait")
    wait.add_argument("--path", action="append", required=True)
    wait.add_argument("--timeout", type=float, default=60.0)

    args = parser.parse_args(argv)
    try:
        if args.command == "mark":
            value = write_marker(args.path, args.event)
            print(json.dumps(value, ensure_ascii=False))
        else:
            values = wait_for_markers(args.path, args.timeout)
            print(json.dumps(values, ensure_ascii=False))
    except ProtocolError as error:
        print(str(error))
        return 6
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
