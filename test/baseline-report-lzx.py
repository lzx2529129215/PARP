#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import statistics
from pathlib import Path
from typing import Any, Callable


VALID_STATUS = "VALID_DIAGNOSTIC"


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def metric_stats(results: list[dict[str, Any]], getter: Callable[[dict[str, Any]], float]) -> dict[str, Any]:
    values = [float(getter(item)) for item in results]
    return {
        "mean": statistics.mean(values) if values else 0.0,
        "min": min(values, default=0.0),
        "max": max(values, default=0.0),
        "sample_stdev": statistics.stdev(values) if len(values) > 1 else 0.0,
        "round_values": values,
    }


def monitor_extrema(suite_root: Path) -> dict[str, float]:
    values: dict[str, list[float]] = {
        "memavailable": [],
        "swapfree": [],
        "psi_some_avg10": [],
        "psi_full_avg10": [],
        "memory_current": [],
        "vm_oom_kill": [],
        "pswpin": [],
        "pswpout": [],
        "events_high": [],
        "events_max": [],
        "events_oom": [],
        "events_oom_kill": [],
        "low_memory_popup_count": [],
    }
    delta_keys = ("vm_oom_kill", "pswpin", "pswpout", "events_high", "events_max", "events_oom", "events_oom_kill")
    cumulative_deltas = {key: 0.0 for key in delta_keys}
    for path in sorted(suite_root.glob("round-*/monitor.csv")):
        round_values = {key: [] for key in delta_keys}
        with path.open(encoding="utf-8", newline="") as stream:
            for row in csv.DictReader(stream):
                for key in values:
                    try:
                        value = float(row.get(key, 0) or 0)
                        values[key].append(value)
                        if key in round_values:
                            round_values[key].append(value)
                    except ValueError:
                        pass
        for key, items in round_values.items():
            cumulative_deltas[key] += max(items, default=0.0) - min(items, default=0.0)

    return {
        "min_memavailable_bytes": min(values["memavailable"], default=0.0),
        "min_swapfree_bytes": min(values["swapfree"], default=0.0),
        "max_psi_some_avg10": max(values["psi_some_avg10"], default=0.0),
        "max_psi_full_avg10": max(values["psi_full_avg10"], default=0.0),
        "max_test_cgroup_memory_current_bytes": max(values["memory_current"], default=0.0),
        "host_oom_kill_delta": cumulative_deltas["vm_oom_kill"],
        "pswpin_delta": cumulative_deltas["pswpin"],
        "pswpout_delta": cumulative_deltas["pswpout"],
        "test_cgroup_memory_high_delta": cumulative_deltas["events_high"],
        "test_cgroup_memory_max_delta": cumulative_deltas["events_max"],
        "test_cgroup_oom_delta": cumulative_deltas["events_oom"],
        "test_cgroup_oom_kill_delta": cumulative_deltas["events_oom_kill"],
        "max_low_memory_popup_count": max(values["low_memory_popup_count"], default=0.0),
    }


def suite_metrics(summary_path: Path) -> dict[str, Any]:
    summary = load_json(summary_path)
    results = [item for item in summary.get("results", []) if item.get("status") == VALID_STATUS]
    metrics = {
        "trace_page_fault_user": metric_stats(results, lambda item: item["trace"]["page_fault_user"]),
        "cgroup_pgfault": metric_stats(results, lambda item: item["cgroup"]["pgfault_delta"]),
        "cgroup_pgmajfault": metric_stats(results, lambda item: item["cgroup"]["pgmajfault_delta"]),
        "direct_reclaim_begin": metric_stats(results, lambda item: item["trace"]["direct_reclaim_begin"]),
        "kswapd_wake": metric_stats(results, lambda item: item["trace"]["kswapd_wake"]),
        "parp_decision": metric_stats(results, lambda item: item["trace"]["parp_decision"]),
        "parp_access": metric_stats(results, lambda item: item["trace"]["parp_access"]),
        "parp_outcome": metric_stats(results, lambda item: item["trace"]["parp_outcome"]),
        "launch_failures": metric_stats(results, lambda item: item["events"]["launch_failures"]),
        "low_memory_popups": metric_stats(results, lambda item: item["events"]["low_memory_popups"]),
        "app_oom_kills": metric_stats(results, lambda item: item["events"]["app_oom_kills"]),
        "failure_total": metric_stats(results, lambda item: item["events"]["failure_total"]),
        "trace_loss_total": metric_stats(results, lambda item: item["trace"]["loss_total"]),
    }
    suite_root = summary_path.parent
    preflight_path = suite_root / "round-01/preflight.json"
    preflight = load_json(preflight_path) if preflight_path.exists() else {}
    return {
        "summary_path": str(summary_path.resolve()),
        "kernel_release": summary.get("kernel_release", ""),
        "status": summary.get("status", ""),
        "rounds_requested": summary.get("rounds_requested", 0),
        "rounds_valid": len(results),
        "case_done_total": sum(int(item["automation"]["case_done"]) for item in results),
        "workload_contract": summary.get("workload_contract", {}),
        "metrics": metrics,
        "monitor_extrema": monitor_extrema(suite_root),
        "preflight": preflight,
    }


