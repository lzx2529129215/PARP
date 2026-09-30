#!/usr/bin/env python3
"""Report collector prerequisites without treating optional capabilities as fatal."""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path


MANDATORY_FAILURE = False


def report(level: str, label: str, detail: str) -> None:
    print(f"[{level}] {label}: {detail}")


def check(level: str, label: str, passed: bool, detail: str, *, mandatory=False) -> None:
    global MANDATORY_FAILURE
    report(level if passed else ("FAIL" if mandatory else "WARN"), label, detail)
    if mandatory and not passed:
        MANDATORY_FAILURE = True


def command_exists(name: str) -> bool:
    return shutil.which(name) is not None


def kallsyms_has(symbol: str) -> bool:
    path = Path("/proc/kallsyms")
    try:
        return any(line.rstrip().endswith(" " + symbol) for line in path.open(errors="replace"))
    except OSError:
        return False


def trace_event_exists(group: str, event: str) -> bool:
    for root in (Path("/sys/kernel/tracing/events"), Path("/sys/kernel/debug/tracing/events")):
        if (root / group / event).is_dir():
            return True
    return False


def main() -> int:
    is_linux = sys.platform.startswith("linux")
    check("PASS", "Linux", is_linux, platform.platform(), mandatory=True)
    check("PASS", "Python", sys.version_info >= (3, 8), sys.version.split()[0], mandatory=True)
    cgroup = Path("/sys/fs/cgroup")
    cgroup_v2 = (cgroup / "cgroup.controllers").is_file()
    check("PASS", "cgroup v2", cgroup_v2, str(cgroup / "cgroup.controllers"), mandatory=True)
    check("PASS", "cgroup filesystem readable", os.access(cgroup, os.R_OK | os.X_OK), str(cgroup), mandatory=True)
    check("PASS", "BTF vmlinux", Path("/sys/kernel/btf/vmlinux").is_file(),
          "optional for this non-CO-RE build")
    check("PASS", "system memory PSI", Path("/proc/pressure/memory").is_file(), "/proc/pressure/memory")
    check("PASS", "clear_refs for current process", os.access(Path("/proc/self/clear_refs"), os.W_OK),
          "/proc/self/clear_refs")
    check("PASS", "smaps for current process", os.access(Path("/proc/self/smaps"), os.R_OK),
          "/proc/self/smaps")
    for tool in ("clang", "llvm-config", "make", "gcc", "pkg-config"):
        check("PASS", tool, command_exists(tool), "found in PATH" if command_exists(tool) else "not found in PATH")
    libbpf = command_exists("pkg-config") and subprocess.run(
        ["pkg-config", "--exists", "libbpf"], check=False).returncode == 0
    check("PASS", "libbpf development files", libbpf, "pkg-config libbpf")
    check("PASS", "bpftool", command_exists("bpftool"), "optional diagnostic/build helper")
    check("PASS", "tracepoint exceptions/page_fault_user",
          trace_event_exists("exceptions", "page_fault_user"), "tracefs event directory")
    for symbol in ("__vmf_anon_prepare", "filemap_fault", "do_wp_page", "shmem_fault"):
        check("PASS", f"kprobe {symbol}", kallsyms_has(symbol), "/proc/kallsyms")
    report("PASS" if not MANDATORY_FAILURE else "FAIL", "overall mandatory prerequisites",
           "ready" if not MANDATORY_FAILURE else "missing Linux, Python, or cgroup-v2 prerequisite")
    return 1 if MANDATORY_FAILURE else 0


if __name__ == "__main__":
    raise SystemExit(main())
