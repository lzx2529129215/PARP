#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import datetime as dt
import importlib.util  #lzx
import json
import statistics
import sys  #lzx
from pathlib import Path
from typing import Any, Callable


VALID_STATUS = "VALID_DIAGNOSTIC"
RUNNER_SPEC = importlib.util.spec_from_file_location("parp_acceptance_metrics_lzx", Path(__file__).with_name("parp-acceptance-lzx.py"))  #lzx
assert RUNNER_SPEC is not None and RUNNER_SPEC.loader is not None  #lzx
RUNNER = importlib.util.module_from_spec(RUNNER_SPEC)  #lzx
sys.modules[RUNNER_SPEC.name] = RUNNER  #lzx
RUNNER_SPEC.loader.exec_module(RUNNER)  #lzx


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def metric_stats_values(values: list[float | None]) -> dict[str, Any]:  #lzx
    present = [float(value) for value in values if value is not None]  #lzx
    return {
        "available": bool(present),  #lzx
        "available_rounds": len(present),  #lzx
        "mean": statistics.mean(present) if present else None,  #lzx
        "min": min(present) if present else None,  #lzx
        "max": max(present) if present else None,  #lzx
        "sample_stdev": statistics.stdev(present) if len(present) > 1 else (0.0 if present else None),  #lzx
        "round_values": values,
    }


def metric_stats(results: list[dict[str, Any]], getter: Callable[[dict[str, Any]], float | None]) -> dict[str, Any]:
    values: list[float | None] = []  #lzx
    for item in results:  #lzx
        try:  #lzx
            value = getter(item)  #lzx
            values.append(float(value) if value is not None else None)  #lzx
        except (KeyError, TypeError, ValueError):  #lzx
            values.append(None)  #lzx
    return metric_stats_values(values)  #lzx


def monitor_round_values(suite_root: Path, field: str, round_indices: list[int] | None = None) -> list[float | None]:  #lzx
    values: list[float | None] = []  #lzx
    paths = (  #lzx
        [suite_root / f"round-{index + 1:02d}/monitor.csv" for index in round_indices]  #lzx
        if round_indices is not None  #lzx
        else sorted(suite_root.glob("round-*/monitor.csv"))  #lzx
    )  #lzx
    for path in paths:  #lzx
        if not path.exists():  #lzx
            values.append(None)  #lzx
            continue  #lzx
        with path.open(encoding="utf-8", newline="") as stream:  #lzx
            rows = list(csv.DictReader(stream))  #lzx
        if not rows or field not in rows[0] or field not in rows[-1]:  #lzx
            values.append(None)  #lzx
            continue  #lzx
        try:  #lzx
            values.append(float(rows[-1][field]) - float(rows[0][field]))  #lzx
        except (KeyError, TypeError, ValueError):  #lzx
            values.append(None)  #lzx
    return values  #lzx


def scaled(values: list[float | None], divisor: float) -> list[float | None]:  #lzx
    return [None if value is None else value / divisor for value in values]  #lzx