def gib(value: float) -> str:
    return f"{value / 1024**3:.3f} GiB"


def number(value: float) -> str:
    if float(value).is_integer():
        return str(int(value))
    return f"{value:.3f}"


def values_text(values: list[float]) -> str:
    return ", ".join(number(value) for value in values)


def metric_table(title: str, metrics: dict[str, dict[str, Any]], pagefault_label: str) -> list[str]:
    labels = {
        "trace_page_fault_user": pagefault_label,
        "cgroup_pgfault": "slice pgfault",
        "cgroup_pgmajfault": "slice pgmajfault",
        "direct_reclaim_begin": "direct reclaim begin",
        "kswapd_wake": "kswapd wake",
        "parp_decision": "PARP decision事件",
        "parp_access": "PARP access事件",
        "parp_outcome": "PARP outcome事件",
        "launch_failures": "启动/自动化失败",
        "low_memory_popups": "低内存弹窗",
        "app_oom_kills": "测试 cgroup OOM kill",
        "failure_total": "峰值异常总数",
        "trace_loss_total": "trace 丢失",
    }
    lines = [f"## {title}", "", "| 指标 | 均值 | 最小 | 最大 | 样本标准差 | 各轮原始值 |", "|---|---:|---:|---:|---:|---|"]
    for key, stats in metrics.items():
        lines.append(
            f"| {labels[key]} | `{number(stats['mean'])}` | `{number(stats['min'])}` | "
            f"`{number(stats['max'])}` | `{number(stats['sample_stdev'])}` | `{values_text(stats['round_values'])}` |"
        )
    lines.append("")
    return lines


