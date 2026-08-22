#!/usr/bin/env python3
"""Inspect a Linux application's process tree and infer cgroup-friendly roles."""

import argparse
import json
import os
import re
import subprocess
import time
from pathlib import Path


ROLE_PATTERNS = {
    "audio": [r"audio\.mojom", r"audio[-_ ]?service", r"--type=audio"],
    "gpu": [r"--type=gpu-process", r"gpu\.mojom"],
    "network": [r"network\.mojom", r"network[-_ ]?service"],
    "renderer": [r"--type=renderer", r"renderer-process"],
    "utility": [r"--type=utility"],
}


def read_text(path: Path, binary=False):
    try:
        data = path.read_bytes()
        if binary:
            return data.decode(errors="replace")
        return data.decode(errors="replace").strip()
    except (FileNotFoundError, PermissionError, ProcessLookupError):
        return None


def proc_table():
    result = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        stat = read_text(entry / "stat")
        if not stat:
            continue
        end = stat.rfind(")")
        try:
            ppid = int(stat[end + 2 :].split()[1])
        except (ValueError, IndexError):
            continue
        raw = read_text(entry / "cmdline", binary=True) or ""
        cmdline = " ".join(x for x in raw.split("\0") if x)
        comm = read_text(entry / "comm") or ""
        try:
            exe = os.readlink(entry / "exe")
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            exe = None
        result[pid] = {"pid": pid, "ppid": ppid, "comm": comm, "exe": exe, "cmdline": cmdline}
    return result


def descendants(table, roots):
    selected = set(roots)
    changed = True
    while changed:
        changed = False
        for pid, info in table.items():
            if info["ppid"] in selected and pid not in selected:
                selected.add(pid)
                changed = True
    return selected


def role_for(info, audio_pids):
    if info["pid"] in audio_pids:
        return "audio", "audio-stream-pid"
    text = f'{info["comm"]} {info["cmdline"]}'.lower()
    for role, patterns in ROLE_PATTERNS.items():
        for pattern in patterns:
            if re.search(pattern, text):
                return role, pattern
    return "other", "fallback"


def pactl_audio_pids():
    try:
        run = subprocess.run(
            ["pactl", "-f", "json", "list", "sink-inputs"],
            text=True, capture_output=True, timeout=4, check=False,
        )
        values = json.loads(run.stdout or "[]") if run.returncode == 0 else []
    except (FileNotFoundError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return set(), []
    pids, streams = set(), []
    for value in values:
        props = value.get("properties", {})
        raw_pid = props.get("application.process.id")
        try:
            pid = int(raw_pid)
            pids.add(pid)
        except (TypeError, ValueError):
            pid = None
        streams.append({
            "pid": pid,
            "application_name": props.get("application.name"),
            "media_name": props.get("media.name"),
        })
    return pids, streams


def snapshot(pattern):
    table = proc_table()
    matcher = re.compile(pattern, re.I)
    own_ancestors, cursor = {os.getpid()}, os.getppid()
    while cursor in table and cursor not in own_ancestors:
        own_ancestors.add(cursor)
        cursor = table[cursor]["ppid"]
    roots = []
    for pid, info in table.items():
        searchable = " ".join(filter(None, (info["comm"], info["exe"] or "", info["cmdline"])))
        if pid not in own_ancestors and matcher.search(searchable):
            roots.append(pid)
    # Keep only topmost matches, then include every descendant regardless of name.
    root_set = set(roots)
    roots = sorted(pid for pid in roots if table[pid]["ppid"] not in root_set)
    selected = descendants(table, roots)
    audio_pids, streams = pactl_audio_pids()
    processes = []
    for pid in sorted(selected):
        info = dict(table[pid])
        info["cgroup"] = read_text(Path("/proc") / str(pid) / "cgroup")
        info["role"], info["role_evidence"] = role_for(info, audio_pids)
        processes.append(info)
    return {
        "timestamp_ns": time.time_ns(),
        "pattern": pattern,
        "roots": roots,
        "processes": processes,
        "audio_streams": streams,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--pattern", default=r"bilibili|bili-app|io\.github\.msojocs\.bilibili")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--samples", type=int, default=1)
    parser.add_argument("--interval", type=float, default=2.0)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8") as handle:
        for index in range(args.samples):
            handle.write(json.dumps(snapshot(args.pattern), ensure_ascii=False) + "\n")
            handle.flush()
            if index + 1 < args.samples:
                time.sleep(args.interval)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
