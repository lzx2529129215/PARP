#!/usr/bin/env python3
"""Log an isolated Phase2.10 QQ profile in without exposing credentials.

The credential file is read only in this process.  Secret values are passed to
xdotool over stdin, never through argv, stdout, trace markers, or scenario JSON.
"""

import argparse
import json
import os
from pathlib import Path
import stat
import subprocess
import time


LOGIN_WIDTH = 320
LOGIN_HEIGHT = 460
MAIN_MIN_WIDTH = 700
MAIN_MIN_HEIGHT = 550


def run(command, *, input_text=None):
    return subprocess.run(
        command,
        input=input_text,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )


def profile_pids(profile):
    needle = ("--user-data-dir=" + str(profile / "chromium")).encode()
    result = []
    for proc in Path("/proc").glob("[0-9]*"):
        try:
            if os.stat(proc).st_uid != os.getuid():
                continue
            if os.path.realpath(proc / "exe") != "/opt/QQ/qq":
                continue
            if needle not in (proc / "cmdline").read_bytes():
                continue
            result.append(int(proc.name))
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
    return sorted(result)


def geometry(window_id):
    completed = run(["xdotool", "getwindowgeometry", "--shell", window_id])
    if completed.returncode:
        return None
    values = {}
    for line in completed.stdout.splitlines():
        key, separator, value = line.partition("=")
        if separator and value.isdigit():
            values[key] = int(value)
    if "WIDTH" not in values or "HEIGHT" not in values:
        return None
    return values["WIDTH"], values["HEIGHT"]


def profile_windows(profile):
    windows = {}
    for pid in profile_pids(profile):
        completed = run(["xdotool", "search", "--onlyvisible", "--pid", str(pid)])
        if completed.returncode not in (0, 1):
            continue
        for window_id in completed.stdout.split():
            if not window_id.isdigit():
                continue
            size = geometry(window_id)
            if size:
                windows[window_id] = size
    return sorted(windows.items(), key=lambda item: item[1][0] * item[1][1], reverse=True)


def main_window(profile):
    for window_id, (width, height) in profile_windows(profile):
        if width >= MAIN_MIN_WIDTH and height >= MAIN_MIN_HEIGHT:
            return window_id, width, height
    return None


def wait_main_window(profile, seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        found = main_window(profile)
        if found:
            return found
        time.sleep(0.5)
    return None


def login_window(profile, seconds=30):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        candidates = profile_windows(profile)
        if candidates:
            window_id, (width, height) = candidates[0]
            if width < MAIN_MIN_WIDTH or height < MAIN_MIN_HEIGHT:
                return window_id, width, height
        time.sleep(0.5)
    return None


def click(window_id, x, y):
    run(["xdotool", "windowmap", window_id])
    run(["xdotool", "windowactivate", "--sync", window_id])
    run(["xdotool", "mousemove", "--window", window_id, str(x), str(y)])
    run(["xdotool", "click", "--clearmodifiers", "1"])


def replace_field(window_id, x, y, value):
    click(window_id, x, y)
    run(["xdotool", "key", "--window", window_id, "--clearmodifiers", "ctrl+a"])
    run(["xdotool", "key", "--window", window_id, "--clearmodifiers", "BackSpace"])
    completed = run(
        ["xdotool", "type", "--window", window_id, "--clearmodifiers", "--delay", "35", "--file", "-"],
        input_text=value,
    )
    if completed.returncode:
        raise RuntimeError("credential field input failed")


def submit_account_form(window_id, width, height, account, password):
    # Coordinates are ratios of QQ 3.2.32's fixed 320x460 login client.  Using
    # ratios keeps the interaction correct if the desktop scale changes.
    x = round(width * 0.50)
    replace_field(window_id, x, round(height * 0.37), account)
    replace_field(window_id, x, round(height * 0.49), password)
    click(window_id, round(width * 0.14), round(height * 0.79))
    click(window_id, x, round(height * 0.70))


def read_credentials(path):
    metadata = path.stat()
    if not stat.S_ISREG(metadata.st_mode):
        raise RuntimeError("credential path is not a regular file")
    if metadata.st_uid != os.getuid():
        raise RuntimeError("credential file owner does not match desktop user")
    if stat.S_IMODE(metadata.st_mode) & 0o077:
        raise RuntimeError("credential file must not grant group/other access")
    with path.open(encoding="utf-8") as stream:
        values = [line.rstrip("\r\n") for line in stream]
    if len(values) != 2 or not all(values):
        raise RuntimeError("credential file must contain exactly two non-empty lines")
    return values


def write_evidence(path, *, status, attempts, width, height):
    payload = {
        "schema_version": 1,
        "status": status,
        "authenticated_main_ui": status == "AUTHENTICATED_MAIN_UI_CONFIRMED",
        "login_attempts": attempts,
        "window_width": width,
        "window_height": height,
        "credential_values_logged": False,
        "message_sent": False,
    }
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    os.replace(temporary, path)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--credential-file", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args()

    account, password = read_credentials(args.credential_file)
    args.evidence.parent.mkdir(parents=True, exist_ok=True)
    attempts = 0
    try:
        found = wait_main_window(args.profile, 3)
        if not found:
            login = login_window(args.profile)
            if not login:
                raise RuntimeError("isolated QQ login window not found")
            window_id, width, height = login
            attempts += 1
            submit_account_form(window_id, width, height, account, password)
            found = wait_main_window(args.profile, 20)

        if not found:
            # The profile can occasionally reopen on the QR tab.  Select the
            # account/password tab and perform one bounded retry.
            login = login_window(args.profile, 5)
            if not login:
                raise RuntimeError("QQ did not expose a retryable login window")
            window_id, width, height = login
            click(window_id, round(width * 0.38), round(height * 0.92))
            time.sleep(1)
            attempts += 1
            submit_account_form(window_id, width, height, account, password)
            found = wait_main_window(args.profile, 30)

        if not found:
            raise RuntimeError("authenticated QQ main UI was not confirmed")
        _, width, height = found
        write_evidence(
            args.evidence,
            status="AUTHENTICATED_MAIN_UI_CONFIRMED",
            attempts=attempts,
            width=width,
            height=height,
        )
        print(f"AUTHENTICATED_MAIN_UI_CONFIRMED attempts={attempts} size={width}x{height}")
    except Exception as error:
        write_evidence(
            args.evidence,
            status="AUTHENTICATION_GATE_FAILED",
            attempts=attempts,
            width=0,
            height=0,
        )
        print(f"AUTHENTICATION_GATE_FAILED reason={error}")
        return 1
    finally:
        account = ""
        password = ""
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