def build_report(hotcold_path: Path, peak_path: Path) -> tuple[dict[str, Any], str]:
    hotcold = suite_metrics(hotcold_path)
    peak = suite_metrics(peak_path)
    if hotcold["kernel_release"] != peak["kernel_release"]:
        raise RuntimeError("hotcold 与 peak 不是同一个内核，不能合并为一份基线")
    hot_baseline = hotcold["metrics"]["trace_page_fault_user"]["mean"]
    peak_baseline = peak["metrics"]["failure_total"]["mean"]
    pagefault_target_20 = hot_baseline * 0.80
    pagefault_target_30 = hot_baseline * 0.70
    peak_target_30 = peak_baseline * 0.70 if peak_baseline > 0 else None
    preflight = hotcold.get("preflight", {})
    payload = {
        "report_type": "PARP_CURRENT_SYSTEM_BASELINE",
        "generated_at": dt.datetime.now().isoformat(),
        "kernel_release": hotcold["kernel_release"],
        "environment": {
            "memory_total_bytes": preflight.get("memory", {}).get("total_bytes", 0),
            "swap_bytes": preflight.get("swap_bytes", 0),
            "effective_tier_mode": preflight.get("parp", {}).get("effective_tier_mode", ""),
            "apply_compiled": preflight.get("parp", {}).get("apply_compiled", ""),
            "model_provenance": preflight.get("parp", {}).get("model_provenance", ""),
        },
        "official_acceptance_baseline": {
            "pagefault": {
                "baseline_mean": hot_baseline,
                "target_20_percent_max": pagefault_target_20,
                "challenge_30_percent_max": pagefault_target_30,
                "improvement_percent": None,
                "verdict": "BASELINE_ONLY_WAITING_FOR_APPLY_PAIR",
            },
            "peak_failure_total": {
                "baseline_mean": peak_baseline,
                "target_30_percent_max": peak_target_30,
                "improvement_percent": None,
                "verdict": "ZERO_BASELINE_REQUIRES_CALIBRATION" if peak_baseline == 0 else "BASELINE_ONLY_WAITING_FOR_APPLY_PAIR",
            },
        },
        "hotcold": hotcold,
        "peak": peak,
    }
    env = payload["environment"]
    peak_target_text = "N/A（基线为0，必须先校准出非零基线）" if peak_target_30 is None else number(peak_target_30)
    lines = [
        "# 当前系统 PARP / MGLRU 基线指标", "",
        f"- 生成时间：`{payload['generated_at']}`",
        f"- 内核：`{payload['kernel_release']}`",
        f"- 物理内存：`{gib(float(env['memory_total_bytes']))}`",
        f"- Swap：`{gib(float(env['swap_bytes']))}`",
        f"- effective-tier mode / apply_compiled：`{env['effective_tier_mode']}` / `{env['apply_compiled']}`",
        f"- 模型来源：`{env['model_provenance']}`", "",
        "## 最重要的验收基线", "",
        "| 正式指标 | 当前系统基线 | 20%目标上限 | 30%目标上限 | 当前结论 |",
        "|---|---:|---:|---:|---|",
        f"| 冷热 `page_fault_user` | `{number(hot_baseline)}` 次/轮 | `{number(pagefault_target_20)}` | `{number(pagefault_target_30)}` | 只有基线，等待Apply配对 |",
        f"| 峰值异常总数 | `{number(peak_baseline)}` 次/轮 | — | `{peak_target_text}` | 当前为0，不能计算改善率 |", "",
        "> 改进后必须复用相同内核源码基线、seed、场景和轮数。改善率 = (本报告基线均值 - Apply均值) / 本报告基线均值 × 100%。", "",
        f"- 冷热有效轮次/步骤：`{hotcold['rounds_valid']}/{hotcold['rounds_requested']}` / `{hotcold['case_done_total']}`",
        f"- 峰值有效轮次/步骤：`{peak['rounds_valid']}/{peak['rounds_requested']}` / `{peak['case_done_total']}`", "",
    ]
    lines.extend(metric_table("冷热实验：全部采集指标", hotcold["metrics"], "page_fault_user（正式冷热指标）"))
    lines.extend(metric_table("峰值实验：全部采集指标", peak["metrics"], "page_fault_user（辅助指标）"))
    lines += [
        "## 运行期间资源极值", "",
        "| 实验 | 最低MemAvailable | 最低SwapFree | 最高PSI some/full avg10 | 测试cgroup最高内存 |",
        "|---|---:|---:|---:|---:|",
        f"| 冷热 | `{gib(hotcold['monitor_extrema']['min_memavailable_bytes'])}` | `{gib(hotcold['monitor_extrema']['min_swapfree_bytes'])}` | `{hotcold['monitor_extrema']['max_psi_some_avg10']:.3f}` / `{hotcold['monitor_extrema']['max_psi_full_avg10']:.3f}` | `{gib(hotcold['monitor_extrema']['max_test_cgroup_memory_current_bytes'])}` |",
        f"| 峰值 | `{gib(peak['monitor_extrema']['min_memavailable_bytes'])}` | `{gib(peak['monitor_extrema']['min_swapfree_bytes'])}` | `{peak['monitor_extrema']['max_psi_some_avg10']:.3f}` / `{peak['monitor_extrema']['max_psi_full_avg10']:.3f}` | `{gib(peak['monitor_extrema']['max_test_cgroup_memory_current_bytes'])}` |", "",
        "## Swap、限流与OOM累计变化", "",
        "| 实验 | pswpin | pswpout | memory.high | memory.max | cgroup OOM/OOM kill | 宿主OOM kill | 弹窗最大数 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
        f"| 冷热 | `{number(hotcold['monitor_extrema']['pswpin_delta'])}` | `{number(hotcold['monitor_extrema']['pswpout_delta'])}` | `{number(hotcold['monitor_extrema']['test_cgroup_memory_high_delta'])}` | `{number(hotcold['monitor_extrema']['test_cgroup_memory_max_delta'])}` | `{number(hotcold['monitor_extrema']['test_cgroup_oom_delta'])}` / `{number(hotcold['monitor_extrema']['test_cgroup_oom_kill_delta'])}` | `{number(hotcold['monitor_extrema']['host_oom_kill_delta'])}` | `{number(hotcold['monitor_extrema']['max_low_memory_popup_count'])}` |",
        f"| 峰值 | `{number(peak['monitor_extrema']['pswpin_delta'])}` | `{number(peak['monitor_extrema']['pswpout_delta'])}` | `{number(peak['monitor_extrema']['test_cgroup_memory_high_delta'])}` | `{number(peak['monitor_extrema']['test_cgroup_memory_max_delta'])}` | `{number(peak['monitor_extrema']['test_cgroup_oom_delta'])}` / `{number(peak['monitor_extrema']['test_cgroup_oom_kill_delta'])}` | `{number(peak['monitor_extrema']['host_oom_kill_delta'])}` | `{number(peak['monitor_extrema']['max_low_memory_popup_count'])}` |", "",
        "## 判读说明", "",
        "- 冷热正式指标是受控 sidecar PID 的 `exceptions:page_fault_user`；slice `pgfault/pgmajfault` 是交叉复核值。",
        "- 峰值正式指标是启动/自动化失败、低内存弹窗和测试 cgroup OOM kill 的总和。",
        "- `trace_loss_total` 必须为0，否则相应轮次无效。",
        "- 当前为Shadow内核且 `apply_compiled=0`，只用于建立优化前基线，不能给出改善率。",
        "- 峰值异常基线为0时没有可用分母，需要安全增强峰值场景后重新建立该项基线。", "",
        "## 原始数据", "",
        f"- 冷热汇总：`{hotcold['summary_path']}`",
        f"- 峰值汇总：`{peak['summary_path']}`",
    ]
    return payload, "\n".join(lines) + "\n"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="生成清晰、可用于Apply配对比较的PARP基线指标报告")
    parser.add_argument("--hotcold", type=Path, required=True, help="hotcold summary.json")
    parser.add_argument("--peak", type=Path, required=True, help="peak summary.json")
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    payload, markdown = build_report(args.hotcold.resolve(), args.peak.resolve())
    args.output_dir.mkdir(parents=True, exist_ok=True)
    json_path = args.output_dir / "current-baseline-metrics-lzx.json"
    markdown_path = args.output_dir / "current-baseline-metrics-lzx.md"
    write_json(json_path, payload)
    markdown_path.write_text(markdown, encoding="utf-8")
    print(markdown_path)
    print(json_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
