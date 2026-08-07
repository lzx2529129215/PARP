from __future__ import annotations

import gzip
import json
import os
import platform
from pathlib import Path

from .models import CapabilityReport, CapabilityStatus


CONFIG_KEYS = [
    "CONFIG_DAMON",
    "CONFIG_DAMON_VADDR",
    "CONFIG_DAMON_PADDR",
    "CONFIG_DAMON_SYSFS",
    "CONFIG_TRACEPOINTS",
    "CONFIG_TRACING",
    "CONFIG_FTRACE",
    "CONFIG_BPF",
    "CONFIG_BPF_SYSCALL",
    "CONFIG_DEBUG_INFO_BTF",
]


def _read_kernel_config() -> tuple[str, dict[str, str]]:
    release = platform.uname().release
    candidates = [
        Path("/proc/config.gz"),
        Path(f"/boot/config-{release}"),
        Path("/lib/modules") / release / "build" / ".config",
    ]
    for path in candidates:
        try:
            if path.name == "config.gz":
                text = gzip.decompress(path.read_bytes()).decode("utf-8", errors="replace")
            else:
                text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        configs = {key: "MISSING" for key in CONFIG_KEYS}
        for line in text.splitlines():
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            if key in configs:
                configs[key] = value.strip()
        return str(path), configs
    return "", {key: "UNKNOWN" for key in CONFIG_KEYS}


def _permission(path: Path, mode: int) -> str:
    if not path.exists():
        return "missing"
    return "ok" if os.access(path, mode) else "permission_denied"


def find_tracefs() -> Path:
    for path in (Path("/sys/kernel/tracing"), Path("/sys/kernel/debug/tracing")):
        if path.exists():
            return path
    return Path("/sys/kernel/tracing")


def probe_capabilities() -> CapabilityReport:
    uname_r = platform.uname().release
    config_source, configs = _read_kernel_config()
    damon_sysfs = Path("/sys/kernel/mm/damon/admin")
    tracefs = find_tracefs()
    tracepoint = tracefs / "events" / "damon" / "damon_aggregated"
    format_path = tracepoint / "format"
    errors: list[str] = []

    if not config_source:
        errors.append("kernel config not readable from /proc/config.gz, /boot/config-$(uname -r), or build/.config")

    required_configs = ["CONFIG_DAMON", "CONFIG_DAMON_VADDR", "CONFIG_DAMON_SYSFS", "CONFIG_TRACEPOINTS", "CONFIG_TRACING", "CONFIG_FTRACE"]
    missing_configs = [key for key in required_configs if configs.get(key) not in {"y", "m", "UNKNOWN"}]
    if missing_configs:
        errors.append("missing kernel configs: " + ",".join(missing_configs))

    permissions = {
        "proc_self_maps_read": _permission(Path("/proc/self/maps"), os.R_OK),
        "damon_sysfs_read": _permission(damon_sysfs, os.R_OK),
        "damon_sysfs_write": _permission(damon_sysfs, os.W_OK),
        "tracefs_read": _permission(tracefs, os.R_OK),
        "trace_pipe_read": _permission(tracefs / "trace_pipe", os.R_OK),
        "tracepoint_format_read": _permission(format_path, os.R_OK),
    }

    if missing_configs:
        status = CapabilityStatus.MISSING_CONFIG
    elif not damon_sysfs.exists():
        status = CapabilityStatus.MISSING_SYSFS
        errors.append(f"DAMON sysfs missing: {damon_sysfs}")
    elif not format_path.exists():
        status = CapabilityStatus.MISSING_TRACEPOINT
        errors.append(f"DAMON tracepoint format missing: {format_path}")
    elif any(value == "permission_denied" for key, value in permissions.items() if key in {"damon_sysfs_write", "trace_pipe_read", "tracepoint_format_read"}):
        status = CapabilityStatus.SUPPORTED_NEEDS_ROOT
        errors.append("DAMON/tracefs exists but current user lacks required read/write permission")
    elif permissions["proc_self_maps_read"] != "ok":
        status = CapabilityStatus.PERMISSION_DENIED
        errors.append("/proc/self/maps is not readable")
    else:
        status = CapabilityStatus.SUPPORTED

    return CapabilityReport(
        status=status.value,
        uname_r=uname_r,
        config_source=config_source,
        configs=configs,
        damon_sysfs_path=str(damon_sysfs),
        tracefs_path=str(tracefs),
        tracepoint_path=str(tracepoint),
        permissions=permissions,
        errors=errors,
    )


def write_capability_report(out_dir: Path, report: CapabilityReport) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "capability_report.json").write_text(
        json.dumps(report.to_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    lines = [
        "# Region Monitor capability report",
        "",
        f"- status: {report.status}",
        f"- uname_r: `{report.uname_r}`",
        f"- config_source: `{report.config_source or 'not_found'}`",
        f"- damon_sysfs_path: `{report.damon_sysfs_path}`",
        f"- tracefs_path: `{report.tracefs_path}`",
        f"- tracepoint_path: `{report.tracepoint_path}`",
        "",
        "## Kernel config",
        "",
    ]
    lines.extend(f"- {key}: {value}" for key, value in report.configs.items())
    lines += ["", "## Permissions", ""]
    lines.extend(f"- {key}: {value}" for key, value in report.permissions.items())
    lines += ["", "## Errors", ""]
    lines.extend(f"- {error}" for error in report.errors) if report.errors else lines.append("- none")
    (out_dir / "capability_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    report = probe_capabilities()
    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    return 0 if report.status in {CapabilityStatus.SUPPORTED.value, CapabilityStatus.SUPPORTED_NEEDS_ROOT.value} else 1


if __name__ == "__main__":
    raise SystemExit(main())
