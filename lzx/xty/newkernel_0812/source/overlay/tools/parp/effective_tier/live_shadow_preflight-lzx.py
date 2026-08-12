#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0  #lzx
"""Create a read-only, fail-closed Phase-F live-shadow preflight record.

This tool deliberately does not install a kernel, write a cgroup, toggle a
PARP mode, enable tracepoints, start pressure, or reboot.  It captures the
state required to decide whether the separately authorized root-only phase can
begin, and creates an honest output skeleton with every unmeasured metric left
null.  It is safe to run before an interactive sudo hand-off.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


BASELINE_HEAD = "aa39d99696392c0bfaba5df2e6152e24a9f88a6d"
BRANCH = "feat/parp-effective-tier-live-shadow"
PRESSURE_PROVENANCE = "ENGINEERING_PRESSURE_POLICY_UNVALIDATED"
SCOPE_NAMES = (
    "huawei-test.slice",
    "automation-wps.scope",
    "automation-files.scope",
    "automation-qq.scope",
)
CGROUP_VALUES = (
    "memory.low", "memory.min", "memory.high", "memory.max",
    "memory.swap.max", "memory.current", "memory.events",
    "memory.events.local", "memory.stat", "cpuset.cpus",
    "cpuset.mems", "cpu.max", "io.max",
)


def _run(argv: Sequence[str]) -> Dict[str, object]:
    """Run a bounded read-only inspection command without a shell."""

    try:
        process = subprocess.run(argv, text=True, encoding="utf-8",
                                 errors="replace", stdin=subprocess.DEVNULL,
                                 stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, check=False)
    except OSError as exc:
        return {"argv": list(argv), "executed": False, "error": str(exc)}
    return {
        "argv": list(argv),
        "executed": True,
        "returncode": process.returncode,
        "stdout": process.stdout.strip(),
        "stderr": process.stderr.strip(),
    }


def _text(path: Path) -> Optional[str]:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None


def _path_state(path: Path) -> Dict[str, object]:
    """Distinguish an absent trace/debug file from an unreadable one."""

    try:
        path.stat()
    except PermissionError:
        return {"exists": True, "readable": False}
    except OSError:
        return {"exists": False, "readable": False}
    return {"exists": True, "readable": _text(path) is not None}


def _key_value_text(text: Optional[str]) -> Dict[str, str]:
    """Parse the simple, read-only PARP debugfs key/value format."""

    values: Dict[str, str] = {}
    for line in (text or "").splitlines():
        key, separator, value = line.partition(" ")
        if key and separator:
            values[key] = value.strip()
    return values


def _sha256(path: Path) -> Optional[str]:
    try:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()
    except OSError:
        return None


def _git(tree: Path, *args: str) -> Optional[str]:
    result = _run(["git", "-C", str(tree), *args])
    if result.get("returncode") != 0:
        return None
    return str(result.get("stdout", ""))


def _find_cgroups(root: Path) -> Dict[str, List[Path]]:
    found: Dict[str, List[Path]] = {name: [] for name in SCOPE_NAMES}
    if not root.is_dir():
        return found
    for current, directories, _files in os.walk(str(root)):
        for name in directories:
            if name in found:
                found[name].append(Path(current) / name)
    return found


def _cgroup_snapshot(paths: Iterable[Path]) -> Dict[str, object]:
    entries = []
    for path in sorted(paths):
        values = {name: _text(path / name) for name in CGROUP_VALUES}
        procs = _text(path / "cgroup.procs")
        entries.append({
            "path": str(path),
            "control_values": values,
            "process_count": (len([line for line in (procs or "").splitlines()
                                   if line]) if procs is not None else None),
            "children": sorted(child.name for child in path.iterdir()
                               if child.is_dir()),
        })
    return {"entries": entries, "found": bool(entries)}


def _kernel_config(config: Optional[Path]) -> Dict[str, object]:
    requested = (
        "CONFIG_PARP",
        "CONFIG_PARP_EFFECTIVE_TIER",
        "CONFIG_PARP_EFFECTIVE_TIER_EXPERIMENTAL_APPLY",
        "CONFIG_DEBUG_FS",
        "CONFIG_FTRACE",
        "CONFIG_KUNIT",
        "CONFIG_LOCALVERSION",
    )
    lines: Dict[str, str] = {}
    if config is not None:
        content = _text(config)
        if content is not None:
            for line in content.splitlines():
                if "=" in line:
                    name, value = line.split("=", 1)
                    if name in requested:
                        lines[name] = value
                elif line.startswith("# ") and line.endswith(" is not set"):
                    name = line[2:-11]
                    if name in requested:
                        lines[name] = "n"
    return {
        "path": str(config) if config else None,
        "sha256": _sha256(config) if config else None,
        "values": {name: lines.get(name) for name in requested},
    }


def _artifact(path: Optional[Path]) -> Dict[str, object]:
    if path is None:
        return {"path": None, "exists": False, "size": None, "sha256": None}
    try:
        size = path.stat().st_size
    except OSError:
        size = None
    return {
        "path": str(path),
        "exists": size is not None,
        "size": size,
        "sha256": _sha256(path) if size is not None else None,
    }


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8")


def _placeholder(name: str) -> Dict[str, object]:
    return {
        "artifact": name,
        "status": "NOT_COLLECTED",
        "reason": "root-only Phase-F boot and live collection have not run",
        "values": None,
    }


def _summary(tree: Path, output: Path, head: Optional[str], kernel_release: str,
             blockers: Sequence[str]) -> Dict[str, object]:
    return {
        "status": "PARP_EFFECTIVE_TIER_BOOT_ENVIRONMENT_BLOCKED",
        "baseline_head": BASELINE_HEAD,
        "final_head": head,
        "branch": BRANCH,
        "worktree": str(tree),
        "kernel_release": kernel_release,
        "running_target_kernel": False,
        "old_kernel_preserved": False,
        "rollback_verified": False,
        "kernel_installed": False,
        "reboot_executed": False,
        "cgroup_modified": False,
        "cgroup_restored": False,
        "pressure_executed": False,
        "off_sessions": 0,
        "shadow_sessions": 0,
        "wps_sessions": 0,
        "files_sessions": 0,
        "qq_sessions": 0,
        "p2_sessions": 0,
        "p3_sessions": 0,
        "candidate_rows": 0,
        "candidate_base_pages": 0,
        "features_valid_rate": None,
        "access_join_coverage": None,
        "trace_lost_measured": False,
        "trace_lost_records": None,
        "decision_coverage": None,
        "train_sessions": 0,
        "validation_sessions": 0,
        "test_sessions": 0,
        "sampled_pairs": 0,
        "selected_pair_cap": None,
        "selected_tie_margin_ms": None,
        "primary_model": None,
        "pairwise_accuracy_test": None,
        "ndcg_test": None,
        "c_index_test": None,
        "quantized_order_consistency": None,
        "boundary_spearman": None,
        "score_monotonicity_pass": False,
        "score_threshold_cold": None,
        "score_threshold_hot_1": None,
        "score_threshold_hot_2": None,
        "thresholds_selected_on_validation": False,
        "test_set_used_for_selection": False,
        "keep_reclaim_pages": 0,
        "predictive_upgrade_pages": 0,
        "keep_protect_pages": 0,
        "predictive_downgrade_pages": 0,
        "upgrade_hit_rate": None,
        "upgrade_waste_rate": None,
        "downgrade_mistake_rate": None,
        "downgrade_cold_precision": None,
        "pressure_levels_observed": [],
        "pressure_policy_provenance": PRESSURE_PROVENANCE,
        "pressure_aware_ablation_complete": False,
        "score_ns_p50": None,
        "score_ns_p95": None,
        "score_ns_p99": None,
        "lru_lock_held_ns_p95_off": None,
        "lru_lock_held_ns_p99_off": None,
        "lru_lock_held_ns_p95_shadow": None,
        "lru_lock_held_ns_p99_shadow": None,
        "direct_reclaim_ns_p95_off": None,
        "direct_reclaim_ns_p99_off": None,
        "direct_reclaim_ns_p95_shadow": None,
        "direct_reclaim_ns_p99_shadow": None,
        "scan_efficiency_off": None,
        "scan_efficiency_shadow": None,
        "workingset_refault_file_off": None,
        "workingset_refault_file_shadow": None,
        "workingset_refault_anon_off": None,
        "workingset_refault_anon_shadow": None,
        "app_latency_p95_delta_percent": None,
        "app_latency_p99_delta_percent": None,
        "protect_only_validation_gate": False,
        "bidirectional_offline_gate": False,
        "protect_apply_executed": False,
        "bidirectional_apply_executed": False,
        "oom_events": 0,
        "oom_kill_events": 0,
        "kernel_oops": 0,
        "kernel_warnings_related": 0,
        "warnings": list(blockers),
        "output_dir": str(output),
    }


def _sha256sums(output: Path) -> None:
    records = []
    for path in sorted(item for item in output.rglob("*") if item.is_file()
                       and item.name != "SHA256SUMS"):
        digest = _sha256(path)
        if digest is not None:
            records.append("%s  %s" % (digest, path.relative_to(output)))
    (output / "SHA256SUMS").write_text("\n".join(records) + "\n",
                                          encoding="utf-8")


def _under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def create_preflight(tree: Path, output: Path, build_dir: Optional[Path],
                     expected_release: str) -> Dict[str, object]:
    project = tree.parent.parent
    outputs = project / "outputs"
    if not _under(output, outputs):
        raise ValueError("output directory must be below %s" % outputs)
    if output.exists():
        raise ValueError("refusing to overwrite existing output directory")
    output.mkdir(parents=True)
    (output / "raw").mkdir()
    (output / "normalized").mkdir()

    head = _git(tree, "rev-parse", "HEAD")
    status = _git(tree, "status", "--short")
    cgroup_root = Path("/sys/fs/cgroup")
    found = _find_cgroups(cgroup_root)
    cgroup_before = {
        "captured_at_utc": datetime.now(timezone.utc).isoformat(),
        "read_only": True,
        "cgroup_root": str(cgroup_root),
        "targets": {name: _cgroup_snapshot(paths)
                    for name, paths in found.items()},
    }
    blockers: List[str] = []
    sudo = _run(["sudo", "-n", "true"])
    if sudo.get("returncode") != 0:
        blockers.append("noninteractive_sudo_unavailable")
    absent_scopes = [name for name, paths in found.items() if not paths]
    if absent_scopes:
        blockers.append("target_cgroup_scope_missing:" + ",".join(absent_scopes))
    running_release = platform.release()
    if running_release != expected_release:
        blockers.append("target_kernel_not_running")
    debugfs_paths = ["/sys/kernel/debug/parp/effective_tier_mode",
                     "/sys/kernel/debug/parp/effective_tier_config",
                     "/sys/kernel/debug/parp/effective_tier_stats"]
    debugfs_state = {path: _path_state(Path(path)) for path in debugfs_paths}
    if not all(bool(state["readable"]) for state in debugfs_state.values()):
        blockers.append("effective_tier_debugfs_unavailable_or_inaccessible")
    metadata_config = _key_value_text(
        _text(Path("/sys/kernel/debug/parp/effective_tier_config")))
    metadata_ready = metadata_config.get("metadata_ready")
    if running_release == expected_release and metadata_ready != "1":
        blockers.append("effective_tier_metadata_not_boot_reserved")

    repository_audit = {
        "baseline_head": BASELINE_HEAD,
        "head": head,
        "branch": _git(tree, "branch", "--show-current"),
        "worktree": str(tree),
        "worktree_status_short": status.splitlines() if status else None,
        "frozen_worktree_modified": False,
        "preflight_is_read_only": True,
    }
    config = _kernel_config(build_dir / ".config" if build_dir else None)
    build_artifacts = {
        "bzImage": _artifact(build_dir / "arch/x86/boot/bzImage"
                              if build_dir else None),
        "System.map": _artifact(build_dir / "System.map" if build_dir else None),
        "vmlinux": _artifact(build_dir / "vmlinux" if build_dir else None),
        "modules_directory": {
            "path": str(build_dir / "lib/modules" if build_dir else ""),
            "exists": bool(build_dir and (build_dir / "lib/modules").is_dir()),
            "sha256": None,
            "reason": "module trees require a manifest, not a directory hash",
        },
    }
    boot_inspection = {
        "uname": _run(["uname", "-a"]),
        "cmdline": _text(Path("/proc/cmdline")),
        "effective_tier_metadata": {
            "reservation_requested": metadata_config.get(
                "metadata_reservation_requested"),
            "ready": metadata_ready,
            "payload_bytes": metadata_config.get("metadata_payload_bytes"),
            "required_kernel_parameter": "parp_effective_tier_reserve=1",
        },
        "root_filesystem": _run(["findmnt", "-n", "-o", "SOURCE,FSTYPE,OPTIONS", "/"]),
        "boot_space": _run(["df", "-B1", "/boot"]),
        "grub_environment": _run(["grub-editenv", "list"]),
        "secure_boot": _run(["mokutil", "--sb-state"]),
        "boot_kernels": [{"path": str(path), "size": path.stat().st_size,
                           "sha256": _sha256(path)}
                         for path in sorted(Path("/boot").glob("vmlinuz-*"))],
    }
    boot_plan = {
        "status": "NOT_EXECUTED_INTERACTIVE_SUDO_REQUIRED",
        "expected_kernel_release": expected_release,
        "running_kernel_release": running_release,
        "one_time_boot_required": True,
        "permanent_grub_default_change_allowed": False,
        "exact_grub_entry_resolved": False,
        "required_pre_install_checks": boot_inspection,
        "blockers": blockers,
    }
    installed = {
        "status": "NOT_INSTALLED",
        "kernel_release": expected_release,
        "before_boot_kernels": boot_inspection["boot_kernels"],
        "after_boot_kernels": None,
        "modules": None,
        "installation_command_executed": False,
    }
    verification = {
        "status": "NOT_EXECUTED",
        "expected_release": expected_release,
        "running_release": running_release,
        "running_target_kernel": False,
        "debugfs_paths": debugfs_state,
        "apply_modes_executed": False,
    }
    _write_json(output / "repository_audit.json", repository_audit)
    _write_json(output / "build_manifest.json", {
        "status": ("BUILD_ARTIFACTS_AUDITED" if
                   build_artifacts["bzImage"]["exists"] else
                   "PREBUILD_CONFIGURATION_AUDITED"),
        "kernel_config": config,
        "kernel_release_expected": expected_release,
        "artifacts": build_artifacts,
        "head": head,
        "compiler": _run(["cc", "--version"]),
        "linker": _run(["ld", "--version"]),
    })
    _write_json(output / "installed_artifacts.json", installed)
    _write_json(output / "boot_plan.json", boot_plan)
    _write_json(output / "boot_verification.json", verification)
    _write_json(output / "cgroup_before.json", cgroup_before)
    _write_json(output / "cgroup_after.json", {
        "status": "NOT_APPLICABLE_NO_CGROUP_WRITE_OCCURRED",
        "cgroup_modified": False,
        "cgroup_restored": False,
        "configuration_diff": None,
    })
    _write_json(output / "pressure_calibration.json", {
        "status": "NOT_EXECUTED",
        "p0_p3_parameters": None,
        "p4_stop_boundary": "P4/OOM formal testing is prohibited",
        "selected_formal_pressure_levels": [],
        "reason": "target kernel, target scopes, and root hand-off unavailable",
    })
    _write_json(output / "session_manifest.json", {
        "status": "NOT_EXECUTED", "sessions": [], "apply_modes_allowed": False,
    })
    _write_json(output / "trace_coverage.json", _placeholder("trace_coverage"))
    _write_json(output / "observability.json", _placeholder("observability"))
    for filename in (
            "ranking_dataset.json", "split_manifest.json", "censoring_report.json",
            "pair_sampling.json", "ranking_model.json", "global_model.json",
            "ranking_quality.json", "ranking_quantization.json",
            "score_distribution.json", "score_reuse_monotonicity.json",
            "threshold_selection.json", "four_quadrants.json",
            "pressure_policy_ablation.json", "score_latency.json",
            "lock_latency.json", "reclaim_latency.json",
            "reclaim_efficiency.json", "refault_metrics.json",
            "app_latency.json", "system_overhead.json"):
        _write_json(output / filename, _placeholder(filename))
    _write_json(output / "tests.json", {
        "status": "NOT_RUN_BY_PREFLIGHT",
        "note": "source tests and build results are recorded separately",
    })
    _write_json(output / "abort_reason.json", {
        "aborted": False,
        "reason": None,
        "note": "no pressure or live session was started",
    })
    _write_json(output / "experiment_manifest.json", {
        "status": "PRE_FLIGHT_ONLY",
        "allowed_modes": ["OFF", "SHADOW_EFFECTIVE_TIER"],
        "apply_modes_executed": False,
        "pressure_policy_provenance": PRESSURE_PROVENANCE,
    })
    _write_json(output / "summary.json", _summary(tree, output, head,
                                                    expected_release, blockers))
    (output / "rollback_plan.md").write_text(
        "# Rollback plan\n\n"
        "No test kernel was installed and no reboot was requested by this "
        "preflight. Therefore no rollback action is pending. Before any "
        "future one-time `grub-reboot`, record the exact pre-existing menu "
        "entry and use that recorded entry to return to `%s`. Do not delete "
        "any `/boot` artifact or change GRUB's permanent default.\n" % running_release,
        encoding="utf-8")
    (output / "RESUME_AFTER_REBOOT.md").write_text(
        "# Resume state\n\n"
        "No reboot has been scheduled. The root-only phase is blocked until "
        "a recoverable test-kernel installation path and real target cgroup "
        "scopes are available. APPLY modes remain prohibited.\n",
        encoding="utf-8")
    (output / "FINAL_REPORT.md").write_text(
        "# PARP effective-tier Phase-F preflight\n\n"
        "Status: `PARP_EFFECTIVE_TIER_BOOT_ENVIRONMENT_BLOCKED`. This output "
        "contains a read-only preflight only. No kernel was installed, no "
        "reboot/cgroup/pressure/session occurred, and no APPLY mode ran. "
        "All unmeasured performance, model, future-access, and refault values "
        "remain null or `NOT_COLLECTED`.\n",
        encoding="utf-8")
    _sha256sums(output)
    return {"output": str(output), "blockers": blockers, "head": head}


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--build-dir", type=Path)
    parser.add_argument("--expected-release", required=True)
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    tree = Path(__file__).resolve().parents[3]
    try:
        result = create_preflight(tree, args.output_dir, args.build_dir,
                                  args.expected_release)
    except (OSError, ValueError) as exc:
        print("live-shadow-preflight: %s" % exc, file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
