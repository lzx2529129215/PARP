#!/usr/bin/env python3
"""Validate exact scenario pairing and compare Native/OFF with one PARP variant."""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
from pathlib import Path
from typing import Any


def load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def canonical_hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def metric(result: dict[str, Any], suite: str) -> float:
    if suite == "hotcold":
        return float(result["trace"]["page_fault_user"])
    return float(result["events"]["failure_total"])


def normalized_kernel_config(metadata: dict[str, Any]) -> dict[str, str] | None:
    config_path = metadata.get("kernel_config", {}).get("path")
    if not config_path:
        return None
    try:
        lines = Path(str(config_path)).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    values: dict[str, str] = {}
    for line in lines:
        if line.startswith("CONFIG_") and "=" in line:
            name, value = line.split("=", 1)
        elif line.startswith("# CONFIG_") and line.endswith(" is not set"):
            name = line[2:-11]
            value = "n"
        else:
            continue
        if name == "CONFIG_LOCALVERSION" or name.startswith("CONFIG_PARP"):
            continue
        values[name] = value
    return values


def compatible_metadata(baseline_root: Path, optimized_root: Path) -> tuple[bool, list[str]]:
    baseline = load(baseline_root / "system-metadata-lzx.json")
    optimized = load(optimized_root / "system-metadata-lzx.json")
    keys = ("hostname", "machine", "cpu_model", "cpu_count", "page_size", "vm_sysctls", "swap", "transparent_hugepage", "cpu_governors")
    differences = [key for key in keys if baseline.get(key) != optimized.get(key)]
    if abs(int(baseline.get("memtotal_bytes", 0)) - int(optimized.get("memtotal_bytes", 0))) > 64 * 1024**2:
        differences.append("memtotal_bytes")
    # lzx-note: Native is a pristine v6.17.13 image; compare configs after
    # removing the intentional PARP and local-version differences.
    baseline_config = normalized_kernel_config(baseline)
    optimized_config = normalized_kernel_config(optimized)
    if baseline_config is None or optimized_config is None or baseline_config != optimized_config:
        differences.append("kernel_config")
    return not differences, differences


def build_pair(baseline_path: Path, optimized_path: Path) -> dict[str, Any]:
    baseline = load(baseline_path)
    optimized = load(optimized_path)
    baseline_root = baseline_path.parent
    optimized_root = optimized_path.parent
    errors: list[str] = []
    suite = str(baseline.get("suite", ""))
    if suite != optimized.get("suite") or baseline.get("profile") != optimized.get("profile"):
        errors.append("suite/profile mismatch")
    if baseline.get("variant") != "native":
        errors.append(f"baseline variant must be native, got {baseline.get('variant')}")
    if optimized.get("variant") not in {"effective", "tier2", "tier2_bin", "combined", "combined_no_reclaim"}:
        errors.append(f"optimized variant is not a treatment: {optimized.get('variant')}")
    metadata_ok, metadata_differences = compatible_metadata(baseline_root, optimized_root)
    if not metadata_ok:
        errors.append("system metadata mismatch: " + ",".join(metadata_differences))
    baseline_results = baseline.get("results", [])
    optimized_results = optimized.get("results", [])
    if len(baseline_results) != len(optimized_results):
        errors.append("round count mismatch")
    pairs: list[dict[str, Any]] = []
    for index, (base_result, opt_result) in enumerate(zip(baseline_results, optimized_results), start=1):
        base_plan_path = baseline_root / f"round-{index:02d}" / "scenario-plan.json"
        opt_plan_path = optimized_root / f"round-{index:02d}" / "scenario-plan.json"
        if not base_plan_path.is_file() or not opt_plan_path.is_file():
            errors.append(f"round {index}: scenario plan missing")
            continue
        base_plan = load(base_plan_path)
        opt_plan = load(opt_plan_path)
        base_hash = canonical_hash(base_plan)
        opt_hash = canonical_hash(opt_plan)
        if base_hash != opt_hash:
            errors.append(f"round {index}: scenario plan hash mismatch")
        if base_result.get("status") != "VALID_DIAGNOSTIC" or opt_result.get("status") != "VALID_DIAGNOSTIC":
            errors.append(f"round {index}: one or both rounds invalid")
        base_value = metric(base_result, suite)
        opt_value = metric(opt_result, suite)
        pairs.append({
            "round": index, "scenario_plan_sha256": base_hash,
            "baseline": base_value, "optimized": opt_value,
            "delta": opt_value - base_value,
            "improvement_percent": ((base_value - opt_value) / base_value * 100.0) if base_value else None,
        })
    baseline_values = [pair["baseline"] for pair in pairs]
    optimized_values = [pair["optimized"] for pair in pairs]
    baseline_mean = statistics.mean(baseline_values) if baseline_values else None
    optimized_mean = statistics.mean(optimized_values) if optimized_values else None
    improvement = (
        (baseline_mean - optimized_mean) / baseline_mean * 100.0
        if baseline_mean not in (None, 0) and optimized_mean is not None else None
    )
    target = 20.0 if suite == "hotcold" else 30.0
    challenge = 30.0 if suite == "hotcold" else None
    if baseline_mean == 0:
        errors.append("baseline mean is zero; improvement ratio is not evaluable")
    return {
        "status": "VALID_PAIR" if not errors else "INVALID_PAIR",
        "suite": suite,
        "profile": baseline.get("profile"),
        "baseline_variant": baseline.get("variant"),
        "optimized_variant": optimized.get("variant"),
        "baseline_summary": str(baseline_path.resolve()),
        "optimized_summary": str(optimized_path.resolve()),
        "rounds": len(pairs),
        "scenario_plans_identical": not any("plan" in error for error in errors),
        "metadata_identical": metadata_ok,
        "metric": "trace_page_fault_user" if suite == "hotcold" else "failure_total",
        "baseline_mean": baseline_mean,
        "optimized_mean": optimized_mean,
        "improvement_percent": improvement,
        "target_percent": target,
        "challenge_percent": challenge,
        "target_met": improvement is not None and improvement >= target and not errors,
        "challenge_met": challenge is not None and improvement is not None and improvement >= challenge and not errors,
        "errors": errors,
        "pairs": pairs,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--optimized", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    report = build_pair(args.baseline, args.optimized)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    json_path = args.output_dir / "paired-report-lzx.json"
    md_path = args.output_dir / "paired-report-lzx.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    improvement = "N/A" if report["improvement_percent"] is None else f"{report['improvement_percent']:.2f}%"
    lines = [
        "# Linux 6.17.13 Native 与 PARP Apply 配对报告", "",
        f"- 状态：`{report['status']}`。",
        f"- 变体：`{report['baseline_variant']}` → `{report['optimized_variant']}`。",
        f"- 场景计划逐轮相同：`{report['scenario_plans_identical']}`；系统环境与非PARP配置兼容：`{report['metadata_identical']}`。",
        f"- 指标：`{report['metric']}`；基线均值：`{report['baseline_mean']}`；优化均值：`{report['optimized_mean']}`。",
        f"- 改善率：`{improvement}`；目标：`{report['target_percent']:.0f}%`；达标：`{report['target_met']}`。", "",
    ]
    if report["errors"]:
        lines += ["## 阻断原因", ""] + [f"- {error}" for error in report["errors"]] + [""]
    md_path.write_text("\n".join(lines), encoding="utf-8")
    print(md_path)
    return 0 if report["status"] == "VALID_PAIR" else 1


if __name__ == "__main__":
    raise SystemExit(main())
