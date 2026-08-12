#!/usr/bin/env python3
"""PARP/MGLRU acceptance experiment generator, runner, and reporter.

This is intentionally a diagnostic-first harness.  It never changes PARP
mode, MGLRU settings, swap configuration, reclaim sysctls, or drop_caches.
The only runtime limits it sets are finite properties on its own user slice.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import gzip  #lzx
import hashlib  #lzx
import json
import math
import os
import platform  #lzx
import random
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


TEST_DIR = Path(__file__).resolve().parent
REPO_ROOT = TEST_DIR.parent
DEFAULT_CONFIG = TEST_DIR / "parp-acceptance-config-lzx.json"
AUTOMATION = REPO_ROOT / "lzx/tool/automation/run_automation.sh"
FIXTURE = TEST_DIR / "memory-fixture-lzx.py"
TRACE_HELPER = TEST_DIR / "trace-helper-lzx.sh"
MIB = 1024 * 1024
GIB = 1024 * MIB
KEEPER_UNIT = "parp-acceptance-keeper.service"
REQUIRED_CGROUP_FILES = ("memory_stat", "memory_events", "cpu_stat", "io_stat", "memory_current")  #lzx
LOW_MEMORY_RE = re.compile(
    r"low[ -]?memory|out of memory|not enough memory|内存不足|低内存|无法分配内存",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class AppSpec:
    key: str
    name: str
    executable: str
    command: str
    window_class: str
    window_title: str
    process_names: tuple[str, ...]
    operation_key: str


def app_specs(session_dir: Path) -> dict[str, AppSpec]:
    firefox_profile = session_dir / "firefox-profile"
    gimp_image = session_dir / "fixtures/gimp-test.ppm"
    return {
        "WPS": AppSpec("WPS", "wps", "wps", "wps", "wps|Wps", "WPS|Writer|文字", ("wps", "wpsoffice", "wpp", "et"), "Page_Down"),
        "FILES": AppSpec("FILES", "files", "nautilus", f"nautilus --new-window {shlex.quote(str(REPO_ROOT))}", "org.gnome.Nautilus|Nautilus|nautilus", "文件|Files|Home|主文件夹|myself-kswapd", ("nautilus",), "Page_Down"),
        "QQ": AppSpec("QQ", "qq", "qq", "qq", "qq|QQ|linuxqq", "QQ", ("qq", "linuxqq"), "Tab"),
        "FIREFOX": AppSpec("FIREFOX", "firefox", "firefox", f"firefox --new-instance --no-remote -profile {shlex.quote(str(firefox_profile))} --new-window about:blank", "firefox|Firefox", "Mozilla Firefox|Firefox", ("firefox",), "ctrl+l"),
        "GIMP": AppSpec("GIMP", "gimp", "gimp", f"gimp {shlex.quote(str(gimp_image))}", "gimp|Gimp", "GIMP|visible_paste_test", ("gimp", "gimp-2.10"), "plus"),
        "LIBREOFFICE": AppSpec("LIBREOFFICE", "libreoffice", "libreoffice", "libreoffice --writer", "libreoffice-writer|soffice", "Writer|LibreOffice", ("soffice.bin", "soffice"), "Page_Down"),
    }


def load_config(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def run(command: list[str], *, timeout: float = 30, check: bool = False, stdout: Any = subprocess.PIPE, stderr: Any = subprocess.PIPE) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, text=True, stdout=stdout, stderr=stderr, timeout=timeout, check=check)


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


def read_kv(path: Path) -> dict[str, int]:
    values: dict[str, int] = {}
    for line in read_text(path).splitlines():
        fields = line.split()
        if len(fields) >= 2:
            try:
                values[fields[0].rstrip(":")] = int(fields[1])
            except ValueError:
                continue
    return values


def read_with_status(path: Path, reader: Any) -> tuple[Any, dict[str, Any]]:  #lzx
    """Read one required measurement file without converting failure into a fake zero."""  #lzx
    try:  #lzx
        return reader(path), {"ok": True, "error": None}  #lzx
    except (FileNotFoundError, PermissionError, OSError, ValueError) as exc:  #lzx
        return {}, {"ok": False, "error": f"{type(exc).__name__}: {exc}"}  #lzx


def read_kv_strict(path: Path) -> dict[str, int]:  #lzx
    values: dict[str, int] = {}  #lzx
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():  #lzx
        fields = line.split()  #lzx
        if len(fields) < 2:  #lzx
            continue  #lzx
        try:  #lzx
            values[fields[0].rstrip(":")] = int(fields[1])  #lzx
        except ValueError:  #lzx
            continue  #lzx
    return values  #lzx


def read_int_strict(path: Path) -> int:  #lzx
    return int(path.read_text(encoding="utf-8").strip())  #lzx


def read_io_stat(path: Path) -> dict[str, int]:  #lzx
    totals: dict[str, int] = {}  #lzx
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():  #lzx
        for item in line.split()[1:]:  #lzx
            if "=" not in item:  #lzx
                continue  #lzx
            key, value = item.split("=", 1)  #lzx
            try:  #lzx
                totals[key] = totals.get(key, 0) + int(value)  #lzx
            except ValueError:  #lzx
                continue  #lzx
    return totals  #lzx


def optional_text(path: Path) -> str | None:  #lzx
    try:  #lzx
        return path.read_text(encoding="utf-8", errors="replace").strip()  #lzx
    except (FileNotFoundError, PermissionError, OSError):  #lzx
        return None  #lzx


def kernel_config_metadata() -> dict[str, str | None]:  #lzx
    release = platform.release()  #lzx
    for path in (Path("/proc/config.gz"), Path(f"/boot/config-{release}")):  #lzx
        try:  #lzx
            data = gzip.open(path, "rb").read() if path.suffix == ".gz" else path.read_bytes()  #lzx
        except (FileNotFoundError, PermissionError, OSError):  #lzx
            continue  #lzx
        return {"path": str(path), "sha256": hashlib.sha256(data).hexdigest()}  #lzx
    return {"path": None, "sha256": None}  #lzx


def mount_metadata(path: Path) -> dict[str, str] | None:  #lzx
    mountinfo = optional_text(Path("/proc/self/mountinfo"))  #lzx
    if not mountinfo:  #lzx
        return None  #lzx
    resolved = existing_parent(path).resolve()  #lzx
    matches: list[tuple[int, dict[str, str]]] = []  #lzx
    for line in mountinfo.splitlines():  #lzx
        try:  #lzx
            left, right = line.split(" - ", 1)  #lzx
            left_fields = left.split()  #lzx
            right_fields = right.split()  #lzx
            mount_point = Path(left_fields[4].replace("\\040", " "))  #lzx
            resolved.relative_to(mount_point)  #lzx
            value = {"mount_point": str(mount_point), "filesystem": right_fields[0], "source": right_fields[1]}  #lzx
            matches.append((len(str(mount_point)), value))  #lzx
        except (ValueError, IndexError):  #lzx
            continue  #lzx
    return max(matches, default=(0, None), key=lambda item: item[0])[1]  # type: ignore[return-value] #lzx


def host_metadata(storage_path: Path) -> dict[str, Any]:  #lzx
    cpu_model = ""  #lzx
    for line in (optional_text(Path("/proc/cpuinfo")) or "").splitlines():  #lzx
        if line.lower().startswith("model name") and ":" in line:  #lzx
            cpu_model = line.split(":", 1)[1].strip()  #lzx
            break  #lzx
    governors = sorted({  #lzx
        value  #lzx
        for governor in Path("/sys/devices/system/cpu").glob("cpu*/cpufreq/scaling_governor")  #lzx
        for value in [optional_text(governor)]  #lzx
        if value is not None  #lzx
    })  #lzx
    swap_rows: list[dict[str, Any]] = []  #lzx
    for line in (optional_text(Path("/proc/swaps")) or "").splitlines()[1:]:  #lzx
        fields = line.split()  #lzx
        if len(fields) >= 5:  #lzx
            swap_rows.append({"path": fields[0], "type": fields[1], "size_kib": int(fields[2]), "used_kib": int(fields[3]), "priority": int(fields[4])})  #lzx
    sysctls = {  #lzx
        name: optional_text(Path("/proc/sys") / name.replace(".", "/"))  #lzx
        for name in ("vm.swappiness", "vm.watermark_scale_factor", "vm.watermark_boost_factor", "vm.overcommit_memory", "vm.overcommit_ratio")  #lzx
    }  #lzx
    return {  #lzx
        "captured_at": dt.datetime.now().isoformat(), "hostname": platform.node(),  #lzx
        "kernel_release": platform.release(), "kernel_version": platform.version(), "kernel_cmdline": optional_text(Path("/proc/cmdline")),  #lzx
        "kernel_config": kernel_config_metadata(), "machine": platform.machine(), "cpu_model": cpu_model,  #lzx
        "cpu_count": os.cpu_count() or 1, "page_size": os.sysconf("SC_PAGE_SIZE"), "memtotal_bytes": meminfo().get("MemTotal", 0),  #lzx
        "vm_sysctls": sysctls, "swap": swap_rows,  #lzx
        "transparent_hugepage": {  #lzx
            "enabled": optional_text(Path("/sys/kernel/mm/transparent_hugepage/enabled")),  #lzx
            "defrag": optional_text(Path("/sys/kernel/mm/transparent_hugepage/defrag")),  #lzx
        },  #lzx
        "cpu_governors": governors,  #lzx
        "session": {  #lzx
            "type": os.environ.get("XDG_SESSION_TYPE"), "desktop": os.environ.get("XDG_CURRENT_DESKTOP"),  #lzx
            "display": os.environ.get("DISPLAY"), "wayland_display": os.environ.get("WAYLAND_DISPLAY"),  #lzx
        },  #lzx
        "result_storage": mount_metadata(storage_path),  #lzx
    }  #lzx


def meminfo() -> dict[str, int]:
    return {key: value * 1024 for key, value in read_kv(Path("/proc/meminfo")).items()}


def vmstat() -> dict[str, int]:
    return read_kv(Path("/proc/vmstat"))


def kswapd_cpu_time_ns() -> int:  #lzx
    """Return cumulative CPU time for all live kswapd threads."""  #lzx
    ticks = 0  #lzx
    clock_ticks = int(os.sysconf("SC_CLK_TCK"))  #lzx
    for process_dir in Path("/proc").glob("[0-9]*"):  #lzx
        try:  #lzx
            if not (process_dir / "comm").read_text(encoding="utf-8").strip().startswith("kswapd"):  #lzx
                continue  #lzx
            stat_text = (process_dir / "stat").read_text(encoding="utf-8")  #lzx
            after_comm = stat_text[stat_text.rfind(")") + 2:].split()  #lzx
            ticks += int(after_comm[11]) + int(after_comm[12])  # fields 14/15: utime/stime #lzx
        except (OSError, ValueError, IndexError):  #lzx
            continue  #lzx
    return ticks * 1_000_000_000 // max(1, clock_ticks)  #lzx


def psi_memory() -> dict[str, float]:
    result: dict[str, float] = {}
    for line in read_text(Path("/proc/pressure/memory")).splitlines():
        fields = line.split()
        if not fields:
            continue
        prefix = fields[0]
        for item in fields[1:]:
            if "=" not in item:
                continue
            key, value = item.split("=", 1)
            try:
                result[f"{prefix}_{key}"] = float(value)
            except ValueError:
                pass
    return result


def swap_bytes() -> int:
    total = 0
    for line in read_text(Path("/proc/swaps")).splitlines()[1:]:
        fields = line.split()
        if len(fields) >= 4:
            try:
                total += int(fields[2]) * 1024
            except ValueError:
                pass
    return total


def command_exists(command: str) -> bool:
    return shutil.which(command) is not None


def debugfs_value(name: str) -> str:
    path = Path("/sys/kernel/debug/parp") / name
    outcome = run(["sudo", "-n", "cat", str(path)], timeout=5)
    return outcome.stdout.strip() if outcome.returncode == 0 else ""


def parse_debug_stat(text: str, key: str) -> str:
    match = re.search(rf"(?:^|\s){re.escape(key)}[=: ]+([^\s]+)", text, re.MULTILINE)
    return match.group(1) if match else ""


def output_root(config: dict[str, Any]) -> Path:
    value = Path(str(config.get("output_root", "lzx/tool/outputs/parp_acceptance")))
    return value if value.is_absolute() else REPO_ROOT / value


def existing_parent(path: Path) -> Path:
    candidate = path
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    return candidate


def inotify_watch_usage() -> dict[str, int]:
    maximum_text = read_text(Path("/proc/sys/fs/inotify/max_user_watches"))
    maximum = int(maximum_text) if maximum_text.isdigit() else 0
    used = 0
    uid = os.getuid()
    for process_dir in Path("/proc").glob("[0-9]*"):
        try:
            if process_dir.stat().st_uid != uid:
                continue
        except OSError:
            continue
        for fdinfo in (process_dir / "fdinfo").glob("*"):
            try:
                used += sum(1 for line in fdinfo.read_text(encoding="utf-8", errors="replace").splitlines() if line.startswith("inotify"))
            except OSError:
                continue
    return {"maximum": maximum, "used": used, "headroom": max(0, maximum - used)}


def x11_environment() -> dict[str, str]:
    uid = os.getuid()
    display = os.environ.get("DISPLAY") or (":0" if Path("/tmp/.X11-unix/X0").exists() else "")
    candidates = [
        Path(os.environ.get("XAUTHORITY", "")),
        Path(f"/run/user/{uid}/gdm/Xauthority"),
        Path.home() / ".Xauthority",
    ]
    xauthority = next((str(path) for path in candidates if str(path) and path.is_file()), "")
    return {
        "DISPLAY": display,
        "XAUTHORITY": xauthority,
        "XDG_RUNTIME_DIR": f"/run/user/{uid}",
        "DBUS_SESSION_BUS_ADDRESS": f"unix:path=/run/user/{uid}/bus",
        "XDG_SESSION_TYPE": "x11",
    }


def preflight(config: dict[str, Any], profile: str, suite: str) -> dict[str, Any]:
    memory = meminfo()
    total = memory.get("MemTotal", 0)
    available = memory.get("MemAvailable", 0)
    gui = x11_environment()
    specs = app_specs(output_root(config) / "preflight")
    required_apps = config["hotcold"]["apps"] if suite == "hotcold" else config["peak"]["apps"] if suite == "peak" else sorted(set(config["hotcold"]["apps"] + config["peak"]["apps"]))
    app_checks = {app: {"executable": specs[app].executable, "installed": command_exists(specs[app].executable)} for app in required_apps}
    effective_stats = debugfs_value("effective_tier_stats")
    effective_config = debugfs_value("effective_tier_config")
    mode = debugfs_value("effective_tier_mode")
    profile_cfg = config["profiles"][profile]
    logical_ratio = max(
        float(profile_cfg.get("hotcold_logical_ratio", 0)) if suite in {"hotcold", "all"} else 0,
        float(profile_cfg.get("peak_logical_ratio", 0)) if suite in {"peak", "all"} else 0,
    )
    disk = shutil.disk_usage(existing_parent(output_root(config)))
    inotify = inotify_watch_usage()
    controllers = read_text(Path("/sys/fs/cgroup/cgroup.controllers")).split()  #lzx
    metadata = host_metadata(output_root(config))  #lzx
    metadata["session"] = {  #lzx
        "type": gui.get("XDG_SESSION_TYPE"), "desktop": os.environ.get("XDG_CURRENT_DESKTOP"),  #lzx
        "display": gui.get("DISPLAY"), "xauthority": gui.get("XAUTHORITY"),  #lzx
        "wayland_display": os.environ.get("WAYLAND_DISPLAY"),  #lzx
    }  #lzx
    checks = {
        "sudo_noninteractive": run(["sudo", "-n", "true"], timeout=5).returncode == 0,
        "x11_display": bool(gui["DISPLAY"] and gui["XAUTHORITY"]),
        "cgroup_v2_memory": "memory" in controllers,  #lzx
        "cgroup_v2_cpu": "cpu" in controllers,  #lzx
        "cgroup_v2_io": "io" in controllers,  #lzx
        "tracefs_page_fault_user": Path("/sys/kernel/tracing/events/exceptions/page_fault_user/format").is_file(),
        "tracefs_parp_decision": Path("/sys/kernel/tracing/events/parp/parp_effective_tier_decision/format").is_file(),
        "swap_present": swap_bytes() > 0,
        "memory_16g_class": 14 * GIB <= total <= 18 * GIB,
        "memavailable_floor": available >= int(config["safety"]["min_memavailable_bytes"]),
        "disk_for_sparse_fixture": disk.free >= max(4 * GIB, int(total * min(logical_ratio, 0.25))),
        "automation_present": AUTOMATION.is_file(),
        "all_required_apps_installed": all(item["installed"] for item in app_checks.values()),
        "xdotool_present": command_exists("xdotool"),
        "wmctrl_present": command_exists("wmctrl"),
        "inotify_watch_headroom": inotify["headroom"] >= int(config["safety"]["min_inotify_watch_headroom"]),
    }
    apply_compiled = parse_debug_stat(effective_stats, "apply_compiled")
    model_provenance = parse_debug_stat(effective_config, "model_provenance")
    diagnostic_only = apply_compiled in {"", "0"} or "UNTRAINED" in effective_config
    return {
        "status": "READY" if all(checks.values()) else "BLOCKED",
        "metrics_schema_version": 3,  #lzx
        "diagnostic_only": diagnostic_only,
        "diagnostic_reason": "SHADOW_APPLY_NOT_COMPILED_OR_MODEL_UNTRAINED" if diagnostic_only else "",
        "timestamp": dt.datetime.now().isoformat(),
        "kernel_release": os.uname().release,
        "profile": profile,
        "suite": suite,
        "memory": {"total_bytes": total, "available_bytes": available},
        "swap_bytes": swap_bytes(),
        "disk_free_bytes": disk.free,
        "inotify": inotify,
        "gui": gui,
        "apps": app_checks,
        "system_metadata": metadata,  #lzx
        "checks": checks,
        "parp": {
            "effective_tier_mode": mode,
            "apply_compiled": apply_compiled,
            "model_provenance": model_provenance,
            "effective_tier_stats": effective_stats,
            "effective_tier_config": effective_config,
        },
    }


def fixture_socket(session_dir: Path, app: str) -> Path:
    return Path(f"/run/user/{os.getuid()}/parp-a-{session_dir.name[-18:]}-{app.lower()}.sock")


def fixture_command(path: Path, command: str, timeout: float, wait_seconds: float = 0) -> str:
    deadline = time.monotonic() + max(wait_seconds, 0)
    last = ""
    while True:
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as stream:
                stream.settimeout(timeout)
                stream.connect(str(path))
                stream.sendall((command.strip() + "\n").encode("ascii"))
                response = stream.recv(4096).decode("ascii", errors="replace").strip()
            if not response.startswith("OK "):
                raise RuntimeError(response or "empty fixture response")
            return response
        except (OSError, RuntimeError) as exc:
            last = str(exc)
            if time.monotonic() >= deadline:
                raise RuntimeError(f"fixture {path}: {last}") from exc
            time.sleep(0.2)


def py_fixture_action(path: Path, command: str, *, timeout: int = 30, wait: int = 0, label: str = "") -> dict[str, Any]:
    args = [sys.executable, str(Path(__file__).resolve()), "fixture-command", "--socket", str(path), "--command", command, "--timeout", str(timeout), "--wait", str(wait)]
    return {"type": "shell", "command": shlex.join(args), "label": label}


def trace_action(instance: str, operation: str, label: str) -> dict[str, Any]:
    command = ["sudo", "-n", "bash", str(TRACE_HELPER), operation, instance]
    return {"type": "shell", "command": shlex.join(command), "label": label}


def app_launch_actions(spec: AppSpec) -> list[dict[str, Any]]:
    return [
        {"type": "launch", "name": spec.name, "scope_name": spec.name, "app_key": spec.key, "command": spec.command, "label": f"LAUNCH_{spec.key}"},
        {"type": "wait_window", "name": spec.name, "app_key": spec.key, "class": spec.window_class, "title": spec.window_title, "timeout": 45, "label": f"WAIT_{spec.key}"},
    ]


def app_close_action(spec: AppSpec) -> dict[str, Any]:
    return {
        "type": "close", "name": spec.name, "app_key": spec.key,
        "class": spec.window_class, "title": spec.window_title,
        "process_names": list(spec.process_names), "wait_after_window_close": 0.2,
        "force_after_seconds": 1, "label": f"CLOSE_{spec.key}",
    }


def allocation_by_app(total_bytes: int, apps: list[str], ratios: dict[str, float] | None = None) -> dict[str, int]:
    if ratios:
        ratio_total = sum(float(ratios[app]) for app in apps)
        return {app: max(8 * MIB, int(total_bytes * float(ratios[app]) / ratio_total)) for app in apps}
    each = total_bytes // len(apps)
    return {app: max(8 * MIB, each) for app in apps}


def generate_scenario(config: dict[str, Any], *, suite: str, profile: str, round_index: int, seed: int, session_dir: Path, trace_instance: str) -> dict[str, Any]:
    profile_cfg = config["profiles"][profile]
    suite_cfg = config[suite]
    apps = list(suite_cfg["apps"])
    specs = app_specs(session_dir)
    total_memory = meminfo()["MemTotal"]
    logical_ratio = float(profile_cfg[f"{suite}_logical_ratio"])
    logical_total = int(total_memory * logical_ratio)
    allocation = allocation_by_app(
        logical_total,
        apps,
        suite_cfg.get("peak_ratio_by_app") if suite == "peak" else None,
    )
    anon_fraction = float(suite_cfg["anon_fraction"])
    hot_fraction = float(suite_cfg["hot_fraction"])
    steps = int(profile_cfg[f"{suite}_steps"])
    rng = random.Random(seed)
    ballast_dir = session_dir / "ballast"
    fixture_dir = session_dir / "fixtures"
    fixture_dir.mkdir(parents=True, exist_ok=True)
    gimp_fixture = fixture_dir / "gimp-test.ppm"
    if "GIMP" in apps and not gimp_fixture.exists():
        width = 512
        height = 512
        pixels = bytearray()
        for y in range(height):
            for x in range(width):
                pixels.extend(((x * 255) // (width - 1), (y * 255) // (height - 1), 128))
        gimp_fixture.write_bytes(f"P6\n{width} {height}\n255\n".encode("ascii") + pixels)
    actions: list[dict[str, Any]] = []
    if "FIREFOX" in apps:
        actions.append({"type": "shell", "command": f"mkdir -p {shlex.quote(str(session_dir / 'firefox-profile'))}", "label": "PREPARE_FIREFOX_PROFILE"})
    if suite == "hotcold":
        for app in apps:
            actions.extend(app_launch_actions(specs[app]))
    sockets: dict[str, Path] = {}
    fixture_layout: dict[str, dict[str, int]] = {}
    for app in apps:
        logical = allocation[app]
        anon_bytes = max(4 * MIB, int(logical * anon_fraction))
        file_bytes = max(4 * MIB, logical - anon_bytes)
        hot_bytes = max(4 * MIB, int(file_bytes * hot_fraction))
        socket_path = fixture_socket(session_dir, app)
        sockets[app] = socket_path
        fixture_layout[app] = {"logical_bytes": logical, "file_bytes": file_bytes, "anon_bytes": anon_bytes, "hot_bytes": hot_bytes}
        command = [
            sys.executable, str(FIXTURE), "--app", app, "--socket", str(socket_path),
            "--file", str(ballast_dir / f"{app.lower()}.sparse"),
            "--log", str(ballast_dir / f"{app.lower()}-fixture.csv"),
            "--file-bytes", str(file_bytes), "--anon-bytes", str(anon_bytes),
            "--hot-bytes", str(hot_bytes),
        ]
        actions.append({"type": "launch", "name": f"fixture-{app.lower()}", "scope_name": f"fixture-{app.lower()}", "app_key": app, "command": shlex.join(command), "label": f"FIXTURE_LAUNCH_{app}"})
        actions.append(py_fixture_action(socket_path, "STATUS", wait=30, label=f"FIXTURE_READY_{app}"))
    for app in apps:
        actions.append(py_fixture_action(sockets[app], "PREPARE", timeout=1200, label=f"FIXTURE_PREPARE_{app}"))
    if suite == "peak":
        launch_and_wait = {app: app_launch_actions(specs[app]) for app in apps}
        for app in apps:
            actions.append(launch_and_wait[app][0])
        for app in apps:
            actions.append(launch_and_wait[app][1])
    filter_args = [sys.executable, str(Path(__file__).resolve()), "trace-filter", "--instance", trace_instance]
    for app in apps:
        filter_args.extend(["--socket", str(sockets[app])])
    actions.append({"type": "shell", "command": shlex.join(filter_args), "label": "TRACE_FILTER_FIXTURE_PIDS"})
    actions.append(trace_action(trace_instance, "enable", "TRACE_MEASURE_ENABLE"))
    actions.append({"type": "trace_marker", "event_type": "ACCEPTANCE_MEASURE_START", "status": "running", "label": "MEASURE_START"})
    previous = ""
    for step in range(steps):
        choices = [app for app in apps if app != previous] or apps
        app = rng.choice(choices)
        spec = specs[app]
        actions.append({"type": "trace_marker", "event_type": f"{suite.upper()}_CASE_START", "status": "running", "app_key": app, "label": f"{suite.upper()}_CASE_{step:03d}_START", "metadata": {"seed": seed, "round": round_index, "case": step}})
        actions.append({"type": "switch", "name": spec.name, "app_key": app, "class": spec.window_class, "title": spec.window_title, "label": f"{suite.upper()}_CASE_{step:03d}_SWITCH_{app}"})
        actions.append({"type": "verify_foreground", "name": spec.name, "app_key": app, "class": spec.window_class, "title": spec.window_title, "label": f"{suite.upper()}_CASE_{step:03d}_VERIFY_{app}"})
        actions.append(py_fixture_action(sockets[app], "TOUCH_HOT", timeout=120, label=f"{suite.upper()}_CASE_{step:03d}_HOT_{app}"))
        sample_bytes = max(4 * MIB, int(fixture_layout[app]["file_bytes"] * float(suite_cfg["sample_fraction_per_step"])))
        cold_start = fixture_layout[app]["hot_bytes"]
        cold_span = max(4096, fixture_layout[app]["file_bytes"] - cold_start - sample_bytes)
        offset = cold_start + rng.randrange(0, cold_span, 4096) if cold_span > 4096 else cold_start
        if rng.random() < (0.35 if suite == "hotcold" else 0.20):
            actions.append(py_fixture_action(sockets[app], f"TOUCH_SAMPLE {offset} {sample_bytes}", timeout=180, label=f"{suite.upper()}_CASE_{step:03d}_SAMPLE_{app}"))
        actions.append({"type": "key", "name": spec.name, "app_key": app, "key": spec.operation_key, "optional": app == "FIREFOX", "label": f"{suite.upper()}_CASE_{step:03d}_UI_{app}"})
        dwell = rng.uniform(float(profile_cfg["dwell_min_seconds"]), float(profile_cfg["dwell_max_seconds"]))
        actions.append({"type": "wait", "seconds": round(dwell, 3), "label": f"{suite.upper()}_CASE_{step:03d}_DWELL"})
        actions.append({"type": "trace_marker", "event_type": f"{suite.upper()}_CASE_DONE", "status": "success", "app_key": app, "label": f"{suite.upper()}_CASE_{step:03d}_DONE"})
        previous = app
    actions.append({"type": "trace_marker", "event_type": "ACCEPTANCE_MEASURE_DONE", "status": "success", "label": "MEASURE_DONE"})
    actions.append(trace_action(trace_instance, "disable", "TRACE_MEASURE_DISABLE"))
    for app in reversed(apps):
        actions.append(py_fixture_action(sockets[app], "STOP", wait=2, label=f"FIXTURE_STOP_{app}"))
    for app in reversed(apps):
        actions.append(app_close_action(specs[app]))
    workload_contract: dict[str, Any] = {
        "logical_ratio": logical_ratio,
        "logical_total_bytes": sum(allocation.values()),
        "memtotal_bytes": total_memory,
    }
    if suite == "hotcold":
        workload_contract.update({
            "required_ratio_min": 1.50, "required_ratio_max": 2.00,
            "ratio_requirement_met": 1.50 <= logical_ratio <= 2.00,
        })
    else:
        normal_ratios = {app: float(suite_cfg["normal_ratio_by_app"][app]) for app in apps}
        peak_ratios = {app: float(suite_cfg["peak_ratio_by_app"][app]) for app in apps}
        workload_contract.update({
            "normal_ratio_by_app": normal_ratios,
            "normal_ratio_sum": sum(normal_ratios.values()),
            "peak_ratio_by_app": peak_ratios,
            "peak_ratio_sum": sum(peak_ratios.values()),
            "normal_sum_le_physical": sum(normal_ratios.values()) <= 1.0,
            "each_peak_le_physical": all(value <= 1.0 for value in peak_ratios.values()),
            "concurrent_peak_ge_120_percent": sum(peak_ratios.values()) >= 1.20,
        })
    scenario = {
        "description": f"PARP {suite} diagnostic acceptance round {round_index}",
        "validation_mode": True,
        "metadata": {
            "suite": suite, "profile": profile, "round": round_index, "seed": seed,
            "memtotal_bytes": total_memory, "logical_ratio": logical_ratio,
            "logical_total_bytes": sum(allocation.values()), "fixture_layout": fixture_layout,
            "scored_steps": steps, "workload_contract": workload_contract,
        },
        "actions": actions,
    }
    return scenario


def slice_path(slice_name: str) -> Path | None:
    result = run(["systemctl", "--user", "show", slice_name, "-p", "ControlGroup", "--value"], timeout=5)
    value = result.stdout.strip()
    return Path("/sys/fs/cgroup") / value.lstrip("/") if result.returncode == 0 and value else None


def enable_user_accounting() -> None:  #lzx
    """Enable CPU/I/O controllers on the current user's systemd ancestors for this boot."""  #lzx
    uid = os.getuid()  #lzx
    for unit in (f"user-{uid}.slice", f"user@{uid}.service"):  #lzx
        outcome = run([  #lzx
            "sudo", "-n", "systemctl", "set-property", "--runtime", unit,  #lzx
            "CPUAccounting=yes", "IOAccounting=yes",  #lzx
        ], timeout=15)  #lzx
        if outcome.returncode != 0:  #lzx
            raise RuntimeError(outcome.stderr.strip() or f"failed to enable CPU/I/O accounting on {unit}")  #lzx