def monitor_extrema(suite_root: Path, round_indices: list[int] | None = None) -> dict[str, float]:  #lzx
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
    paths = (  #lzx
        [suite_root / f"round-{index + 1:02d}/monitor.csv" for index in round_indices]  #lzx
        if round_indices is not None  #lzx
        else sorted(suite_root.glob("round-*/monitor.csv"))  #lzx
    )  #lzx
    for path in paths:  #lzx
        if not path.exists():  #lzx
            continue  #lzx
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
    all_results = summary.get("results", [])  #lzx
    valid_indices = [index for index, item in enumerate(all_results) if item.get("status") == VALID_STATUS]  #lzx
    results = [all_results[index] for index in valid_indices]  #lzx
    suite_root = summary_path.parent  #lzx
    trace_rounds = [  #lzx
        RUNNER.count_trace_events(path) if path.exists() else {}  #lzx
        for index in valid_indices  #lzx
        for path in [suite_root / f"round-{index + 1:02d}/trace/trace.txt"]  #lzx
    ]  #lzx
    monitor = lambda field: monitor_round_values(suite_root, field, valid_indices)  #lzx
    refault_file = monitor("refault_file")  #lzx
    refault_anon = monitor("refault_anon")  #lzx
    pgscan = monitor("pgscan")  #lzx
    pgsteal = monitor("pgsteal")  #lzx
    scan_efficiency = [  #lzx
        (100.0 * steal / scan) if scan not in (None, 0) and steal is not None else None  #lzx
        for scan, steal in zip(pgscan, pgsteal)  #lzx
    ]  #lzx
    metrics = {
        "trace_page_fault_user": metric_stats(results, lambda item: item["trace"]["page_fault_user"]),
        "cgroup_pgfault": metric_stats(results, lambda item: item["cgroup"]["pgfault_delta"]),
        "cgroup_pgmajfault": metric_stats(results, lambda item: item["cgroup"]["pgmajfault_delta"]),
        "workingset_refault_file": metric_stats_values(refault_file),  #lzx
        "workingset_refault_anon": metric_stats_values(refault_anon),  #lzx
        "workingset_activate_file": metric_stats_values(monitor("activate_file")),  #lzx
        "workingset_activate_anon": metric_stats_values(monitor("activate_anon")),  #lzx
        "workingset_restore_file": metric_stats_values(monitor("restore_file")),  #lzx
        "workingset_restore_anon": metric_stats_values(monitor("restore_anon")),  #lzx
        "pgscan": metric_stats_values(pgscan), "pgsteal": metric_stats_values(pgsteal),  #lzx
        "scan_efficiency_percent": metric_stats_values(scan_efficiency),  #lzx
        "page_refault_ratio_percent": metric_stats(results, lambda item: item["cgroup"].get("page_refault_ratio_percent")),  #lzx
        "direct_reclaim_scan_ratio_percent": metric_stats(results, lambda item: item["cgroup"].get("direct_reclaim_scan_ratio_percent")),  #lzx
        "pgscan_direct": metric_stats_values(monitor("pgscan_direct")),  #lzx
        "pgsteal_direct": metric_stats_values(monitor("pgsteal_direct")),  #lzx
        "pgscan_kswapd": metric_stats_values(monitor("pgscan_kswapd")),  #lzx
        "pgsteal_kswapd": metric_stats_values(monitor("pgsteal_kswapd")),  #lzx
        "direct_reclaim_begin": metric_stats(results, lambda item: item["trace"]["direct_reclaim_begin"]),
        "direct_reclaim_latency_p95_ms": metric_stats_values(scaled([item.get("direct_reclaim_latency_ns_p95") for item in trace_rounds], 1_000_000)),  #lzx
        "direct_reclaim_time_total_ms": metric_stats_values(scaled([item.get("direct_reclaim_time_ns_total") for item in trace_rounds], 1_000_000)),  #lzx
        "direct_reclaim_pages_reclaimed": metric_stats_values([item.get("direct_reclaim_pages_reclaimed") for item in trace_rounds]),  #lzx
        "memcg_reclaim_latency_p95_ms": metric_stats_values(scaled([item.get("memcg_reclaim_latency_ns_p95") for item in trace_rounds], 1_000_000)),  #lzx
        "kswapd_wake": metric_stats(results, lambda item: item["trace"]["kswapd_wake"]),
        "kswapd_active_time_total_ms": metric_stats_values(scaled([item.get("kswapd_active_time_ns_total") for item in trace_rounds], 1_000_000)),  #lzx
        "kswapd_cpu_time_ms": metric_stats_values(scaled(monitor("kswapd_cpu_time_ns"), 1_000_000)),  #lzx
        "cgroup_cpu_usage_ms": metric_stats(results, lambda item: item["cgroup"].get("cpu_usage_usec_delta") / 1000 if item["cgroup"].get("cpu_usage_usec_delta") is not None else None),  #lzx
        "cgroup_cpu_one_core_percent": metric_stats(results, lambda item: item["cgroup"].get("cpu_one_core_percent")),  #lzx
        "cgroup_cpu_machine_percent": metric_stats(results, lambda item: item["cgroup"].get("cpu_machine_percent")),  #lzx
        "cgroup_io_read_mib": metric_stats(results, lambda item: item["cgroup"].get("io_read_bytes_delta") / 1024**2 if item["cgroup"].get("io_read_bytes_delta") is not None else None),  #lzx
        "cgroup_io_write_mib": metric_stats(results, lambda item: item["cgroup"].get("io_write_bytes_delta") / 1024**2 if item["cgroup"].get("io_write_bytes_delta") is not None else None),  #lzx
        "cgroup_io_read_mib_per_second": metric_stats(results, lambda item: item["cgroup"].get("io_read_mib_per_second")),  #lzx
        "cgroup_io_write_mib_per_second": metric_stats(results, lambda item: item["cgroup"].get("io_write_mib_per_second")),  #lzx
        "launch_ready_latency_mean_ms": metric_stats(results, lambda item: item.get("launch", {}).get("mean_ms")),  #lzx
        "launch_ready_latency_p95_ms": metric_stats(results, lambda item: item.get("launch", {}).get("p95_ms")),  #lzx
        "parp_decision": metric_stats(results, lambda item: item["trace"]["parp_decision"]),
        "parp_access": metric_stats(results, lambda item: item["trace"]["parp_access"]),
        "parp_outcome": metric_stats(results, lambda item: item["trace"]["parp_outcome"]),
        "launch_failures": metric_stats(results, lambda item: item["events"]["launch_failures"]),
        "low_memory_popups": metric_stats(results, lambda item: item["events"]["low_memory_popups"]),
        "cgroup_oom_events": metric_stats_values(monitor("events_oom")),  #lzx
        "app_oom_kills": metric_stats(results, lambda item: item["events"]["app_oom_kills"]),
        "host_oom_kills": metric_stats_values(monitor("vm_oom_kill")),  #lzx
        "failure_total": metric_stats(results, lambda item: item["events"]["failure_total"]),
        "trace_loss_total": metric_stats(results, lambda item: item["trace"]["loss_total"]),
    }
    preflight_path = suite_root / "round-01/preflight.json"
    preflight = load_json(preflight_path) if preflight_path.exists() else {}
    if int(preflight.get("metrics_schema_version", 1)) < 2:  #lzx
        metrics["memcg_reclaim_latency_p95_ms"] = metric_stats_values([None] * len(results))  #lzx
    if int(preflight.get("metrics_schema_version", 1)) < 3:  #lzx
        for name in (  #lzx
            "page_refault_ratio_percent", "direct_reclaim_scan_ratio_percent",  #lzx
            "cgroup_cpu_usage_ms", "cgroup_cpu_one_core_percent", "cgroup_cpu_machine_percent",  #lzx
            "cgroup_io_read_mib", "cgroup_io_write_mib", "cgroup_io_read_mib_per_second", "cgroup_io_write_mib_per_second",  #lzx
            "launch_ready_latency_mean_ms", "launch_ready_latency_p95_ms",  #lzx
        ):  #lzx
            metrics[name] = metric_stats_values([None] * len(results))  #lzx
    return {
        "summary_path": str(summary_path.resolve()),
        "kernel_release": summary.get("kernel_release", ""),
        "status": summary.get("status", ""),
        "rounds_requested": summary.get("rounds_requested", 0),
        "rounds_valid": len(results),
        "case_done_total": sum(int(item["automation"]["case_done"]) for item in results),
        "workload_contract": summary.get("workload_contract", {}),
        "metrics": metrics,
        "monitor_extrema": monitor_extrema(suite_root, valid_indices),  #lzx
        "preflight": preflight,
    }


