#!/usr/bin/env python3
"""Compare four paired R12 GUI/OOM rounds under one frozen pressure contract."""

from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path
from typing import Any


MIB = 1024 * 1024


def read(path: Path) -> dict[str, Any] | list[dict[str, Any]]:
    return json.loads(path.read_text(encoding="utf-8"))


def valid_runs(session: Path, expected_policy: str) -> tuple[dict[str, Any], dict[int, dict[str, Any]]]:
    summary = read(session / "summary.json")
    if summary["policy"] != expected_policy:
        raise ValueError(f"{session}: expected {expected_policy}, got {summary['policy']}")
    rows: dict[int, dict[str, Any]] = {}
    for row in summary["runs"]:
        if not row.get("valid"):
            continue
        seed = int(row["seed"])
        if seed in rows:
            raise ValueError(f"{session}: duplicate valid seed {seed}")
        rows[seed] = row
    return summary, rows


def round_metrics(row: dict[str, Any]) -> dict[str, Any]:
    root = Path(row["run_dir"])
    recovery = read(root / "r12-recovery.json")
    monitor = read(root / "monitor.json")
    before = read(root / "r8-before-pressure.json")
    after = read(root / "r8-after-pressure.json")
    environment = read(root / "environment.json")
    pressure = read(root / "r8-pressure.json")
    if not recovery["valid"] or not pressure["pressure_complete"]:
        raise ValueError(f"{root}: recovery or pressure evidence incomplete")
    bstat = before["cgroup"]["memory_stat"]
    astat = after["cgroup"]["memory_stat"]
    apps = recovery["apps"]
    survivor_times = [float(data["response_ms"]) for data in apps.values() if not data["oom_victim"]]
    victim_times = [float(data["recovery_ms"]) for data in apps.values() if data["oom_victim"]]
    return {
        "seed": int(row["seed"]), "kernel": environment["kernel_release"],
        "run_dir": str(root), "action_plan_sha256": row["action_plan_sha256"],
        "victim_apps": sorted(row["victim_apps"]),
        "victim_count": len(row["victim_apps"]),
        "survivor_response_mean_ms": statistics.mean(survivor_times) if survivor_times else None,
        "victim_restart_mean_ms": statistics.mean(victim_times) if victim_times else None,
        "oom_event_delta": int(row["oom_event_delta"]),
        "oom_kill_delta": int(row["oom_kill_delta"]),
        "oom_group_kill_delta": int(row["oom_group_kill_delta"]),
        "psi_some_avg10_peak": max(float(sample["psi"]["some_avg10"]) for sample in monitor),
        "psi_full_avg10_peak": max(float(sample["psi"]["full_avg10"]) for sample in monitor),
        "swapfree_start_mib": int(monitor[0]["swapfree"]) / MIB,
        "swapfree_min_mib": min(int(sample["swapfree"]) for sample in monitor) / MIB,
        "pgfault_delta": int(astat.get("pgfault", 0)) - int(bstat.get("pgfault", 0)),
        "pgmajfault_delta": int(astat.get("pgmajfault", 0)) - int(bstat.get("pgmajfault", 0)),
        "pswpout_delta": int(astat.get("pswpout", 0)) - int(bstat.get("pswpout", 0)),
        "pressure_committed_mib": int(pressure["pressure_committed_bytes"]) // MIB,
        "apps": apps,
    }


def mean(values: list[float | int | None]) -> float | None:
    present = [float(value) for value in values if value is not None]
    return statistics.mean(present) if present else None


def fmt(value: float | int | None, digits: int = 1) -> str:
    return "—" if value is None else f"{value:.{digits}f}"