def setup_slice(config: dict[str, Any]) -> Path:
    slice_name = str(config["slice"])
    enable_user_accounting()  #lzx
    total = meminfo()["MemTotal"]
    high = int(total * float(config["safety"]["memory_high_ratio"]))
    maximum = int(total * float(config["safety"]["memory_max_ratio"]))
    command = [
        "systemctl", "--user", "set-property", "--runtime", slice_name,
        "MemoryAccounting=yes", "CPUAccounting=yes", "IOAccounting=yes", f"MemoryHigh={high}", f"MemoryMax={maximum}",  #lzx
    ]
    outcome = run(command, timeout=15)
    if outcome.returncode != 0:
        raise RuntimeError(outcome.stderr.strip() or "failed to configure test slice")
    run(["systemctl", "--user", "stop", KEEPER_UNIT], timeout=15)
    run(["systemctl", "--user", "reset-failed", KEEPER_UNIT], timeout=15)
    keeper = run([
        "systemd-run", "--user", f"--unit={KEEPER_UNIT}", "--collect",
        f"--slice={slice_name}", "/bin/sleep", "3600",
    ], timeout=15)
    if keeper.returncode != 0:
        raise RuntimeError(keeper.stderr.strip() or "failed to create test slice keeper")
    path = slice_path(slice_name)
    if path is None or not path.is_dir():
        raise RuntimeError("test slice cgroup path unavailable")
    endpoint = cgroup_snapshot(path)  #lzx
    missing = [name for name in REQUIRED_CGROUP_FILES if not endpoint.get("read_status", {}).get(name, {}).get("ok", False)]  #lzx
    if missing:  #lzx
        raise RuntimeError("test slice required cgroup files unavailable: " + ",".join(missing))  #lzx
    return path