def gib(value: float) -> str:
    return f"{value / 1024**3:.3f} GiB"


def number(value: float | None) -> str:  #lzx
    if value is None:  #lzx
        return "N/A"  #lzx
    if float(value).is_integer():
        return str(int(value))
    return f"{value:.3f}"


def values_text(values: list[float | None]) -> str:  #lzx
    return ", ".join(number(value) for value in values)


def metric_table(title: str, metrics: dict[str, dict[str, Any]], pagefault_label: str) -> list[str]:
    labels = {
        "trace_page_fault_user": pagefault_label,
        "cgroup_pgfault": "slice pgfault",
        "cgroup_pgmajfault": "slice pgmajfault",
        "workingset_refault_file": "workingset_refault_file（真实文件页refault）",  #lzx
        "workingset_refault_anon": "workingset_refault_anon（真实匿名页refault）",  #lzx
        "workingset_activate_file": "workingset_activate_file",  #lzx
        "workingset_activate_anon": "workingset_activate_anon",  #lzx
        "workingset_restore_file": "workingset_restore_file",  #lzx
        "workingset_restore_anon": "workingset_restore_anon",  #lzx
        "pgscan": "pgscan", "pgsteal": "pgsteal",  #lzx
        "scan_efficiency_percent": "扫描效率 pgsteal/pgscan（%）",  #lzx
        "page_refault_ratio_percent": "refault/pgsteal（%）",  #lzx
        "direct_reclaim_scan_ratio_percent": "direct扫描占比（%）",  #lzx
        "pgscan_direct": "pgscan_direct", "pgsteal_direct": "pgsteal_direct",  #lzx
        "pgscan_kswapd": "pgscan_kswapd", "pgsteal_kswapd": "pgsteal_kswapd",  #lzx
        "direct_reclaim_begin": "direct reclaim begin",
        "direct_reclaim_latency_p95_ms": "direct reclaim P95延迟（ms）",  #lzx
        "direct_reclaim_time_total_ms": "direct reclaim总墙钟时间（ms）",  #lzx
        "direct_reclaim_pages_reclaimed": "direct reclaim回收页数",  #lzx
        "memcg_reclaim_latency_p95_ms": "memcg reclaim P95延迟（ms）",  #lzx
        "kswapd_wake": "kswapd wake",
        "kswapd_active_time_total_ms": "kswapd活跃墙钟时间（ms）",  #lzx
        "kswapd_cpu_time_ms": "kswapd CPU时间（ms）",  #lzx
        "cgroup_cpu_usage_ms": "测试slice CPU时间（ms）",  #lzx
        "cgroup_cpu_one_core_percent": "测试slice CPU单核等价（%）",  #lzx
        "cgroup_cpu_machine_percent": "测试slice CPU整机占比（%）",  #lzx
        "cgroup_io_read_mib": "测试slice块层读取（MiB）",  #lzx
        "cgroup_io_write_mib": "测试slice块层写入（MiB）",  #lzx
        "cgroup_io_read_mib_per_second": "测试slice块层读取吞吐（MiB/s）",  #lzx
        "cgroup_io_write_mib_per_second": "测试slice块层写入吞吐（MiB/s）",  #lzx
        "launch_ready_latency_mean_ms": "应用启动到窗口就绪均值（ms）",  #lzx
        "launch_ready_latency_p95_ms": "应用启动到窗口就绪P95（ms）",  #lzx
        "parp_decision": "PARP decision事件",
        "parp_access": "PARP access事件",
        "parp_outcome": "PARP outcome事件",
        "launch_failures": "启动/自动化失败",
        "low_memory_popups": "低内存弹窗",
        "cgroup_oom_events": "测试 cgroup OOM事件",  #lzx
        "app_oom_kills": "测试 cgroup OOM kill",
        "host_oom_kills": "宿主 OOM kill",  #lzx
        "failure_total": "峰值异常总数",
        "trace_loss_total": "trace 丢失",
    }
    purposes = {  #lzx
        "trace_page_fault_user": "反映受控应用访问时需要重新建立页映射的次数，是冷热切换的正式指标。",  #lzx
        "cgroup_pgfault": "反映整个测试slice的总缺页活动，用于交叉复核GUI应用与sidecar的整体内存访问压力。",  #lzx
        "cgroup_pgmajfault": "反映需要磁盘或swap I/O才能解决的重大缺页，与可感知卡顿风险直接相关。",  #lzx
        "workingset_refault_file": "表示已回收文件页又被访问的次数，用于识别文件页误回收和工作集抖动。",  #lzx
        "workingset_refault_anon": "表示已回收匿名页又被访问的次数，用于识别swap往返和匿名工作集抖动。",  #lzx
        "workingset_activate_file": "表示因重用而进入活跃工作集的文件页，辅助判断文件热页识别是否及时。",  #lzx
        "workingset_activate_anon": "表示因重用而进入活跃工作集的匿名页，辅助判断匿名热页识别是否及时。",  #lzx
        "workingset_restore_file": "反映文件页被恢复回工作集的数量，用于观察文件工作集恢复压力。",  #lzx
        "workingset_restore_anon": "反映匿名页被恢复回工作集的数量，用于观察匿名工作集恢复压力。",  #lzx
        "pgscan": "内核回收路径扫描的总页数，反映为找到可回收页付出的搜索成本。",  #lzx
        "pgsteal": "实际成功回收的页数，反映回收产出。",  #lzx
        "scan_efficiency_percent": "pgsteal/pgscan，表示每扫描100页能成功回收多少；需结合refault判断，不是越高越好。",  #lzx
        "page_refault_ratio_percent": "真实文件页与匿名页refault之和除以pgsteal，用于衡量每回收100页带来的短期误回收代价。",  #lzx
        "direct_reclaim_scan_ratio_percent": "direct扫描页占direct与kswapd扫描总量的比例，越高通常表示越多回收成本落在前台任务。",  #lzx
        "pgscan_direct": "由申请内存的前台任务同步触发的扫描页数，反映应用被迫参与回收的压力。",  #lzx
        "pgsteal_direct": "direct reclaim实际回收的页数，用于对照同步扫描成本和产出。",  #lzx
        "pgscan_kswapd": "kswapd后台回收扫描页数，反映系统提前处理内存压力的工作量。",  #lzx
        "pgsteal_kswapd": "kswapd后台实际回收页数，用于评估后台回收的有效产出。",  #lzx
        "direct_reclaim_begin": "前台任务进入同步回收的次数；次数过多通常意味着更频繁的应用停顿。",  #lzx
        "direct_reclaim_latency_p95_ms": "95%的direct reclaim停顿不超过该值，用于衡量前台同步回收的尾延迟。",  #lzx
        "direct_reclaim_time_total_ms": "所有direct reclaim区间累计的墙钟时间，反映前台任务同步等待回收的总负担。",  #lzx
        "direct_reclaim_pages_reclaimed": "direct reclaim期间实际回收页数，用于结合时间判断同步回收效率。",  #lzx
        "memcg_reclaim_latency_p95_ms": "测试cgroup回收的P95墙钟延迟，反映容器/slice内存限制造成的尾部停顿。",  #lzx
        "kswapd_wake": "kswapd被唤醒的次数，反映系统进入后台回收状态的频率。",  #lzx
        "kswapd_active_time_total_ms": "kswapd从唤醒到休眠的累计墙钟时间，反映后台回收持续时长。",  #lzx
        "kswapd_cpu_time_ms": "kswapd线程实际消耗的CPU时间，反映后台回收的处理器开销。",  #lzx
        "cgroup_cpu_usage_ms": "测试slice内全部受控进程累计CPU时间，用于比较策略是否增加总体处理器开销。",  #lzx
        "cgroup_cpu_one_core_percent": "CPU时间除以轮次墙钟时间，100%代表持续占满一个逻辑CPU，可超过100%。",  #lzx
        "cgroup_cpu_machine_percent": "单核等价CPU占比再除以逻辑CPU数，表示测试负载占整机总CPU容量的比例。",  #lzx
        "cgroup_io_read_mib": "cgroup块层实际读字节，不包含页缓存命中，用于识别swap/文件回读放大。",  #lzx
        "cgroup_io_write_mib": "cgroup块层实际写字节，用于识别swap写出和文件写入开销。",  #lzx
        "cgroup_io_read_mib_per_second": "块层读取量除以轮次时间，反映持续读取压力；缓存命中不会计入。",  #lzx
        "cgroup_io_write_mib_per_second": "块层写入量除以轮次时间，反映持续写盘压力。",  #lzx
        "launch_ready_latency_mean_ms": "从GUI启动动作开始到对应X11窗口验证成功的平均代理延迟；不是首个可交互帧。",  #lzx
        "launch_ready_latency_p95_ms": "同一轮各应用X11窗口验证代理延迟的P95；峰值并发场景中逐个验证会使其成为上界。",  #lzx
        "parp_decision": "PARP做出页层级决策的trace事件数，用于确认策略是否实际进入决策路径。",  #lzx
        "parp_access": "PARP记录的页访问事件数，用于检查特征/访问观测链路是否生效。",  #lzx
        "parp_outcome": "PARP决策后结果事件数，用于检查决策与后续页行为的关联覆盖。",  #lzx
        "launch_failures": "应用启动、窗口识别或自动化动作失败次数，反映用户任务可用性。",  #lzx
        "low_memory_popups": "低内存告警窗口出现次数，反映用户可见的内存压力异常。",  #lzx
        "cgroup_oom_events": "测试cgroup进入OOM处理的次数，可能未杀进程，用于识别已触及内存上限。",  #lzx
        "app_oom_kills": "测试cgroup中进程真正被OOM killer终止的次数，是峰值异常正式组成项。",  #lzx
        "host_oom_kills": "宿主系统OOM kill次数；非零表示安全边界被突破，该轮不应用于验收。",  #lzx
        "failure_total": "启动/自动化失败+低内存弹窗+cgroup OOM kill，是峰值场景的正式异常总指标。",  #lzx
        "trace_loss_total": "trace overrun、commit overrun和dropped events之和；非零表示采集不完整，该轮无效。",  #lzx
    }  #lzx
    lines = [f"## {title}", "", "| 指标 | 指标作用（能说明什么） | 均值 | 最小 | 最大 | 样本标准差 | 各轮原始值 |", "|---|---|---:|---:|---:|---:|---|"]  #lzx
    for key, stats in metrics.items():
        lines.append(
            f"| {labels[key]} | {purposes[key]} | `{number(stats['mean'])}` | `{number(stats['min'])}` | "  #lzx
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
    hot_refault_file = hotcold["metrics"]["workingset_refault_file"]["mean"]  #lzx
    hot_refault_anon = hotcold["metrics"]["workingset_refault_anon"]["mean"]  #lzx
    peak_refault_file = peak["metrics"]["workingset_refault_file"]["mean"]  #lzx
    peak_refault_anon = peak["metrics"]["workingset_refault_anon"]["mean"]  #lzx
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
            "system_metadata": preflight.get("system_metadata", {}),  #lzx
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
        "diagnostic_safety_baseline": {  #lzx
            "hotcold_workingset_refault_file_mean": hot_refault_file,  #lzx
            "hotcold_workingset_refault_anon_mean": hot_refault_anon,  #lzx
            "peak_workingset_refault_file_mean": peak_refault_file,  #lzx
            "peak_workingset_refault_anon_mean": peak_refault_anon,  #lzx
            "comparison_rule": "Apply不得显著增加真实workingset refault；必须在相同seed/场景下配对比较。",  #lzx
        },  #lzx
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
        f"- 模型来源：`{env['model_provenance']}`",
        f"- 指标schema版本：`{preflight.get('metrics_schema_version', 1)}`", "",  #lzx
        "## 可复现系统元数据", "",  #lzx
        f"- kernel config SHA-256：`{env['system_metadata'].get('kernel_config', {}).get('sha256') or 'N/A'}`",  #lzx
        f"- CPU：`{env['system_metadata'].get('cpu_model') or 'N/A'}`，逻辑CPU：`{env['system_metadata'].get('cpu_count', 'N/A')}`",  #lzx
        f"- CPU governor：`{', '.join(env['system_metadata'].get('cpu_governors', [])) or 'N/A'}`",  #lzx
        f"- THP enabled / defrag：`{env['system_metadata'].get('transparent_hugepage', {}).get('enabled') or 'N/A'}` / `{env['system_metadata'].get('transparent_hugepage', {}).get('defrag') or 'N/A'}`",  #lzx
        f"- vm sysctl：`{json.dumps(env['system_metadata'].get('vm_sysctls', {}), ensure_ascii=False, sort_keys=True)}`",  #lzx
        f"- 结果文件系统：`{json.dumps(env['system_metadata'].get('result_storage'), ensure_ascii=False, sort_keys=True)}`", "",  #lzx
        "## 最重要的验收基线", "",
        "| 正式指标 | 当前系统基线 | 20%目标上限 | 30%目标上限 | 当前结论 |",
        "|---|---:|---:|---:|---|",
        f"| 冷热 `page_fault_user` | `{number(hot_baseline)}` 次/轮 | `{number(pagefault_target_20)}` | `{number(pagefault_target_30)}` | 只有基线，等待Apply配对 |",
        f"| 峰值异常总数 | `{number(peak_baseline)}` 次/轮 | — | `{peak_target_text}` | 当前为0，不能计算改善率 |", "",
        "> 改进后必须复用相同内核源码基线、seed、场景和轮数。改善率 = (本报告基线均值 - Apply均值) / 本报告基线均值 × 100%。", "",
        f"- 冷热有效轮次/步骤：`{hotcold['rounds_valid']}/{hotcold['rounds_requested']}` / `{hotcold['case_done_total']}`",
        f"- 峰值有效轮次/步骤：`{peak['rounds_valid']}/{peak['rounds_requested']}` / `{peak['case_done_total']}`", "",
        "## 真实refault与OOM基线", "",  #lzx
        "| 场景 | workingset_refault_file | workingset_refault_anon | cgroup OOM | cgroup OOM kill | 宿主OOM kill |",  #lzx
        "|---|---:|---:|---:|---:|---:|",  #lzx
        f"| 冷热 | `{number(hot_refault_file)}` | `{number(hot_refault_anon)}` | `{number(hotcold['metrics']['cgroup_oom_events']['mean'])}` | `{number(hotcold['metrics']['app_oom_kills']['mean'])}` | `{number(hotcold['metrics']['host_oom_kills']['mean'])}` |",  #lzx
        f"| 峰值 | `{number(peak_refault_file)}` | `{number(peak_refault_anon)}` | `{number(peak['metrics']['cgroup_oom_events']['mean'])}` | `{number(peak['metrics']['app_oom_kills']['mean'])}` | `{number(peak['metrics']['host_oom_kills']['mean'])}` |", "",  #lzx
        "> refault是已被真实回收的页再次进入工作集，不等同于未来访问标签。Apply相对OFF不得显著增加该值；OOM/OOM kill必须分别报告。", "",  #lzx
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
        "- schema-v3 要求 cgroup 路径、device/inode、memory/cpu/io 读取端点一致，trace begin/end无丢配；任一失败则该轮无效。",  #lzx
        "- 指标表若出现 `N/A`，表示该轮未采集字段，不能解释成0；旧 schema-v2 基线中新增CPU/I/O/启动延迟将显示 `N/A`。",  #lzx
        "- 应用启动延迟是启动动作到X11窗口验证成功的代理值，不是首个可交互帧；并发峰值场景中应视为上界。",  #lzx
        "- direct/memcg reclaim trace统计的是同步回收墙钟延迟；kswapd CPU时间来自 `/proc/<kswapd>/stat`，两者不能混用。",  #lzx
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