def compare(parp_session: Path, native_session: Path, output: Path, pairs: int = 4) -> dict[str, Any]:
    parp_summary, parp_runs = valid_runs(parp_session, "current_kernel")
    native_summary, native_runs = valid_runs(native_session, "native_kernel")
    if parp_summary["config_sha256"] != native_summary["config_sha256"]:
        raise ValueError("frozen config hashes differ")
    seeds = sorted(parp_runs)
    if len(seeds) != pairs or sorted(native_runs) != seeds:
        raise ValueError(f"expected exactly {pairs} matching valid seeds; PARP={seeds}, Native={sorted(native_runs)}")
    paired: list[dict[str, Any]] = []
    for seed in seeds:
        parp = round_metrics(parp_runs[seed])
        native = round_metrics(native_runs[seed])
        if "parp-lzx" not in parp["kernel"] or native["kernel"] != "6.17.13-native-6.17.13":
            raise ValueError(f"seed {seed}: running kernel identity does not match PARP/Native")
        if parp["action_plan_sha256"] != native["action_plan_sha256"]:
            raise ValueError(f"seed {seed}: action plans differ")
        if parp["pressure_committed_mib"] != native["pressure_committed_mib"]:
            raise ValueError(f"seed {seed}: committed pressure differs")
        common = [
            app for app in parp["apps"]
            if not parp["apps"][app]["oom_victim"] and not native["apps"][app]["oom_victim"]
        ]
        paired.append({
            "seed": seed, "parp": parp, "native": native,
            "common_survivor_apps": common,
            "common_survivor_response_mean_ms": {
                "parp": mean([parp["apps"][app]["response_ms"] for app in common]),
                "native": mean([native["apps"][app]["response_ms"] for app in common]),
            },
        })
    keys = (
        "victim_count", "survivor_response_mean_ms", "victim_restart_mean_ms",
        "oom_event_delta", "oom_kill_delta", "oom_group_kill_delta",
        "psi_some_avg10_peak", "psi_full_avg10_peak", "swapfree_start_mib", "swapfree_min_mib",
        "pgfault_delta", "pgmajfault_delta", "pswpout_delta",
    )
    averages = {
        policy: {key: mean([pair[policy][key] for pair in paired]) for key in keys}
        for policy in ("parp", "native")
    }
    averages["common_survivor_response_mean_ms"] = {
        policy: mean([pair["common_survivor_response_mean_ms"][policy] for pair in paired])
        for policy in ("parp", "native")
    }
    payload = {
        "schema_version": 1, "status": "COMPLETE", "pairs": pairs, "seeds": seeds,
        "config_sha256": parp_summary["config_sha256"],
        "parp_session": str(parp_session), "native_session": str(native_session),
        "attempts": {
            "parp": len(parp_summary["runs"]),
            "native": len(native_summary["runs"]),
        },
        "valid_round_corrections": {
            policy: [
                {"seed": seed, "correction": runs[seed]["analysis_correction"]}
                for seed in seeds if runs[seed].get("analysis_correction")
            ]
            for policy, runs in (("parp", parp_runs), ("native", native_runs))
        },
        "rounds": paired, "averages": averages,
    }
    output.mkdir(parents=True, exist_ok=True)
    (output / "comparison.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    labels = {
        "victim_count": "OOM 应用数", "survivor_response_mean_ms": "存活应用响应均值 (ms)",
        "victim_restart_mean_ms": "被杀应用重启均值 (ms)", "oom_event_delta": "memcg oom 增量",
        "oom_kill_delta": "memcg oom_kill 增量", "oom_group_kill_delta": "memcg oom_group_kill 增量",
        "psi_some_avg10_peak": "PSI some avg10 峰值 (%)", "psi_full_avg10_peak": "PSI full avg10 峰值 (%)",
        "swapfree_start_mib": "全机 SwapFree 轮次起点 (MiB)",
        "swapfree_min_mib": "全机 SwapFree 最低值 (MiB)", "pgfault_delta": "缺页增量",
        "pgmajfault_delta": "重大缺页增量", "pswpout_delta": "swap out 页增量",
    }
    lines = [
        "# R12：PARP 与 Native 四组配对比较", "",
        f"状态：**COMPLETE**；4 个有效配对 seed：{', '.join(map(str, seeds))}。两个内核使用同一冻结配置和逐轮相同的动作计划、申请总量。",
        "", f"PARP 共尝试 {len(parp_summary['runs'])} 轮，Native 共尝试 {len(native_summary['runs'])} 轮；各自只纳入 4 轮有效结果。PARP 有效轮须有 3–4 个不同应用 OOM，Native 只要求场景与证据完整，因此 OOM 数比较以这 4 个 PARP 入选 seed 为条件。样本量只有 4，不作统计显著性结论。", "",
        "| 指标 | PARP | Native | Native − PARP |", "|---|---:|---:|---:|",
    ]
    for key, label in labels.items():
        left, right = averages["parp"][key], averages["native"][key]
        lines.append(f"| {label} | {fmt(left)} | {fmt(right)} | {fmt(right-left if left is not None and right is not None else None)} |")
    left = averages["common_survivor_response_mean_ms"]["parp"]
    right = averages["common_survivor_response_mean_ms"]["native"]
    lines += [
        f"| 两内核共同存活应用的响应均值 (ms) | {fmt(left)} | {fmt(right)} | {fmt(right-left if left is not None and right is not None else None)} |",
        "", f"全机 SwapFree 在轮次开始时的均值相差 {fmt(averages['native']['swapfree_start_mib'] - averages['parp']['swapfree_start_mib'])} MiB。这个宿主状态差异可能影响 OOM 与响应时间；延迟均值的差不能单独归因于内核。",
        "", "Native seed 20260928 曾因采集结束后的 slice 清理超时被误判无效；该轮压力、OOM 归因、动作哈希、监控及 12 应用恢复均已在清理前完成。原始判定和更正审计记录均保留在该轮目录。",
        "", "## 每组结果", "",
        "| seed | PARP OOM 应用 | Native OOM 应用 | PARP 共同存活响应 (ms) | Native 共同存活响应 (ms) |",
        "|---:|---|---|---:|---:|",
    ]
    for pair in paired:
        common_times = pair["common_survivor_response_mean_ms"]
        lines.append(
            f"| {pair['seed']} | {', '.join(pair['parp']['victim_apps']) or '无'} | "
            f"{', '.join(pair['native']['victim_apps']) or '无'} | "
            f"{fmt(common_times['parp'])} | {fmt(common_times['native'])} |"
        )
    lines += [
        "", "共同存活响应只比较同一 seed 中两内核都未被 OOM 杀死的应用；被杀应用重启时间只在各自内核确实发生 OOM 时计入均值。",
        "", f"冻结配置 SHA-256：`{parp_summary['config_sha256']}`。完整逐应用数据、OOM trace、PSI 与 cgroup 快照保存在两个会话的轮次目录。", "",
    ]
    (output / "comparison.md").write_text("\n".join(lines), encoding="utf-8")
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parp-session", required=True, type=Path)
    parser.add_argument("--native-session", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--pairs", type=int, default=4)
    args = parser.parse_args()
    payload = compare(args.parp_session.resolve(), args.native_session.resolve(), args.output.resolve(), args.pairs)
    print(args.output.resolve() / "comparison.md")
    return 0 if payload["status"] == "COMPLETE" else 1


if __name__ == "__main__":
    raise SystemExit(main())