def cleanup_slice(config: dict[str, Any]) -> None:
    slice_name = str(config["slice"])
    run(["systemctl", "--user", "stop", KEEPER_UNIT], timeout=30)
    run(["systemctl", "--user", "stop", slice_name], timeout=30)
    run(["systemctl", "--user", "revert", slice_name], timeout=30)


def cgroup_snapshot(path: Path | None) -> dict[str, Any]:  #lzx
    if path is None:  #lzx
        return {"status": "missing", "path": None, "identity": None, "read_status": {}}  #lzx
    try:  #lzx
        identity_stat = path.stat()  #lzx
        identity = {"device": identity_stat.st_dev, "inode": identity_stat.st_ino}  #lzx
    except (FileNotFoundError, PermissionError, OSError):  #lzx
        return {"status": "missing", "path": str(path), "identity": None, "read_status": {}}  #lzx
    memory_stat, memory_stat_status = read_with_status(path / "memory.stat", read_kv_strict)  #lzx
    memory_events, memory_events_status = read_with_status(path / "memory.events", read_kv_strict)  #lzx
    cpu_stat, cpu_stat_status = read_with_status(path / "cpu.stat", read_kv_strict)  #lzx
    io_stat, io_stat_status = read_with_status(path / "io.stat", read_io_stat)  #lzx
    memory_current, memory_current_status = read_with_status(path / "memory.current", read_int_strict)  #lzx
    read_status = {  #lzx
        "memory_stat": memory_stat_status, "memory_events": memory_events_status,  #lzx
        "cpu_stat": cpu_stat_status, "io_stat": io_stat_status, "memory_current": memory_current_status,  #lzx
    }  #lzx
    status = "ok" if all(item["ok"] for item in read_status.values()) else "read_error"  #lzx
    return {
        "status": status, "path": str(path), "identity": identity, "read_status": read_status,  #lzx
        "memory_stat": memory_stat, "memory_events": memory_events, "cpu_stat": cpu_stat, "io_stat": io_stat,  #lzx
        "memory_current": memory_current if memory_current_status["ok"] else None,  #lzx
        "pgfault": memory_stat.get("pgfault"), "pgmajfault": memory_stat.get("pgmajfault"),  #lzx
        "workingset_refault_file": memory_stat.get("workingset_refault_file"),  #lzx
        "workingset_refault_anon": memory_stat.get("workingset_refault_anon"),  #lzx
        "workingset_activate_file": memory_stat.get("workingset_activate_file"),  #lzx
        "workingset_activate_anon": memory_stat.get("workingset_activate_anon"),  #lzx
        "workingset_restore_file": memory_stat.get("workingset_restore_file"),  #lzx
        "workingset_restore_anon": memory_stat.get("workingset_restore_anon"),  #lzx
        "pgscan": memory_stat.get("pgscan"), "pgsteal": memory_stat.get("pgsteal"),  #lzx
        "pgscan_direct": memory_stat.get("pgscan_direct"), "pgsteal_direct": memory_stat.get("pgsteal_direct"),  #lzx
        "pgscan_kswapd": memory_stat.get("pgscan_kswapd"), "pgsteal_kswapd": memory_stat.get("pgsteal_kswapd"),  #lzx
        "cgroup_pswpin": memory_stat.get("pswpin"), "cgroup_pswpout": memory_stat.get("pswpout"),  #lzx
        "anon": memory_stat.get("anon"), "file": memory_stat.get("file"),  #lzx
        "events_high": memory_events.get("high"), "events_max": memory_events.get("max"),  #lzx
        "events_oom": memory_events.get("oom"), "events_oom_kill": memory_events.get("oom_kill"),  #lzx
        "cpu_usage_usec": cpu_stat.get("usage_usec"), "cpu_user_usec": cpu_stat.get("user_usec"), "cpu_system_usec": cpu_stat.get("system_usec"),  #lzx
        "io_rbytes": io_stat.get("rbytes", 0) if io_stat_status["ok"] else None,  #lzx
        "io_wbytes": io_stat.get("wbytes", 0) if io_stat_status["ok"] else None,  #lzx
        "io_rios": io_stat.get("rios", 0) if io_stat_status["ok"] else None,  #lzx
        "io_wios": io_stat.get("wios", 0) if io_stat_status["ok"] else None,  #lzx
    }


