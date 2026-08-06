#!/usr/bin/env python3
"""只读检查当前进程是否具备真实桌面自动化条件。"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
from pathlib import Path


def command(args: list[str]) -> tuple[int, str]:
    try:
        result = subprocess.run(args, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False, timeout=5)
        return result.returncode, result.stdout.strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 127, str(exc)


def main() -> int:
    parser = argparse.ArgumentParser(description="检查 GUI 自动化会话条件")
    parser.add_argument("--json-output", required=True, type=Path)
    parser.add_argument("--markdown-output", required=True, type=Path)
    args = parser.parse_args()
    dependencies = {name: shutil.which(name) or "" for name in ("xdotool", "wmctrl", "ydotool", "gnome-screenshot", "xprop")}
    session_type = os.environ.get("XDG_SESSION_TYPE", "")
    display = os.environ.get("DISPLAY", "")
    wayland_display = os.environ.get("WAYLAND_DISPLAY", "")
    session_id = os.environ.get("XDG_SESSION_ID", "")
    loginctl_rc, loginctl = command(["loginctl", "show-session", session_id]) if session_id else (1, "XDG_SESSION_ID 未设置")
    ps_rc, process_text = command(["ps", "-ef"])
    compositor = [line for line in process_text.splitlines() if any(token in line for token in ("Xorg", "Xwayland", "wayland"))]
    active_rc, active_window = command(["xdotool", "getactivewindow"]) if display and dependencies["xdotool"] else (1, "DISPLAY 或 xdotool 不可用")
    reasons = []
    if session_type in {"", "tty"}: reasons.append("当前进程不是图形桌面会话")
    if not display and not wayland_display: reasons.append("DISPLAY 与 WAYLAND_DISPLAY 均为空")
    if not dependencies["xdotool"]: reasons.append("缺少 xdotool")
    if not display: reasons.append("没有 X11/Xwayland DISPLAY，无法安全使用 xdotool")
    if display and active_rc != 0: reasons.append("无法读取活动窗口")
    status = "AVAILABLE" if not reasons else "GUI_SESSION_UNAVAILABLE"
    payload = {"gui_session_status": status, "xdg_session_type": session_type or "unset", "display": display, "wayland_display": wayland_display, "xdg_session_id": session_id, "loginctl": loginctl, "loginctl_rc": loginctl_rc, "compositor_processes": compositor, "dependencies": dependencies, "active_window": active_window, "active_window_rc": active_rc, "reasons": reasons}
    args.json_output.parent.mkdir(parents=True, exist_ok=True)
    args.json_output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = ["# GUI 会话预检", "", f"- GUI_SESSION_STATUS: `{status}`", f"- XDG_SESSION_TYPE: `{session_type or 'unset'}`", f"- DISPLAY: `{display or 'unset'}`", f"- WAYLAND_DISPLAY: `{wayland_display or 'unset'}`", f"- active_window: `{active_window}`", "- 原因:"] + [f"  - {reason}" for reason in reasons]
    args.markdown_output.parent.mkdir(parents=True, exist_ok=True)
    args.markdown_output.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(status)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
