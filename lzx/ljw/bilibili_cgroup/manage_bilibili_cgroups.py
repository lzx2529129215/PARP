#!/usr/bin/env python3
"""Launch Bilibili in a delegated cgroup v2 subtree and classify its processes."""

import argparse
import json
import os
import re
import signal
import subprocess
import time
from pathlib import Path


EXECUTABLE = "/opt/apps/io.github.msojocs.bilibili/files/bin/bin/bilibili"
GROUP_RULES = (
    ("audio", re.compile(r"--utility-sub-type=audio\.mojom\.AudioService")),
    ("gpu", re.compile(r"--type=gpu-process")),
    ("network", re.compile(r"--utility-sub-type=network\.mojom\.NetworkService")),
    ("renderer", re.compile(r"--type=renderer")),
)
GROUPS = tuple(name for name, _ in GROUP_RULES) + ("other",)


def own_cgroup():
    for line in Path("/proc/self/cgroup").read_text().splitlines():
        if line.startswith("0::"):
            return Path("/sys/fs/cgroup") / line[3:].lstrip("/")
    raise RuntimeError("not running in cgroup v2")


def read_processes():
    result = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / "stat").read_text()
            end = stat.rfind(")")
            ppid = int(stat[end + 2:].split()[1])
            cmdline = " ".join(x for x in (entry / "cmdline").read_bytes().decode(errors="replace").split("\0") if x)
            result[int(entry.name)] = (ppid, cmdline)
        except (FileNotFoundError, PermissionError, ProcessLookupError, ValueError, IndexError):
            pass
    return result


def descendants(table, root):
    values = {root}
    changed = True
    while changed:
        changed = False
        for pid, (ppid, _) in table.items():
            if ppid in values and pid not in values:
                values.add(pid)
                changed = True
    values.discard(root)
    return values


def classify(cmdline):
    for group, pattern in GROUP_RULES:
        if pattern.search(cmdline):
            return group, pattern.pattern
    return "other", "fallback"


def write(path, value):
    path.write_text(str(value), encoding="ascii")


def setup_tree(parent):
    available = set((parent / "cgroup.controllers").read_text().split())
    enabled = [name for name in ("cpu", "memory", "io", "pids") if name in available]
    children = {}
    for name in GROUPS:
        child = parent / name
        child.mkdir(exist_ok=True)
        children[name] = child
    # Move the manager out of the domain parent before enabling controllers.
    write(children["other"] / "cgroup.procs", os.getpid())
    if enabled:
        write(parent / "cgroup.subtree_control", " ".join(f"+{x}" for x in enabled))
    return children, enabled


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--events", type=Path, required=True)
    parser.add_argument("--duration", type=float, default=0, help="0 means until app exits")
    parser.add_argument("app_args", nargs="*")
    args = parser.parse_args()
    parent = own_cgroup()
    children, controllers = setup_tree(parent)
    args.events.parent.mkdir(parents=True, exist_ok=True)
    stop = False

    def request_stop(_signum, _frame):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    app = subprocess.Popen([EXECUTABLE, *args.app_args])
    deadline = time.monotonic() + args.duration if args.duration else None
    last_group = {}
    with args.events.open("w", encoding="utf-8") as events:
        events.write(json.dumps({
            "event": "start", "parent": str(parent), "manager_pid": os.getpid(),
            "app_pid": app.pid, "controllers": controllers,
        }) + "\n")
        events.flush()
        while not stop and app.poll() is None and (deadline is None or time.monotonic() < deadline):
            table = read_processes()
            for pid in sorted(descendants(table, app.pid) | {app.pid}):
                info = table.get(pid)
                if not info:
                    continue
                group, evidence = classify(info[1])
                if last_group.get(pid) == group:
                    continue
                try:
                    write(children[group] / "cgroup.procs", pid)
                except (FileNotFoundError, ProcessLookupError, PermissionError, OSError) as error:
                    events.write(json.dumps({"event": "move_error", "pid": pid, "group": group, "error": str(error)}) + "\n")
                else:
                    last_group[pid] = group
                    try:
                        cgroup_after = (Path("/proc") / str(pid) / "cgroup").read_text().strip()
                    except (FileNotFoundError, PermissionError, ProcessLookupError):
                        cgroup_after = None
                    events.write(json.dumps({"event": "move", "pid": pid, "group": group, "evidence": evidence, "cmdline": info[1], "cgroup_after": cgroup_after}) + "\n")
                events.flush()
            time.sleep(0.2)
        if app.poll() is None:
            app.terminate()
            try:
                app.wait(timeout=8)
            except subprocess.TimeoutExpired:
                app.kill()
                app.wait()
        events.write(json.dumps({"event": "stop", "returncode": app.returncode}) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