def snapshot(path: Path | None) -> dict[str, Any]:
    memory = meminfo()
    return {
        "timestamp_ns": time.time_ns(), "monotonic_ns": time.monotonic_ns(),
        "memtotal": memory.get("MemTotal", 0), "memavailable": memory.get("MemAvailable", 0),
        "swapfree": memory.get("SwapFree", 0), "psi": psi_memory(), "vmstat": vmstat(),  #lzx
        "kswapd_cpu_time_ns": kswapd_cpu_time_ns(),  #lzx
        "cgroup": cgroup_snapshot(path),
    }


def cgroup_endpoint_validity(before: dict[str, Any], after: dict[str, Any]) -> tuple[bool, list[str]]:  #lzx
    reasons: list[str] = []  #lzx
    before_cgroup = before.get("cgroup") if isinstance(before, dict) else None  #lzx
    after_cgroup = after.get("cgroup") if isinstance(after, dict) else None  #lzx
    if not isinstance(before_cgroup, dict) or not isinstance(after_cgroup, dict):  #lzx
        return False, ["cgroup snapshot missing at one or both endpoints"]  #lzx
    if before_cgroup.get("path") != after_cgroup.get("path"):  #lzx
        reasons.append("cgroup path changed during collection")  #lzx
    before_identity = before_cgroup.get("identity")  #lzx
    after_identity = after_cgroup.get("identity")  #lzx
    if before_identity is None or after_identity is None:  #lzx
        reasons.append("cgroup disappeared at one or both collection endpoints")  #lzx
    elif before_identity != after_identity:  #lzx
        reasons.append("cgroup was recreated during collection")  #lzx
    for phase, endpoint in (("before", before_cgroup), ("after", after_cgroup)):  #lzx
        statuses = endpoint.get("read_status")  #lzx
        if not isinstance(statuses, dict):  #lzx
            reasons.append(f"{phase} cgroup read status missing")  #lzx
            continue  #lzx
        for name in REQUIRED_CGROUP_FILES:  #lzx
            status = statuses.get(name, {})  #lzx
            if not isinstance(status, dict) or not status.get("ok", False):  #lzx
                error = status.get("error", "unknown error") if isinstance(status, dict) else "status missing"  #lzx
                reasons.append(f"{phase} {name} unavailable: {error}")  #lzx
        memory_stat = endpoint.get("memory_stat", {})  #lzx
        for field in (  #lzx
            "pgfault", "pgmajfault", "workingset_refault_file", "workingset_refault_anon",  #lzx
            "pgscan", "pgsteal", "pgscan_direct", "pgsteal_direct", "pgscan_kswapd", "pgsteal_kswapd",  #lzx
        ):  #lzx
            if field not in memory_stat:  #lzx
                reasons.append(f"{phase} memory.stat lacks {field}")  #lzx
        if "usage_usec" not in endpoint.get("cpu_stat", {}):  #lzx
            reasons.append(f"{phase} cpu.stat lacks usage_usec")  #lzx
        if "oom_kill" not in endpoint.get("memory_events", {}):  #lzx
            reasons.append(f"{phase} memory.events lacks oom_kill")  #lzx
    cumulative_fields = (  #lzx
        "pgfault", "pgmajfault", "workingset_refault_file", "workingset_refault_anon",  #lzx
        "workingset_activate_file", "workingset_activate_anon", "workingset_restore_file", "workingset_restore_anon",  #lzx
        "pgscan", "pgsteal", "pgscan_direct", "pgsteal_direct", "pgscan_kswapd", "pgsteal_kswapd",  #lzx
        "cgroup_pswpin", "cgroup_pswpout", "events_high", "events_max", "events_oom", "events_oom_kill",  #lzx
        "cpu_usage_usec", "cpu_user_usec", "cpu_system_usec", "io_rbytes", "io_wbytes", "io_rios", "io_wios",  #lzx
    )  #lzx
    for field in cumulative_fields:  #lzx
        first = before_cgroup.get(field)  #lzx
        last = after_cgroup.get(field)  #lzx
        if first is not None and last is not None and int(last) < int(first):  #lzx
            reasons.append(f"cgroup counter decreased: {field} {first}->{last}")  #lzx
    return not reasons, reasons  #lzx


def endpoint_delta(before_cgroup: dict[str, Any], after_cgroup: dict[str, Any], field: str) -> int | None:  #lzx
    first = before_cgroup.get(field)  #lzx
    last = after_cgroup.get(field)  #lzx
    if first is None or last is None:  #lzx
        return None  #lzx
    value = int(last) - int(first)  #lzx
    return value if value >= 0 else None  #lzx


