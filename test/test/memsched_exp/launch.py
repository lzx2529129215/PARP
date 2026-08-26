from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any


def _descendants(root_pid: int) -> set[int]:
    found = {root_pid}
    changed = True
    while changed:
        changed = False
        for status in Path("/proc").glob("[0-9]*/status"):
            try:
                values = status.read_text(encoding="utf-8").splitlines()
                pid = int(status.parent.name)
                ppid = int(next(line.split()[1] for line in values if line.startswith("PPid:")))
            except (OSError, StopIteration, ValueError):
                continue
            if ppid in found and pid not in found:
                found.add(pid)
                changed = True
    return found


def _wmctrl_windows() -> list[dict[str, Any]]:
    if not shutil.which("wmctrl"):
        return []
    result = subprocess.run(["wmctrl", "-lp"], text=True, capture_output=True, check=False)
    windows: list[dict[str, Any]] = []
    for line in result.stdout.splitlines():
        parts = line.split(None, 4)
        if len(parts) < 5:
            continue
        try:
            pid = int(parts[2])
        except ValueError:
            continue
        windows.append({"window_id": parts[0], "pid": pid, "title": parts[4]})
    return windows


def _wmctrl_window(
    root_pid: int,
    title_pattern: re.Pattern[str] | None,
    existing_window_ids: set[str],
    cgroup_pids: set[int] | None = None,
) -> dict[str, Any] | None:
    descendants = _descendants(root_pid)
    for window in _wmctrl_windows():
        if window["window_id"] in existing_window_ids:
            continue
        title_matches = title_pattern is None or title_pattern.search(window["title"])
        owner_matches = window["pid"] in descendants or (
            cgroup_pids is not None and window["pid"] in cgroup_pids
        )
        # systemd-run is not the parent of the service process. In that case the
        # launch probe accepts only a newly-created title-matching window.
        if title_matches and (owner_matches or (title_pattern is not None and cgroup_pids is None)):
            return window
    return None


def _read_cgroup_pids(path_file: Path | None) -> set[int] | None:
    if path_file is None:
        return None
    try:
        cgroup = Path(path_file.read_text(encoding="utf-8").strip())
    except (FileNotFoundError, PermissionError, OSError):
        return set()
    pids: set[int] = set()
    for procs in cgroup.rglob("cgroup.procs"):
        try:
            pids.update(int(value) for value in procs.read_text(encoding="utf-8").split())
        except (FileNotFoundError, PermissionError, OSError, ValueError):
            continue
    return pids


def measure_launch(
    command: list[str],
    timeout_s: float,
    window_regex: str | None,
    ready_file: Path | None,
    start_file: Path | None = None,
    cgroup_path_file: Path | None = None,
) -> dict[str, Any]:
    if ready_file:
        ready_file.unlink(missing_ok=True)
    if start_file:
        start_file.unlink(missing_ok=True)
    title_pattern = re.compile(window_regex, re.IGNORECASE) if window_regex else None
    existing_window_ids = {window["window_id"] for window in _wmctrl_windows()}
    initial_start_ns = time.monotonic_ns()
    start_ns = initial_start_ns
    process = subprocess.Popen(command, start_new_session=True)
    process_created_ns = time.monotonic_ns()
    deadline = time.monotonic() + timeout_s
    if start_file:
        while time.monotonic() < deadline and not start_file.exists():
            time.sleep(0.01)
        if not start_file.exists():
            return {
                "command": command,
                "launcher_pid": process.pid,
                "start_monotonic_ns": None,
                "launcher_spawn_ms": (process_created_ns - initial_start_ns) / 1e6,
                "process_created_ms": None,
                "clock_reset_at_start_marker": True,
                "ready_monotonic_ns": None,
                "launch_latency_ms": None,
                "measurement_source": None,
                "window": None,
                "timed_out": True,
                "timeout_s": timeout_s,
                "warning": "The application start marker was not observed.",
            }
        try:
            marker_value = json.loads(start_file.read_text(encoding="utf-8"))
            start_ns = int(marker_value["monotonic_ns"])
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            start_ns = time.monotonic_ns()
        deadline = time.monotonic() + timeout_s
    window: dict[str, Any] | None = None
    ready_ns: int | None = None
    source: str | None = None
    while time.monotonic() < deadline:
        if ready_file and ready_file.exists():
            ready_ns = time.monotonic_ns()
            source = "ready_file_first_frame_marker"
            break
        window = _wmctrl_window(
            process.pid,
            title_pattern,
            existing_window_ids,
            _read_cgroup_pids(cgroup_path_file),
        )
        if window is not None:
            ready_ns = time.monotonic_ns()
            source = "x11_first_mapped_window"
            break
        if process.poll() is not None and title_pattern is None and ready_file is None:
            break
        time.sleep(0.02)
    return {
        "command": command,
        "launcher_pid": process.pid,
        "start_monotonic_ns": start_ns,
        "launcher_spawn_ms": (process_created_ns - initial_start_ns) / 1e6,
        "process_created_ms": (process_created_ns - start_ns) / 1e6 if start_file is None else None,
        "clock_reset_at_start_marker": start_file is not None,
        "ready_monotonic_ns": ready_ns,
        "launch_latency_ms": (ready_ns - start_ns) / 1e6 if ready_ns else None,
        "measurement_source": source,
        "window": window,
        "timed_out": ready_ns is None,
        "timeout_s": timeout_s,
        "warning": None if source == "ready_file_first_frame_marker" else "X11 mapped-window time is a proxy, not proof of first interactive frame.",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Measure process launch to first window or explicit ready marker")
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--window-regex")
    parser.add_argument("--ready-file")
    parser.add_argument("--start-file", help="reset the launch clock when this file appears")
    parser.add_argument("--cgroup-path-file", help="require the mapped-window PID to belong to this recorded cgroup")
    parser.add_argument("--output", required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("a command is required after --")
    result = measure_launch(
        command,
        args.timeout,
        args.window_regex,
        Path(args.ready_file) if args.ready_file else None,
        Path(args.start_file) if args.start_file else None,
        Path(args.cgroup_path_file) if args.cgroup_path_file else None,
    )
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 2 if result["timed_out"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
