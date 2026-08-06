#!/usr/bin/env python3
"""只读探测语义自动化所需应用，不启动应用、不创建 scope。"""

from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
ALLOWED = {"AVAILABLE", "PARTIAL", "NOT_AVAILABLE", "BLOCKED_BY_WAYLAND", "BLOCKED_BY_LOGIN", "BLOCKED_BY_MISSING_ASSET", "NOT_EXERCISED"}
APPS = {
    "WPS": {"executables": ["wps", "wpsoffice"], "desktop": ["wps-office.desktop", "wps.desktop"], "patterns": ["wps", "kingsoft"]},
    "BROWSER": {"executables": ["firefox", "google-chrome", "chromium"], "desktop": ["firefox.desktop", "org.mozilla.firefox.desktop"], "patterns": ["firefox", "chrome", "chromium"]},
    "WECHAT": {"executables": ["wechat", "weixin", "com.tencent.wechat", "com.tencent.weixin"], "desktop": ["wechat.desktop", "weixin.desktop", "com.tencent.wechat.desktop", "com.tencent.weixin.desktop"], "patterns": ["wechat", "weixin", "微信"]},
    "BILIBILI": {"executables": ["bilibili"], "desktop": ["io.github.msojocs.bilibili.desktop", "bilibili.desktop"], "patterns": ["bilibili", "哔哩哔哩"]},
    "FILES": {"executables": ["nautilus"], "desktop": ["org.gnome.Nautilus.desktop", "nautilus.desktop"], "patterns": ["nautilus", "files", "文件"]},
}


def run(args: list[str]) -> str:
    try:
        return subprocess.run(args, text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False, timeout=5).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


def desktop_files(names: list[str]) -> list[str]:
    bases = [Path.home() / ".local/share/applications", Path("/usr/share/applications")]
    return [str(base / name) for base in bases for name in names if (base / name).exists()]


def x11_windows(patterns: list[str]) -> list[dict[str, str]]:
    if not os.environ.get("DISPLAY") or not shutil.which("xdotool"):
        return []
    rows: list[dict[str, str]] = []
    for pattern in patterns:
        for window_id in run(["xdotool", "search", "--onlyvisible", "--name", pattern]).splitlines():
            title = run(["xdotool", "getwindowname", window_id])
            rows.append({"window_id": window_id, "title": title, "match": pattern})
    return list({row["window_id"]: row for row in rows}.values())


def load_scope(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def probe(app_key: str, spec: dict[str, list[str]], scope_apps: dict[str, dict[str, Any]]) -> dict[str, Any]:
    binaries = {name: shutil.which(name) or "" for name in spec["executables"]}
    found_bins = {name: path for name, path in binaries.items() if path}
    desktops = desktop_files(spec["desktop"])
    windows = x11_windows(spec["patterns"])
    installed = bool(found_bins or desktops)
    session_type = os.environ.get("XDG_SESSION_TYPE", "unknown")
    scope_capable = bool(shutil.which("systemd-run")) and run(["systemctl", "--user", "is-system-running"]) != ""
    electron_paths: list[str] = []
    if app_key == "BILIBILI":
        for root in (Path.home() / ".local/opt/io.github.msojocs.bilibili", Path("/opt/bilibili")):
            if root.exists():
                electron_paths.extend(str(path) for path in root.glob("**/app.asar"))
    if not installed:
        status = "NOT_AVAILABLE"
        reason = "未在 PATH 或 desktop 文件中发现实际安装项"
    elif session_type == "wayland" and not os.environ.get("DISPLAY"):
        status = "BLOCKED_BY_WAYLAND"
        reason = "原生 Wayland 且没有可用 X11/Xwayland DISPLAY，xdotool 无法安全控制"
    elif not scope_capable:
        status = "PARTIAL"
        reason = "应用已发现，但 systemd --user scope 能力未确认"
    elif windows:
        status = "AVAILABLE"
        reason = "发现已安装应用及可见 X11 窗口"
    else:
        status = "PARTIAL"
        reason = "发现安装项，但本次只读探测未启动或操练窗口"
    if status not in ALLOWED:
        raise RuntimeError(status)
    return {
        "app_key": app_key,
        "status": status,
        "reason": reason,
        "installed": installed,
        "executables": found_bins,
        "desktop_files": desktops,
        "x11_windows": windows,
        "session_type": session_type,
        "display": os.environ.get("DISPLAY", ""),
        "wayland_display": os.environ.get("WAYLAND_DISPLAY", ""),
        "scope_capable": scope_capable,
        "runtime_scope_entry": scope_apps.get(app_key, {}),
        "electron_resources": electron_paths,
        "cmdline_matches": [line for line in run(["ps", "-eo", "args"]).splitlines() if any(pattern.lower() in line.lower() for pattern in spec["patterns"])][:20],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="语义自动化应用能力只读探测")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--runtime-scope-config", type=Path, default=ROOT / "configs/runtime/runtime_app_scope.json")
    parser.add_argument("--candidate-runtime-config", type=Path, default=None)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    scope = load_scope(args.runtime_scope_config)
    scope_apps = {str(item.get("app_key")): item for item in scope.get("apps", []) if isinstance(item, dict)}
    results = [probe(key, spec, scope_apps) for key, spec in APPS.items()]
    fields = ["app_key", "status", "installed", "executables", "desktop_files", "x11_windows", "session_type", "display", "wayland_display", "scope_capable", "runtime_scope_entry", "electron_resources", "reason"]
    with (args.output_dir / "app_capability_matrix.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in results:
            writer.writerow({key: json.dumps(row[key], ensure_ascii=False) if isinstance(row[key], (dict, list)) else row[key] for key in fields})
    (args.output_dir / "app_probe_details.json").write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.candidate_runtime_config:
        used_ids = {int(item.get("app_id", 0)) for item in scope.get("apps", []) if str(item.get("app_id", "")).isdigit()}
        candidates = []
        next_id = max(used_ids or {0}) + 1
        for row in results:
            if row["app_key"] in scope_apps or row["status"] not in {"AVAILABLE", "PARTIAL"}:
                continue
            candidates.append({"app_key": row["app_key"], "runtime_app_id": next_id, "model_app_id": None, "vocab_name": "", "scope_name": f"automation-{row['app_key'].lower()}.scope", "class": "|".join(APPS[row["app_key"]]["patterns"]), "cmdline_contains": APPS[row["app_key"]]["patterns"], "window_match": APPS[row["app_key"]]["patterns"], "enabled": False, "reason": "候选项；未配置模型 vocab，默认不启用 prediction"})
            next_id += 1
        args.candidate_runtime_config.parent.mkdir(parents=True, exist_ok=True)
        args.candidate_runtime_config.write_text(json.dumps({"source_config": str(args.runtime_scope_config), "candidates": candidates, "notes": "仅候选配置；不替换现有 runtime_app_scope.json。"}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"capability_matrix={args.output_dir / 'app_capability_matrix.csv'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