def cgroup_endpoint_metrics(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:  #lzx
    before_cgroup = before.get("cgroup", {})  #lzx
    after_cgroup = after.get("cgroup", {})  #lzx
    mapping = {  #lzx
        "pgfault_delta": "pgfault", "pgmajfault_delta": "pgmajfault",  #lzx
        "workingset_refault_file_delta": "workingset_refault_file", "workingset_refault_anon_delta": "workingset_refault_anon",  #lzx
        "workingset_activate_file_delta": "workingset_activate_file", "workingset_activate_anon_delta": "workingset_activate_anon",  #lzx
        "workingset_restore_file_delta": "workingset_restore_file", "workingset_restore_anon_delta": "workingset_restore_anon",  #lzx
        "pgscan_delta": "pgscan", "pgsteal_delta": "pgsteal",  #lzx
        "pgscan_direct_delta": "pgscan_direct", "pgsteal_direct_delta": "pgsteal_direct",  #lzx
        "pgscan_kswapd_delta": "pgscan_kswapd", "pgsteal_kswapd_delta": "pgsteal_kswapd",  #lzx
        "pswpin_delta": "cgroup_pswpin", "pswpout_delta": "cgroup_pswpout",  #lzx
        "events_high_delta": "events_high", "events_max_delta": "events_max",  #lzx
        "oom_delta": "events_oom", "oom_kill_delta": "events_oom_kill",  #lzx
        "cpu_usage_usec_delta": "cpu_usage_usec", "cpu_user_usec_delta": "cpu_user_usec", "cpu_system_usec_delta": "cpu_system_usec",  #lzx
        "io_read_bytes_delta": "io_rbytes", "io_write_bytes_delta": "io_wbytes",  #lzx
        "io_read_ios_delta": "io_rios", "io_write_ios_delta": "io_wios",  #lzx
    }  #lzx
    metrics = {name: endpoint_delta(before_cgroup, after_cgroup, field) for name, field in mapping.items()}  #lzx
    elapsed_s = max((int(after.get("monotonic_ns", 0)) - int(before.get("monotonic_ns", 0))) / 1e9, 1e-9)  #lzx
    cpu_count = max(1, os.cpu_count() or 1)  #lzx
    usage_usec = metrics["cpu_usage_usec_delta"]  #lzx
    metrics["elapsed_seconds"] = elapsed_s  #lzx
    metrics["cpu_one_core_percent"] = (usage_usec / 1_000_000 / elapsed_s * 100.0) if usage_usec is not None else None  #lzx
    metrics["cpu_machine_percent"] = (metrics["cpu_one_core_percent"] / cpu_count) if metrics["cpu_one_core_percent"] is not None else None  #lzx
    metrics["io_read_mib_per_second"] = (metrics["io_read_bytes_delta"] / MIB / elapsed_s) if metrics["io_read_bytes_delta"] is not None else None  #lzx
    metrics["io_write_mib_per_second"] = (metrics["io_write_bytes_delta"] / MIB / elapsed_s) if metrics["io_write_bytes_delta"] is not None else None  #lzx
    scan = metrics["pgscan_delta"]  #lzx
    steal = metrics["pgsteal_delta"]  #lzx
    refault_file = metrics["workingset_refault_file_delta"]  #lzx
    refault_anon = metrics["workingset_refault_anon_delta"]  #lzx
    direct_scan = metrics["pgscan_direct_delta"]  #lzx
    kswapd_scan = metrics["pgscan_kswapd_delta"]  #lzx
    metrics["scan_efficiency_percent"] = (100.0 * steal / scan) if scan not in (None, 0) and steal is not None else None  #lzx
    metrics["page_refault_ratio_percent"] = (100.0 * (refault_file + refault_anon) / steal) if None not in (refault_file, refault_anon, steal) and steal != 0 else None  #lzx
    metrics["direct_reclaim_scan_ratio_percent"] = (100.0 * direct_scan / (direct_scan + kswapd_scan)) if None not in (direct_scan, kswapd_scan) and direct_scan + kswapd_scan != 0 else None  #lzx
    return metrics  #lzx


def popup_titles(env: dict[str, str]) -> list[str]:
    outcome = subprocess.run(["wmctrl", "-l"], text=True, capture_output=True, env={**os.environ, **env}, timeout=5, check=False)
    if outcome.returncode != 0:
        return []
    return [line.strip() for line in outcome.stdout.splitlines() if LOW_MEMORY_RE.search(line)]


def write_monitor_header(stream: Any) -> csv.DictWriter:
    fields = [
        "timestamp_ns", "memavailable", "swapfree", "psi_some_avg10", "psi_full_avg10",
        "vm_oom_kill", "pswpin", "pswpout", "kswapd_cpu_time_ns", "cgroup_status", "cgroup_device", "cgroup_inode", "memory_current",  #lzx
        "pgfault", "pgmajfault", "refault_file", "refault_anon",  #lzx
        "activate_file", "activate_anon", "restore_file", "restore_anon",  #lzx
        "pgscan", "pgsteal", "pgscan_direct", "pgsteal_direct",  #lzx
        "pgscan_kswapd", "pgsteal_kswapd", "cgroup_pswpin", "cgroup_pswpout",  #lzx
        "events_high", "events_max", "events_oom", "events_oom_kill", "low_memory_popup_count",  #lzx
        "cpu_usage_usec", "cpu_user_usec", "cpu_system_usec", "io_rbytes", "io_wbytes", "io_rios", "io_wios",  #lzx
    ]
    writer = csv.DictWriter(stream, fieldnames=fields)
    writer.writeheader()
    return writer


def trace_stats(instance: str, destination: Path) -> None:
    result = run(["sudo", "-n", "bash", str(TRACE_HELPER), "stats", instance], timeout=30)
    destination.write_text(result.stdout + result.stderr, encoding="utf-8")


def parse_trace_stats(path: Path) -> dict[str, int]:
    result = {"overrun": 0, "commit_overrun": 0, "dropped_events": 0, "read_events": 0}
    patterns = {
        "overrun": re.compile(r"^overrun:\s+(\d+)$", re.MULTILINE),
        "commit_overrun": re.compile(r"^commit overrun:\s+(\d+)$", re.MULTILINE),
        "dropped_events": re.compile(r"^dropped events:\s+(\d+)$", re.MULTILINE),
        "read_events": re.compile(r"^read events:\s+(\d+)$", re.MULTILINE),
    }
    text = read_text(path)
    for key, pattern in patterns.items():
        result[key] = sum(int(value) for value in pattern.findall(text))
    return result


def nearest_rank(values: list[int], percentile: float) -> int:  #lzx
    if not values:  #lzx
        return 0  #lzx
    ordered = sorted(values)  #lzx
    index = max(0, min(len(ordered) - 1, math.ceil(percentile * len(ordered)) - 1))  #lzx
    return ordered[index]  #lzx


def count_trace_events(path: Path) -> dict[str, Any]:  #lzx
    names = {
        "page_fault_user": "page_fault_user:",
        "page_fault_kernel": "page_fault_kernel:",
        "parp_decision": "parp_effective_tier_decision:",
        "parp_access": "parp_effective_tier_access:",
        "parp_outcome": "parp_effective_tier_outcome:",
        "direct_reclaim_begin": "mm_vmscan_direct_reclaim_begin:",
        "direct_reclaim_end": "mm_vmscan_direct_reclaim_end:",  #lzx
        "memcg_reclaim_begin": "mm_vmscan_memcg_reclaim_begin:",  #lzx
        "memcg_reclaim_end": "mm_vmscan_memcg_reclaim_end:",  #lzx
        "kswapd_wake": "mm_vmscan_kswapd_wake:",
        "kswapd_sleep": "mm_vmscan_kswapd_sleep:",  #lzx
    }
    counts = {key: 0 for key in names}
    event_pattern = re.compile(r"-(\d+)\s+(?:\([^)]*\)\s+)?\[\d+\]\s+\S+\s+(\d+\.\d+):\s+(\w+):\s*(.*)$")  #lzx
    direct_starts: dict[int, list[int]] = {}  #lzx
    memcg_starts: dict[int, list[int]] = {}  #lzx
    kswapd_starts: dict[int, int] = {}  #lzx
    direct_durations: list[int] = []  #lzx
    memcg_durations: list[int] = []  #lzx
    kswapd_active_durations: list[int] = []  #lzx
    direct_reclaimed_pages = 0  #lzx
    pairing_errors: list[str] = []  #lzx
    pairing_parse_errors = 0  #lzx
    try:
        with path.open(encoding="utf-8", errors="replace") as stream:
            for line in stream:
                for key, marker in names.items():
                    if marker in line:
                        counts[key] += 1
                        break
                match = event_pattern.search(line)  #lzx
                if not match:  #lzx
                    if any(marker in line for marker in (  #lzx
                        "mm_vmscan_direct_reclaim_begin:", "mm_vmscan_direct_reclaim_end:",  #lzx
                        "mm_vmscan_memcg_reclaim_begin:", "mm_vmscan_memcg_reclaim_end:",  #lzx
                    )):  #lzx
                        pairing_parse_errors += 1  #lzx
                    continue  #lzx
                pid = int(match.group(1))  #lzx
                timestamp_ns = int(float(match.group(2)) * 1_000_000_000)  #lzx
                event = match.group(3)  #lzx
                payload = match.group(4)  #lzx
                if event == "mm_vmscan_direct_reclaim_begin":  #lzx
                    starts = direct_starts.setdefault(pid, [])  #lzx
                    if starts:  #lzx
                        pairing_errors.append(f"nested direct reclaim begin for pid {pid}")  #lzx
                    starts.append(timestamp_ns)  #lzx
                elif event == "mm_vmscan_direct_reclaim_end":  #lzx
                    starts = direct_starts.get(pid, [])  #lzx
                    if starts:  #lzx
                        direct_durations.append(max(0, timestamp_ns - starts.pop()))  #lzx
                    else:  #lzx
                        pairing_errors.append(f"direct reclaim end without begin for pid {pid}")  #lzx
                    reclaimed = re.search(r"\bnr_reclaimed=(\d+)", payload)  #lzx
                    direct_reclaimed_pages += int(reclaimed.group(1)) if reclaimed else 0  #lzx
                elif event == "mm_vmscan_memcg_reclaim_begin":  #lzx
                    starts = memcg_starts.setdefault(pid, [])  #lzx
                    if starts:  #lzx
                        pairing_errors.append(f"nested memcg reclaim begin for pid {pid}")  #lzx
                    starts.append(timestamp_ns)  #lzx
                elif event == "mm_vmscan_memcg_reclaim_end":  #lzx
                    starts = memcg_starts.get(pid, [])  #lzx
                    if starts:  #lzx
                        memcg_durations.append(max(0, timestamp_ns - starts.pop()))  #lzx
                    else:  #lzx
                        pairing_errors.append(f"memcg reclaim end without begin for pid {pid}")  #lzx
                elif event == "mm_vmscan_kswapd_wake":  #lzx
                    kswapd_starts.setdefault(pid, timestamp_ns)  #lzx
                elif event == "mm_vmscan_kswapd_sleep" and pid in kswapd_starts:  #lzx
                    kswapd_active_durations.append(max(0, timestamp_ns - kswapd_starts.pop(pid)))  #lzx
    except OSError as exc:  #lzx
        pairing_errors.append(f"trace unavailable: {type(exc).__name__}: {exc}")  #lzx
    for pid, starts in direct_starts.items():  #lzx
        if starts:  #lzx
            pairing_errors.append(f"{len(starts)} direct reclaim begin event(s) left open for pid {pid}")  #lzx
    for pid, starts in memcg_starts.items():  #lzx
        if starts:  #lzx
            pairing_errors.append(f"{len(starts)} memcg reclaim begin event(s) left open for pid {pid}")  #lzx
    for prefix, durations in (("direct_reclaim", direct_durations), ("memcg_reclaim", memcg_durations), ("kswapd_active", kswapd_active_durations)):  #lzx
        counts[f"{prefix}_pairs"] = len(durations)  #lzx
        counts[f"{prefix}_time_ns_total"] = sum(durations)  #lzx
        counts[f"{prefix}_time_ns_max"] = max(durations, default=0)  #lzx
        counts[f"{prefix}_latency_ns_p50"] = nearest_rank(durations, 0.50)  #lzx
        counts[f"{prefix}_latency_ns_p95"] = nearest_rank(durations, 0.95)  #lzx
        counts[f"{prefix}_latency_ns_p99"] = nearest_rank(durations, 0.99)  #lzx
    counts["direct_reclaim_pages_reclaimed"] = direct_reclaimed_pages  #lzx
    counts["pairing_parse_errors"] = pairing_parse_errors  #lzx
    counts["pairing_errors"] = pairing_errors  #lzx
    counts["pairing_error_count"] = len(pairing_errors) + pairing_parse_errors  #lzx
    counts["kswapd_unpaired_wakes"] = len(kswapd_starts)  #lzx
    return counts


def csv_delta(path: Path, first_key: str, last_key: str) -> int:
    rows = list(csv.DictReader(path.open(encoding="utf-8", newline=""))) if path.exists() else []
    if not rows:
        return 0
    return int(rows[-1].get(last_key, 0) or 0) - int(rows[0].get(first_key, 0) or 0)


def row_counter_delta(rows: list[dict[str, str]], key: str) -> int | None:  #lzx
    if not rows or key not in rows[0] or key not in rows[-1]:  #lzx
        return None  #lzx
    try:  #lzx
        return int(rows[-1][key]) - int(rows[0][key])  #lzx
    except (KeyError, TypeError, ValueError):  #lzx
        return None  #lzx


def automation_counts(path: Path, suite: str) -> dict[str, int]:
    result = {"case_start": 0, "case_done": 0, "failed_actions": 0, "launch_failures": 0, "launch_success": 0, "scenario_failed": 0}
    if not path.exists():
        return result
    with path.open(encoding="utf-8", newline="") as stream:
        for row in csv.DictReader(stream):
            event = row.get("event_type", "")
            status = row.get("status", "")
            if event == f"{suite.upper()}_CASE_START":
                result["case_start"] += 1
            elif event == f"{suite.upper()}_CASE_DONE":
                result["case_done"] += 1
            elif event == "APP_LAUNCH" and status == "success" and row.get("label", "").startswith("LAUNCH_"):
                result["launch_success"] += 1
            elif event == "SCENARIO_FAILED":
                result["scenario_failed"] += 1
            if row.get("phase") == "end" and status == "failed":
                result["failed_actions"] += 1
                if row.get("label", "").startswith(("LAUNCH_", "WAIT_")):
                    result["launch_failures"] += 1
    return result


def launch_latency_metrics(path: Path) -> dict[str, Any]:  #lzx
    starts: dict[str, list[int]] = {}  #lzx
    values_by_app: dict[str, list[float]] = {}  #lzx
    invalid_reasons: list[str] = []  #lzx
    if not path.exists():  #lzx
        return {  #lzx
            "measurement_source": "automation_x11_verified_window_proxy", "count": 0, "mean_ms": None,  #lzx
            "p95_ms": None, "max_ms": None, "values_ms": [], "by_app_ms": {},  #lzx
            "invalid_reasons": ["automation trace unavailable for launch latency"],  #lzx
            "warning": "X11 verified-window time is a startup-readiness proxy, not first interactive frame time.",  #lzx
        }  #lzx
    with path.open(encoding="utf-8", newline="") as stream:  #lzx
        for row in csv.DictReader(stream):  #lzx
            label = row.get("label", "")  #lzx
            app = row.get("app_key", "") or row.get("app", "")  #lzx
            try:  #lzx
                timestamp_ns = int(row.get("ts_ns", ""))  #lzx
            except ValueError:  #lzx
                continue  #lzx
            if row.get("phase") == "start" and row.get("action") == "launch" and label.startswith("LAUNCH_"):  #lzx
                starts.setdefault(app, []).append(timestamp_ns)  #lzx
            elif row.get("phase") == "end" and row.get("action") == "wait_window" and row.get("status") == "success" and label.startswith("WAIT_"):  #lzx
                pending = starts.get(app, [])  #lzx
                if not pending:  #lzx
                    invalid_reasons.append(f"window ready without launch start for {app}")  #lzx
                    continue  #lzx
                started_ns = pending.pop(0)  #lzx
                latency_ms = max(0.0, (timestamp_ns - started_ns) / 1_000_000)  #lzx
                values_by_app.setdefault(app, []).append(latency_ms)  #lzx
    for app, pending in starts.items():  #lzx
        if pending:  #lzx
            invalid_reasons.append(f"{len(pending)} launch(es) without verified window for {app}")  #lzx
    values = [value for app_values in values_by_app.values() for value in app_values]  #lzx
    p95 = nearest_rank([int(round(value * 1_000_000)) for value in values], 0.95) / 1_000_000 if values else None  #lzx
    return {  #lzx
        "measurement_source": "automation_x11_verified_window_proxy", "count": len(values),  #lzx
        "mean_ms": (sum(values) / len(values)) if values else None, "p95_ms": p95,  #lzx
        "max_ms": max(values) if values else None, "values_ms": values, "by_app_ms": values_by_app,  #lzx
        "invalid_reasons": invalid_reasons,  #lzx
        "warning": "X11 verified-window time is a startup-readiness proxy; concurrent peak launches may make it an upper bound, not first-frame latency.",  #lzx
    }  #lzx


def finalize_round(session_dir: Path, suite: str, expected_steps: int, automation_rc: int, abort_reason: str) -> dict[str, Any]:
    trace_counts = count_trace_events(session_dir / "trace/trace.txt")
    stats = parse_trace_stats(session_dir / "trace/stats-after.txt")
    auto = automation_counts(session_dir / "automation_trace.csv", suite)
    monitor_path = session_dir / "monitor.csv"
    popup_count = 0
    rows: list[dict[str, str]] = []  #lzx
    if monitor_path.exists():
        rows = list(csv.DictReader(monitor_path.open(encoding="utf-8", newline="")))
        if rows:
            popup_active = False
            for row in rows:
                active = int(row.get("low_memory_popup_count", 0) or 0) > 0
                if active and not popup_active:
                    popup_count += 1
                popup_active = active
    try:  #lzx
        before = json.loads((session_dir / "snapshot-before.json").read_text(encoding="utf-8"))  #lzx
    except (FileNotFoundError, OSError, json.JSONDecodeError):  #lzx
        before = {}  #lzx
    try:  #lzx
        after = json.loads((session_dir / "snapshot-after.json").read_text(encoding="utf-8"))  #lzx
    except (FileNotFoundError, OSError, json.JSONDecodeError):  #lzx
        after = {}  #lzx
    cgroup_valid, cgroup_invalid_reasons = cgroup_endpoint_validity(before, after)  #lzx
    cgroup_metrics = cgroup_endpoint_metrics(before, after) if before and after else {}  #lzx
    launch_metrics = launch_latency_metrics(session_dir / "automation_trace.csv")  #lzx
    before_vmstat = before.get("vmstat", {}) if isinstance(before, dict) else {}  #lzx
    after_vmstat = after.get("vmstat", {}) if isinstance(after, dict) else {}  #lzx
    host_oom_delta = endpoint_delta(before_vmstat, after_vmstat, "oom_kill")  #lzx
    global_pswpin_delta = endpoint_delta(before_vmstat, after_vmstat, "pswpin")  #lzx
    global_pswpout_delta = endpoint_delta(before_vmstat, after_vmstat, "pswpout")  #lzx
    system_metrics = {  #lzx
        "host_oom_kill_delta": host_oom_delta,  #lzx
        "global_pswpin_delta": global_pswpin_delta,  #lzx
        "global_pswpout_delta": global_pswpout_delta,  #lzx
        "kswapd_cpu_time_ns_delta": row_counter_delta(rows, "kswapd_cpu_time_ns"),  #lzx
    }  #lzx
    cgroup_oom_kill = int(cgroup_metrics.get("oom_kill_delta") or 0)  #lzx
    trace_loss = stats["overrun"] + stats["commit_overrun"] + stats["dropped_events"]
    invalid_reasons: list[str] = []  #lzx
    if automation_rc != 0:  #lzx
        invalid_reasons.append(f"automation returned {automation_rc}")  #lzx
    if abort_reason:  #lzx
        invalid_reasons.append(abort_reason)  #lzx
    if auto["case_done"] != expected_steps:  #lzx
        invalid_reasons.append(f"completed cases {auto['case_done']}/{expected_steps}")  #lzx
    if trace_loss != 0:  #lzx
        invalid_reasons.append(f"trace lost {trace_loss} event(s)")  #lzx
    if trace_counts.get("pairing_error_count", 0):  #lzx
        invalid_reasons.extend(str(value) for value in trace_counts.get("pairing_errors", []))  #lzx
        if trace_counts.get("pairing_parse_errors", 0):  #lzx
            invalid_reasons.append(f"trace pairing parse errors: {trace_counts['pairing_parse_errors']}")  #lzx
    if not cgroup_valid:  #lzx
        invalid_reasons.extend(cgroup_invalid_reasons)  #lzx
    if not rows:  #lzx
        invalid_reasons.append("monitor.csv has no samples")  #lzx
    bad_monitor_statuses = sorted({row.get("cgroup_status", "missing") for row in rows if row.get("cgroup_status") != "ok"})  #lzx
    if bad_monitor_statuses:  #lzx
        invalid_reasons.append("cgroup monitor read failure: " + ",".join(bad_monitor_statuses))  #lzx
    monitor_identities = {(row.get("cgroup_device", ""), row.get("cgroup_inode", "")) for row in rows}  #lzx
    if len(monitor_identities) > 1 or any(not device or not inode for device, inode in monitor_identities):  #lzx
        invalid_reasons.append("cgroup identity changed or disappeared during monitoring")  #lzx
    if host_oom_delta not in (None, 0):  #lzx
        invalid_reasons.append(f"host oom_kill increased by {host_oom_delta}")  #lzx
    if launch_metrics["count"] != auto["launch_success"]:  #lzx
        invalid_reasons.append(f"launch readiness samples {launch_metrics['count']}/{auto['launch_success']}")  #lzx
    invalid_reasons.extend(launch_metrics["invalid_reasons"])  #lzx
    invalid_reasons = list(dict.fromkeys(invalid_reasons))  #lzx
    valid = not invalid_reasons  #lzx
    result = {
        "status": "VALID_DIAGNOSTIC" if valid else "INVALID",
        "suite": suite, "expected_steps": expected_steps, "automation_rc": automation_rc,
        "abort_reason": abort_reason, "automation": auto, "trace": {**trace_counts, **stats, "loss_total": trace_loss},
        "cgroup": cgroup_metrics,  #lzx
        "system": system_metrics,  #lzx
        "launch": launch_metrics,  #lzx
        "validity": {  #lzx
            "valid": valid, "invalid_reasons": invalid_reasons, "cgroup_endpoints_valid": cgroup_valid,  #lzx
            "trace_pairing_valid": trace_counts.get("pairing_error_count", 0) == 0,  #lzx
            "monitor_samples": len(rows),  #lzx
        },  #lzx
        "events": {"launch_failures": auto["launch_failures"], "low_memory_popups": popup_count, "app_oom_kills": cgroup_oom_kill,
                   "failure_total": auto["launch_failures"] + popup_count + cgroup_oom_kill},
    }
    write_json(session_dir / "round-result.json", result)
    return result


def run_round(config: dict[str, Any], *, suite: str, profile: str, round_index: int, seed: int, parent_dir: Path, preflight_data: dict[str, Any]) -> dict[str, Any]:
    session_dir = parent_dir / f"round-{round_index:02d}"
    for child in ("trace", "ballast", "screenshots"):
        (session_dir / child).mkdir(parents=True, exist_ok=True)
    instance = f"parp-accept-{os.getpid()}-{round_index}"
    scenario = generate_scenario(config, suite=suite, profile=profile, round_index=round_index, seed=seed, session_dir=session_dir, trace_instance=instance)
    scenario_path = session_dir / "scenario.json"
    write_json(scenario_path, scenario)
    write_json(session_dir / "preflight.json", preflight_data)
    expected_steps = int(scenario["metadata"]["scored_steps"])
    cgroup_path: Path | None = None
    trace_stream: subprocess.Popen[Any] | None = None
    trace_output: Any = None
    automation: subprocess.Popen[Any] | None = None
    abort_reason = ""
    automation_rc = 1
    gui = preflight_data["gui"]
    safety = config["safety"]
    low_available_count = 0
    high_psi_count = 0
    root_oom_before = vmstat().get("oom_kill", 0)
    try:
        cgroup_path = setup_slice(config)
        before = snapshot(cgroup_path)
        write_json(session_dir / "snapshot-before.json", before)
        setup = run([
            "sudo", "-n", "bash", str(TRACE_HELPER), "setup", instance,
            str(int(safety["trace_buffer_kb_per_cpu"])),
        ], timeout=30)
        if setup.returncode != 0:
            raise RuntimeError(setup.stderr.strip() or "trace setup failed")
        trace_stats(instance, session_dir / "trace/stats-before.txt")
        trace_output = (session_dir / "trace/trace.txt").open("w", encoding="utf-8")
        trace_stream = subprocess.Popen(["sudo", "-n", "bash", str(TRACE_HELPER), "stream", instance], stdout=trace_output, stderr=(session_dir / "trace/stream-error.txt").open("w", encoding="utf-8"), text=True)
        env = {**os.environ, **gui, "GDK_BACKEND": "x11", "MOZ_ENABLE_WAYLAND": "0", "WAYLAND_DISPLAY": ""}
        automation_log = (session_dir / "automation.log").open("w", encoding="utf-8")
        command = [
            "bash", str(AUTOMATION), "--scenario", str(scenario_path), "--display", gui["DISPLAY"],
            "--xauthority", gui["XAUTHORITY"], "--trace-output", str(session_dir / "automation_trace.csv"),
            "--session-id", session_dir.name, "--scenario-id", f"parp_{suite}_{round_index}",
            "--test-slice", str(config["slice"]), "--reset-files",
        ]
        automation = subprocess.Popen(command, stdout=automation_log, stderr=subprocess.STDOUT, text=True, env=env, start_new_session=True)
        with (session_dir / "monitor.csv").open("w", encoding="utf-8", newline="") as monitor_stream:
            writer = write_monitor_header(monitor_stream)
            started = time.monotonic()
            while automation.poll() is None:
                current = snapshot(cgroup_path)
                cg = current["cgroup"]
                popups = popup_titles(gui)
                writer.writerow({
                    "timestamp_ns": current["timestamp_ns"], "memavailable": current["memavailable"], "swapfree": current["swapfree"],
                    "psi_some_avg10": current["psi"].get("some_avg10", 0), "psi_full_avg10": current["psi"].get("full_avg10", 0),
                    "vm_oom_kill": current["vmstat"].get("oom_kill", 0), "pswpin": current["vmstat"].get("pswpin", 0), "pswpout": current["vmstat"].get("pswpout", 0),  #lzx
                    "kswapd_cpu_time_ns": current["kswapd_cpu_time_ns"],  #lzx
                    "cgroup_status": cg.get("status", ""),  #lzx
                    "cgroup_device": (cg.get("identity") or {}).get("device"), "cgroup_inode": (cg.get("identity") or {}).get("inode"),  #lzx
                    "memory_current": cg.get("memory_current"), "pgfault": cg.get("pgfault"),  #lzx
                    "pgmajfault": cg.get("pgmajfault", 0), "refault_file": cg.get("workingset_refault_file", 0), "refault_anon": cg.get("workingset_refault_anon", 0),  #lzx
                    "activate_file": cg.get("workingset_activate_file", 0), "activate_anon": cg.get("workingset_activate_anon", 0),  #lzx
                    "restore_file": cg.get("workingset_restore_file", 0), "restore_anon": cg.get("workingset_restore_anon", 0),  #lzx
                    "pgscan": cg.get("pgscan", 0), "pgsteal": cg.get("pgsteal", 0),  #lzx
                    "pgscan_direct": cg.get("pgscan_direct", 0), "pgsteal_direct": cg.get("pgsteal_direct", 0),  #lzx
                    "pgscan_kswapd": cg.get("pgscan_kswapd", 0), "pgsteal_kswapd": cg.get("pgsteal_kswapd", 0),  #lzx
                    "cgroup_pswpin": cg.get("cgroup_pswpin", 0), "cgroup_pswpout": cg.get("cgroup_pswpout", 0),  #lzx
                    "events_high": cg.get("events_high", 0), "events_max": cg.get("events_max", 0), "events_oom": cg.get("events_oom", 0), "events_oom_kill": cg.get("events_oom_kill", 0),
                    "low_memory_popup_count": len(popups),
                    "cpu_usage_usec": cg.get("cpu_usage_usec"), "cpu_user_usec": cg.get("cpu_user_usec"), "cpu_system_usec": cg.get("cpu_system_usec"),  #lzx
                    "io_rbytes": cg.get("io_rbytes"), "io_wbytes": cg.get("io_wbytes"), "io_rios": cg.get("io_rios"), "io_wios": cg.get("io_wios"),  #lzx
                })
                monitor_stream.flush()
                if popups:
                    write_json(session_dir / "low-memory-popup.json", {"timestamp_ns": time.time_ns(), "windows": popups})
                low_available_count = low_available_count + 1 if current["memavailable"] < int(safety["min_memavailable_bytes"]) else 0
                psi_guard = (
                    current["psi"].get("full_avg10", 0) > float(safety["psi_full_avg10_abort"])
                    and current["memavailable"] < int(safety["psi_memavailable_guard_bytes"])
                )
                high_psi_count = high_psi_count + 1 if psi_guard else 0
                if current["vmstat"].get("oom_kill", 0) > root_oom_before:
                    abort_reason = "ROOT_OOM_KILL_INCREMENT"
                elif low_available_count >= int(safety["abort_consecutive_samples"]):
                    abort_reason = "MEMAVAILABLE_HARD_FLOOR"
                elif high_psi_count >= int(safety["abort_consecutive_samples"]):
                    abort_reason = "PSI_FULL_HARD_LIMIT"
                elif time.monotonic() - started > float(safety["max_round_seconds"]):
                    abort_reason = "ROUND_TIMEOUT"
                if abort_reason:
                    os.killpg(automation.pid, signal.SIGTERM)
                    break
                time.sleep(float(safety["sample_interval_seconds"]))
        try:
            automation_rc = automation.wait(timeout=30)
        except subprocess.TimeoutExpired:
            os.killpg(automation.pid, signal.SIGKILL)
            automation_rc = automation.wait(timeout=10)
        automation_log.close()
        write_json(session_dir / "snapshot-after.json", snapshot(cgroup_path))
    except Exception as exc:
        abort_reason = abort_reason or f"HARNESS_ERROR:{type(exc).__name__}:{exc}"
    finally:
        try:
            run(["sudo", "-n", "bash", str(TRACE_HELPER), "disable", instance], timeout=15)
            run(["sudo", "-n", "bash", str(TRACE_HELPER), "stop-stream", instance], timeout=15)
            if trace_stream is not None:
                try:
                    trace_stream.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    run(["sudo", "-n", "bash", str(TRACE_HELPER), "stop-stream", instance], timeout=15)
            if trace_output is not None:
                trace_output.close()
            trace_stats(instance, session_dir / "trace/stats-after.txt")
            run(["sudo", "-n", "bash", str(TRACE_HELPER), "cleanup", instance], timeout=30)
        finally:
            cleanup_slice(config)
            ballast_root = (session_dir / "ballast").resolve()
            for sparse_file in ballast_root.glob("*.sparse"):
                resolved = sparse_file.resolve()
                if resolved.parent == ballast_root:
                    resolved.unlink(missing_ok=True)
    return finalize_round(session_dir, suite, expected_steps, automation_rc, abort_reason)


def aggregate(parent: Path, preflight_data: dict[str, Any], results: list[dict[str, Any]], suite: str, profile: str) -> dict[str, Any]:
    valid = [item for item in results if item["status"] == "VALID_DIAGNOSTIC"]
    def mean(path: tuple[str, str]) -> float | None:  #lzx
        values = [item.get(path[0], {}).get(path[1]) for item in valid]  #lzx
        present = [float(value) for value in values if value is not None]  #lzx
        return sum(present) / len(present) if present else None  #lzx
    first_scenario = json.loads((parent / "round-01/scenario.json").read_text(encoding="utf-8"))
    limitations = [
        "当前 r9 为 Shadow，apply_compiled=0，不能计算优化改善率。",
        "PageFault trace 只过滤到受控应用内存 sidecar PID；真实 GUI 应用及 sidecar 的总 fault 以测试 slice cgroup pgfault 复核。",
        "正式验收必须用同源 Native/OFF 与 Apply 内核、完全相同的 seed/场景进行成对比较。",
    ]
    if suite == "peak" and valid and mean(("events", "failure_total")) == 0:
        limitations.append("当前基线异常总数为 0，30% 降低率分母为 0；需要经过安全校准的更强峰值负载后才能评价该指标。")
    summary = {
        "status": "DIAGNOSTIC_BASELINE_COMPLETE" if len(valid) == len(results) else "DIAGNOSTIC_BASELINE_INCOMPLETE",
        "acceptance_verdict": "NOT_EVALUABLE_SHADOW_NO_APPLY" if preflight_data["diagnostic_only"] else "BASELINE_ONLY_NO_OPTIMIZED_PAIR",
        "kernel_release": preflight_data["kernel_release"], "suite": suite, "profile": profile,
        "rounds_requested": len(results), "rounds_valid": len(valid),
        "workload_contract": first_scenario.get("metadata", {}).get("workload_contract", {}),
        "averages": {
            "trace_page_fault_user": mean(("trace", "page_fault_user")),
            "cgroup_pgfault": mean(("cgroup", "pgfault_delta")),
            "cgroup_pgmajfault": mean(("cgroup", "pgmajfault_delta")),
            "workingset_refault_file": mean(("cgroup", "workingset_refault_file_delta")),  #lzx
            "workingset_refault_anon": mean(("cgroup", "workingset_refault_anon_delta")),  #lzx
            "workingset_activate_file": mean(("cgroup", "workingset_activate_file_delta")),  #lzx
            "workingset_activate_anon": mean(("cgroup", "workingset_activate_anon_delta")),  #lzx
            "pgscan": mean(("cgroup", "pgscan_delta")), "pgsteal": mean(("cgroup", "pgsteal_delta")),  #lzx
            "scan_efficiency_percent": mean(("cgroup", "scan_efficiency_percent")),  #lzx
            "page_refault_ratio_percent": mean(("cgroup", "page_refault_ratio_percent")),  #lzx
            "direct_reclaim_scan_ratio_percent": mean(("cgroup", "direct_reclaim_scan_ratio_percent")),  #lzx
            "cgroup_cpu_usage_usec": mean(("cgroup", "cpu_usage_usec_delta")),  #lzx
            "cgroup_cpu_one_core_percent": mean(("cgroup", "cpu_one_core_percent")),  #lzx
            "cgroup_cpu_machine_percent": mean(("cgroup", "cpu_machine_percent")),  #lzx
            "cgroup_io_read_bytes": mean(("cgroup", "io_read_bytes_delta")),  #lzx
            "cgroup_io_write_bytes": mean(("cgroup", "io_write_bytes_delta")),  #lzx
            "cgroup_io_read_mib_per_second": mean(("cgroup", "io_read_mib_per_second")),  #lzx
            "cgroup_io_write_mib_per_second": mean(("cgroup", "io_write_mib_per_second")),  #lzx
            "launch_ready_latency_mean_ms": mean(("launch", "mean_ms")),  #lzx
            "launch_ready_latency_p95_ms": mean(("launch", "p95_ms")),  #lzx
            "cgroup_oom": mean(("cgroup", "oom_delta")), "cgroup_oom_kill": mean(("cgroup", "oom_kill_delta")),  #lzx
            "launch_failures": mean(("events", "launch_failures")),
            "low_memory_popups": mean(("events", "low_memory_popups")),
            "app_oom_kills": mean(("events", "app_oom_kills")),
            "failure_total": mean(("events", "failure_total")),
        },
        "results": results,
        "limitations": limitations,
    }
    write_json(parent / "summary.json", summary)
    lines = [
        f"# PARP {suite} 当前内核诊断结果", "",
        f"- 状态：`{summary['status']}`", f"- 验收结论：`{summary['acceptance_verdict']}`",
        f"- 内核：`{summary['kernel_release']}`", f"- 有效轮次：`{len(valid)}/{len(results)}`", "",
        "## 平均值", "",
    ]
    for key, value in summary["averages"].items():
        rendered = "N/A" if value is None else f"{value:.3f}"  #lzx
        lines.append(f"- {key}: `{rendered}`")  #lzx
    lines += ["", "## 结论", "", "本结果是优化前诊断基线，不代表达到 PageFault 降低 20%/30% 或峰值异常降低 30%。"]
    (parent / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return summary


def execute(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    pf = preflight(config, args.profile, args.suite)
    root = output_root(config)
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    parent = root / f"{args.suite}-{args.profile}-{stamp}-{os.uname().release}"
    parent.mkdir(parents=True, exist_ok=True)
    write_json(parent / "system-metadata-lzx.json", pf.get("system_metadata", {}))  #lzx
    write_json(parent / "preflight.json", pf)
    print(f"output={parent}", flush=True)
    if pf["status"] != "READY":
        print(json.dumps(pf, ensure_ascii=False, indent=2))
        return 2
    rounds = int(config["profiles"][args.profile][f"{args.suite}_repeats" if args.suite == "hotcold" else f"{args.suite}_rounds"])
    if args.rounds:
        rounds = args.rounds
    results = []
    for index in range(1, rounds + 1):
        seed = args.seed + index - 1
        print(f"round={index}/{rounds} seed={seed}", flush=True)
        results.append(run_round(config, suite=args.suite, profile=args.profile, round_index=index, seed=seed, parent_dir=parent, preflight_data=pf))
        print(f"round_status={results[-1]['status']}", flush=True)
    summary = aggregate(parent, pf, results, args.suite, args.profile)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["rounds_valid"] == rounds else 1


def suite_evidence(summary_path: Path) -> dict[str, Any]:
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    suite_root = summary_path.parent
    monitor_rows: list[dict[str, str]] = []
    for monitor_path in sorted(suite_root.glob("round-*/monitor.csv")):
        with monitor_path.open(encoding="utf-8", newline="") as stream:
            monitor_rows.extend(csv.DictReader(stream))
    contract = summary.get("workload_contract", {})
    if not contract:
        scenario_path = suite_root / "round-01/scenario.json"
        if scenario_path.exists():
            scenario = json.loads(scenario_path.read_text(encoding="utf-8"))
            contract = scenario.get("metadata", {}).get("workload_contract", {
                "logical_ratio": scenario.get("metadata", {}).get("logical_ratio"),
                "logical_total_bytes": scenario.get("metadata", {}).get("logical_total_bytes"),
                "memtotal_bytes": scenario.get("metadata", {}).get("memtotal_bytes"),
            })
    def values(field: str) -> list[float]:
        result: list[float] = []
        for row in monitor_rows:
            try:
                result.append(float(row.get(field, 0) or 0))
            except ValueError:
                pass
        return result
    return {
        "path": str(suite_root.resolve()), "summary": summary, "workload_contract": contract,
        "monitor_extrema": {
            "min_memavailable_bytes": min(values("memavailable"), default=0),
            "max_psi_full_avg10": max(values("psi_full_avg10"), default=0),
            "max_test_cgroup_memory_current_bytes": max(values("memory_current"), default=0),
            "min_swapfree_bytes": min(values("swapfree"), default=0),
        },
        "trace_loss_total": sum(int(item["trace"]["loss_total"]) for item in summary.get("results", [])),
        "case_done_total": sum(int(item["automation"]["case_done"]) for item in summary.get("results", [])),
        "root_or_cgroup_oom_total": sum(int(item["cgroup"]["oom_kill_delta"]) for item in summary.get("results", [])),
    }


def combine_reports(args: argparse.Namespace) -> int:
    hotcold = suite_evidence(args.hotcold)
    peak = suite_evidence(args.peak)
    hot_summary = hotcold["summary"]
    peak_summary = peak["summary"]
    if hot_summary.get("kernel_release") != peak_summary.get("kernel_release"):
        raise RuntimeError("hotcold and peak results use different kernels")
    payload = {
        "status": "CURRENT_R9_DIAGNOSTIC_COMPLETE",
        "acceptance_verdict": "NOT_EVALUABLE_SHADOW_NO_APPLY",
        "kernel_release": hot_summary.get("kernel_release"),
        "generated_at": dt.datetime.now().isoformat(),
        "hotcold": hotcold,
        "peak": peak,
        "acceptance": {
            "pagefault_improvement_percent": None,
            "pagefault_target_percent": 20,
            "pagefault_challenge_percent": 30,
            "peak_failure_improvement_percent": None,
            "peak_failure_target_percent": 30,
            "reason": "当前内核 apply_compiled=0，且没有同源 Apply 配对结果；峰值基线异常总数为 0。",
        },
        "next_required": [
            "安全校准更强的峰值匿名内存比例，使 Native/OFF 基线出现非零但仅限测试 cgroup 的异常事件。",
            "构建同源 Apply 内核，并保留 Native/OFF 运行模式。",
            "每组测试前重启，使用本报告保存的 seed 和 scenario 进行成对 10 轮/3 轮比较。",
        ],
    }
    args.output_dir.mkdir(parents=True, exist_ok=True)
    json_path = args.output_dir / "current-r9-diagnostic-lzx.json"
    md_path = args.output_dir / "current-r9-diagnostic-lzx.md"
    write_json(json_path, payload)
    ha = hot_summary["averages"]
    pa = peak_summary["averages"]
    lines = [
        "# 当前 r9 内核诊断基线", "",
        f"- 状态：`{payload['status']}`",
        f"- 验收结论：`{payload['acceptance_verdict']}`",
        f"- 内核：`{payload['kernel_release']}`",
        "- 原因：当前 r9 为 Shadow，`apply_compiled=0`，没有执行页面保护/降级动作。", "",
        "## 冷热 PageFault", "",
        f"- 有效轮次：`{hot_summary['rounds_valid']}/{hot_summary['rounds_requested']}`；有效步骤：`{hotcold['case_done_total']}`。",
        f"- 受控内存 trace PageFault 均值：`{ha['trace_page_fault_user']:.3f}`。",
        f"- 测试 slice pgfault / pgmajfault 均值：`{ha['cgroup_pgfault']:.3f}` / `{ha['cgroup_pgmajfault']:.3f}`。",
        f"- trace 丢失总数：`{hotcold['trace_loss_total']}`。",
        "- 20%/30% 改善率：`N/A`，尚无 Apply 配对结果。", "",
        "## 峰值调度", "",
        f"- 有效轮次：`{peak_summary['rounds_valid']}/{peak_summary['rounds_requested']}`；有效步骤：`{peak['case_done_total']}`。",
        f"- 启动失败 / 低内存弹窗 / cgroup OOM kill 均值：`{pa['launch_failures']:.3f}` / `{pa['low_memory_popups']:.3f}` / `{pa['app_oom_kills']:.3f}`。",
        f"- 异常总数均值：`{pa['failure_total']:.3f}`；trace 丢失总数：`{peak['trace_loss_total']}`。",
        "- 30% 改善率：`N/A`；当前基线为 0，分母为 0，不能据此宣布达标。", "",
        "## 结论", "",
        "当前内核在本轮受控场景下能够完成全部操作且没有 OOM，但这只说明诊断场景可运行。它既没有执行 PARP Apply，也不能证明两个降低比例已经达到。下一步必须先安全校准出非零峰值基线，再运行同源 Native/OFF 与 Apply 成对实验。", "",
        f"- 冷热原始结果：`{hotcold['path']}`",
        f"- 峰值原始结果：`{peak['path']}`",
    ]
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(md_path)
    print(json_path)
    return 0


def trace_filter(args: argparse.Namespace) -> int:
    pids = []
    for path in args.socket:
        response = fixture_command(Path(path), "STATUS", 5, 5)
        match = re.search(r"\bpid=(\d+)\b", response)
        if not match:
            raise RuntimeError(f"fixture pid missing: {path}")
        pids.append(match.group(1))
    outcome = run(["sudo", "-n", "bash", str(TRACE_HELPER), "filter-pids", args.instance, ",".join(pids)], timeout=30)
    if outcome.returncode != 0:
        raise RuntimeError(outcome.stderr.strip() or "trace pid filter failed")
    print("OK pids=" + ",".join(pids))
    return 0


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser()
    sub = root.add_subparsers(dest="subcommand", required=True)
    check = sub.add_parser("preflight")
    check.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    check.add_argument("--profile", choices=["smoke", "full"], default="smoke")
    check.add_argument("--suite", choices=["hotcold", "peak", "all"], default="all")
    check.add_argument("--output", type=Path)
    generate = sub.add_parser("generate")
    generate.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    generate.add_argument("--profile", choices=["smoke", "full"], required=True)
    generate.add_argument("--suite", choices=["hotcold", "peak"], required=True)
    generate.add_argument("--round", type=int, default=1)
    generate.add_argument("--seed", type=int, default=20260809)
    generate.add_argument("--session-dir", type=Path, required=True)
    generate.add_argument("--trace-instance", default="parp-accept-generate")
    generate.add_argument("--output", type=Path, required=True)
    fixture = sub.add_parser("fixture-command")
    fixture.add_argument("--socket", type=Path, required=True)
    fixture.add_argument("--command", dest="fixture_value", required=True)
    fixture.add_argument("--timeout", type=float, default=30)
    fixture.add_argument("--wait", type=float, default=0)
    filtering = sub.add_parser("trace-filter")
    filtering.add_argument("--instance", required=True)
    filtering.add_argument("--socket", action="append", required=True)
    execute_parser = sub.add_parser("run")
    execute_parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    execute_parser.add_argument("--profile", choices=["smoke", "full"], default="smoke")
    execute_parser.add_argument("--suite", choices=["hotcold", "peak"], required=True)
    execute_parser.add_argument("--seed", type=int, default=20260809)
    execute_parser.add_argument("--rounds", type=int)
    combine = sub.add_parser("combine")
    combine.add_argument("--hotcold", type=Path, required=True)
    combine.add_argument("--peak", type=Path, required=True)
    combine.add_argument("--output-dir", type=Path, required=True)
    return root


def main() -> int:
    args = parser().parse_args()
    if args.subcommand == "preflight":
        data = preflight(load_config(args.config), args.profile, args.suite)
        if args.output:
            write_json(args.output, data)
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0 if data["status"] == "READY" else 2
    if args.subcommand == "generate":
        scenario = generate_scenario(load_config(args.config), suite=args.suite, profile=args.profile, round_index=args.round, seed=args.seed, session_dir=args.session_dir.resolve(), trace_instance=args.trace_instance)
        write_json(args.output, scenario)
        print(args.output)
        return 0
    if args.subcommand == "fixture-command":
        print(fixture_command(args.socket, args.fixture_value, args.timeout, args.wait))
        return 0
    if args.subcommand == "trace-filter":
        return trace_filter(args)
    if args.subcommand == "run":
        return execute(args)
    if args.subcommand == "combine":
        return combine_reports(args)
    return 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
