from __future__ import annotations

import argparse
import json
from pathlib import Path

from .readers import read_key_values, read_pressure


def memory_environment(
    proc_root: Path = Path("/proc"),
    cgroup_root: Path = Path("/sys/fs/cgroup"),
) -> dict[str, object]:
    meminfo = read_key_values(proc_root / "meminfo")
    try:
        pressure = read_pressure(proc_root / "pressure" / "memory")
    except (FileNotFoundError, PermissionError, OSError):
        pressure = {}
    cgroup_path = "/"
    try:
        for line in (proc_root / "self" / "cgroup").read_text(encoding="utf-8").splitlines():
            if line.startswith("0::"):
                cgroup_path = line[3:] or "/"
                break
    except (FileNotFoundError, PermissionError, OSError):
        pass
    current = cgroup_root / cgroup_path.lstrip("/")
    limits: list[dict[str, object]] = []
    while True:
        limit_file = current / "memory.max"
        try:
            raw = limit_file.read_text(encoding="utf-8").strip()
        except (FileNotFoundError, PermissionError, OSError):
            raw = "max"
        if raw != "max":
            try:
                limits.append({"path": str(current), "memory_max_bytes": int(raw)})
            except ValueError:
                pass
        if current == cgroup_root or current.parent == current:
            break
        current = current.parent
    host_total_bytes = meminfo.get("MemTotal", 0) * 1024
    finite_limits = [int(value["memory_max_bytes"]) for value in limits]
    effective_bytes = min([host_total_bytes, *finite_limits]) if host_total_bytes else min(finite_limits, default=0)
    return {
        "mem_total_gib": host_total_bytes / 1024**3,
        "mem_available_gib": meminfo.get("MemAvailable", 0) / 1024**2,
        "cgroup_path": cgroup_path,
        "cgroup_memory_limits": limits,
        "effective_memory_gib": effective_bytes / 1024**3,
        "memory_pressure": pressure,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Refuse paper-aligned runs on an unconstrained large-memory host")
    parser.add_argument("--max-memory-gb", type=float, required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--allow-unconstrained", action="store_true")
    args = parser.parse_args(argv)
    environment = memory_environment()
    effective_gib = float(environment["effective_memory_gib"])
    valid = effective_gib <= args.max_memory_gb or args.allow_unconstrained
    value = {
        **environment,
        "max_paper_aligned_memory_gib": args.max_memory_gb,
        "override_allow_unconstrained": args.allow_unconstrained,
        "valid": valid,
        "reason": None if valid else "host memory exceeds the scenario ceiling; use a boot/cgroup memory limit",
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if not valid:
        print(
            f"Refusing unconstrained run: effective memory {effective_gib:.1f} GiB exceeds "
            f"the {args.max_memory_gb:.1f} GiB scenario ceiling. Apply a memory limit or set "
            "ALLOW_UNCONSTRAINED_MEMORY=1 for a non-paper-aligned smoke test.",
        )
    return 0 if valid else 5


if __name__ == "__main__":
    raise SystemExit(main())
