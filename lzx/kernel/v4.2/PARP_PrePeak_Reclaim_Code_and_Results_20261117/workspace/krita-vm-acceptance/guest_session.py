#!/usr/bin/env python3
"""Temporary foreground job runner over the user-authorized shared directory.

No network listener, login automation, system service, or startup registration.
Runs commands as the desktop user who explicitly starts this script. Ctrl+C
ends the runner. Each request, output and exit status remains in this session.
"""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import platform
import signal
import subprocess
import sys
import time
import uuid

HERE = Path(__file__).resolve().parent
WORKSPACE = HERE.parent


def save(path, data):
    temporary = path.with_suffix(path.suffix+".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    temporary.replace(path)


def verify_frozen():
    record = json.loads((HERE/"reclaim-frozen-before.json").read_text(encoding="utf-8"))
    changed = []
    for item in record["files"]:
        path = WORKSPACE/Path(item["path"].replace("\\", "/"))
        digest = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
        if digest != item["sha256"]:
            changed.append(item["path"])
    if changed:
        raise RuntimeError("提前回收代码哈希发生变化，停止："+repr(changed))
    return len(record["files"])


def probe():
    from shutil import which
    import importlib.util
    files = ("/etc/os-release", "/proc/meminfo", "/proc/swaps", "/proc/cmdline",
             "/proc/self/cgroup", "/sys/module/zswap/parameters/enabled",
             "/sys/module/zswap/parameters/compressor", "/sys/module/zswap/parameters/zpool")
    values = {}
    for name in files:
        try:
            values[name] = dict(value=Path(name).read_text())
        except OSError as error:
            values[name] = dict(value=None, error=str(error))
    tools = {name:which(name) for name in ("krita", "gimp", "xdpyinfo", "xdotool", "wmctrl",
             "xprop", "xwininfo", "tesseract", "systemd-run", "gcc", "sudo", "vmware-hgfsclient")}
    return dict(hostname=platform.node(), kernel=platform.release(), platform=platform.platform(),
                uid=os.getuid(), user=os.environ.get("USER"), workspace=str(WORKSPACE),
                display=os.environ.get("DISPLAY"), session_type=os.environ.get("XDG_SESSION_TYPE"),
                session_variables_present={name:bool(os.getenv(name)) for name in
                  ("XAUTHORITY", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS")},
                files=values, tools=tools, python=sys.executable,
                python_dependencies={name:importlib.util.find_spec(name) is not None
                                     for name in ("pyautogui", "cv2", "PIL")})


def execute(request, session):
    item = json.loads(request.read_text(encoding="utf-8"))
    if set(item)-{"argv", "cwd", "timeout_s", "env"}:
        raise ValueError("unknown request fields")
    argv = item["argv"]
    if not isinstance(argv, list) or not argv or not all(isinstance(x, str) and "\0" not in x for x in argv):
        raise ValueError("argv must be a nonempty string list")
    cwd = (WORKSPACE/item.get("cwd", ".")).resolve(strict=True)
    if not cwd.is_relative_to(WORKSPACE) or not cwd.is_dir():
        raise ValueError("cwd must be within the shared code workspace")
    timeout = item.get("timeout_s", 300)
    if type(timeout) not in (int, float) or not 1 <= timeout <= 3600:
        raise ValueError("timeout must be 1..3600 seconds")
    extra_env = item.get("env", {})
    if not isinstance(extra_env, dict) or not all(isinstance(k, str) and isinstance(v, str)
                                                for k, v in extra_env.items()):
        raise ValueError("env must map strings to strings")
    env = os.environ.copy()
    env.update(extra_env)
    output = session/"logs"/(request.stem+".log")
    started = time.time_ns()
    verify_frozen()
    with output.open("wb") as stream:
        process = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                   stdout=stream, stderr=subprocess.STDOUT, start_new_session=True)
        save(session/"active.json", dict(job=request.stem, pid=process.pid, argv=argv,
                                         cwd=str(cwd), started_ns=started))
        preserved = False
        try:
            code = process.wait(timeout=timeout)
        except KeyboardInterrupt:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass  # The owned child may already have exited after signalling us.
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                print("Owned job remains alive; preserved without SIGKILL.", flush=True)
            raise
        except subprocess.TimeoutExpired:
            # Signal only this owned job. Never SIGKILL a potentially blocked
            # memory.high/reclaim writer or destroy the GUI failure scene.
            os.killpg(process.pid, signal.SIGTERM)
            try:
                code = process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                preserved = True
                code = None
    result = dict(job=request.stem, argv=argv, returncode=code, started_ns=started,
                  ended_ns=time.time_ns(), log=str(output.relative_to(session)),
                  preserved_process=preserved, frozen_files_verified=verify_frozen())
    save(session/"results"/(request.stem+".json"), result)
    if preserved:
        raise RuntimeError("Owned job did not stop; preserved without SIGKILL. Runner exits.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--minutes", type=int, default=120)
    args = parser.parse_args()
    if sys.platform != "linux" or os.getuid() == 0:
        parser.error("run as the ordinary logged-in Linux desktop user")
    if not 1 <= args.minutes <= 360:
        parser.error("session duration must be 1..360 minutes")
    verify_frozen()
    session = HERE/"sessions"/uuid.uuid4().hex
    for name in ("requests", "results", "logs"):
        (session/name).mkdir(parents=True, exist_ok=False)
    save(session/"probe.json", probe())
    save(session/"session.json", dict(status="READY", pid=os.getpid(), workspace=str(WORKSPACE),
         started_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),
         expires_after_minutes=args.minutes, network_listener=False))
    print("Temporary test session READY:", session, flush=True)
    print("No network listener. Runs as your current desktop user. Ctrl+C to stop.", flush=True)
    deadline = time.monotonic()+args.minutes*60
    seen = set()
    try:
        while time.monotonic() < deadline:
            for request in sorted((session/"requests").glob("*.json")):
                if request.stem in seen:
                    continue
                seen.add(request.stem)
                print("Running job", request.stem, flush=True)
                try:
                    execute(request, session)
                except (ValueError, OSError, KeyError, RuntimeError) as error:
                    result = session/"results"/(request.stem+".json")
                    if not result.exists():
                        save(result, dict(error=str(error), job=request.stem))
                    if isinstance(error, RuntimeError):
                        raise
            time.sleep(0.25)
    except KeyboardInterrupt:
        print("Session stopped by user.", flush=True)
    finally:
        save(session/"session.json", dict(status="STOPPED", pid=os.getpid()))


if __name__ == "__main__":
    main()
