#!/usr/bin/env python3
"""Run the frozen four-pair R12 Native replay after the one-shot kernel boot."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
OUTPUT = ROOT / "test/outputs/r12_current_kernel_oom"
PARP_SESSION = OUTPUT / "r8-current_kernel-20260927_173242-6.17.13-parp-lzx-v4.2-apply-myfs-guided-r11-workload-cold-bin"
CONFIG = OUTPUT / "calibration-20260927-v5/frozen-config.json"
STATUS = OUTPUT / "native-four-status.json"
LOG = OUTPUT / "native-four-run.log"
DONE = OUTPUT / "native-four-done.json"
RUNNER = ROOT / "test/test/parp-real-pc-experiment-lzx.py"
COMPARE = ROOT / "test/test/parp-r12-compare-lzx.py"
NATIVE_RELEASE = "6.17.13-native-6.17.13"


def write_status(status: str, **fields: object) -> None:
    STATUS.write_text(json.dumps({
        "schema_version": 1, "status": status, "timestamp_ns": time.time_ns(),
        "kernel": os.uname().release, **fields,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def ready() -> bool:
    def active(unit: str) -> bool:
        return subprocess.run(
            ["systemctl", "--user", "is-active", unit],
            capture_output=True, text=True, check=False, timeout=5,
        ).stdout.strip() == "active"

    if not active("graphical-session.target") or not active("parp-runtime-monitor.service"):
        return False
    monitor = subprocess.run(
        ["systemctl", "--user", "show", "parp-runtime-monitor.service", "-p", "MainPID", "--value"],
        capture_output=True, text=True, check=False, timeout=5,
    ).stdout.strip()
    if not monitor.isdigit() or int(monitor) <= 0:
        return False
    try:
        command = (Path("/proc") / monitor / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
    except OSError:
        return False
    if "--parp-myfs-mode off" not in command or "--file-event-source off" not in command:
        raise RuntimeError("Native runtime monitor does not match the frozen off/off feature state")
    authority = next(Path(f"/run/user/{os.getuid()}").glob(".mutter-Xwaylandauth.*"), None)
    if authority is None:
        return False
    env = {**os.environ, "DISPLAY": ":0", "XAUTHORITY": str(authority)}
    return subprocess.run(
        ["xdotool", "getmouselocation"], env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False, timeout=5,
    ).returncode == 0


def main() -> int:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    if DONE.exists():
        write_status("ALREADY_COMPLETE", done=str(DONE))
        return 0
    if os.uname().release != NATIVE_RELEASE:
        write_status("BLOCKED", reason="Native kernel is not running")
        return 1
    parp = json.loads((PARP_SESSION / "summary.json").read_text(encoding="utf-8"))
    seeds = sorted(int(row["seed"]) for row in parp["runs"] if row.get("valid"))
    if parp["status"] != "COMPLETE" or len(seeds) != 4 or len(set(seeds)) != 4:
        write_status("BLOCKED", reason="four unique valid PARP seeds are unavailable", seeds=seeds)
        return 1
    resume_session = None
    for candidate in sorted(OUTPUT.glob("r8-native_kernel-*"), key=lambda path: path.stat().st_mtime, reverse=True):
        summary_path = candidate / "summary.json"
        if not summary_path.is_file():
            continue
        previous = json.loads(summary_path.read_text(encoding="utf-8"))
        if (previous.get("policy") == "native_kernel"
                and previous.get("scenario") == "r12_current_kernel_oom_baseline"
                and previous.get("config_sha256") == parp.get("config_sha256")
                and previous.get("replay_from") == str(PARP_SESSION)):
            resume_session = candidate
            break
    write_status("WAITING_FOR_GUI", seeds=seeds)
    deadline = time.monotonic() + 600
    while time.monotonic() < deadline:
        if ready():
            break
        time.sleep(5)
    else:
        write_status("BLOCKED", reason="graphical session or runtime monitor did not become ready")
        return 1
    command = [
        sys.executable, "-u", str(RUNNER), "run", "--config", str(CONFIG),
        "--policy", "native_kernel", "--scenario", "r12_current_kernel_oom_baseline",
        "--rounds", "4", "--seed", str(seeds[0]), "--keep-going",
        "--replay-from", str(PARP_SESSION),
    ]
    if resume_session is not None:
        command.extend(["--resume-session", str(resume_session)])
    write_status("RUNNING", seeds=seeds, command=command, resume_session=str(resume_session) if resume_session else None)
    with LOG.open("a", encoding="utf-8") as stream:
        stream.write(f"\nRESUME {resume_session or 'new session'}\n")
        completed = subprocess.run(command, cwd=ROOT, stdout=stream, stderr=subprocess.STDOUT, check=False)
    lines = [line.strip() for line in LOG.read_text(encoding="utf-8").splitlines() if line.strip()]
    sessions = [Path(line) for line in lines if line.startswith(str(OUTPUT / "r8-native_kernel-"))]
    native_session = sessions[-1] if sessions else resume_session
    if completed.returncode != 0 or native_session is None:
        write_status("INCOMPLETE", reason="Native replay failed", returncode=completed.returncode,
                     native_session=str(native_session) if native_session else None, log=str(LOG))
        return 1
    comparison = OUTPUT / "comparison-four"
    compare_command = [
        sys.executable, str(COMPARE), "--parp-session", str(PARP_SESSION),
        "--native-session", str(native_session), "--output", str(comparison),
    ]
    with LOG.open("a", encoding="utf-8") as stream:
        compared = subprocess.run(compare_command, cwd=ROOT, stdout=stream, stderr=subprocess.STDOUT, check=False)
    if compared.returncode != 0:
        write_status("INCOMPLETE", reason="four-pair comparison failed", returncode=compared.returncode,
                     native_session=str(native_session), log=str(LOG))
        return 1
    payload = {
        "schema_version": 1, "status": "COMPLETE", "timestamp_ns": time.time_ns(),
        "parp_session": str(PARP_SESSION), "native_session": str(native_session),
        "comparison": str(comparison / "comparison.md"), "seeds": seeds,
    }
    DONE.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    write_status("COMPLETE", **{key: value for key, value in payload.items() if key not in {"schema_version", "status", "timestamp_ns"}})
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        write_status("ERROR", reason=f"{type(exc).__name__}: {exc}")
        raise
