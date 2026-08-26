from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any

from .schema import pair_key, validate_manifest


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def _get(value: dict[str, Any], dotted: str) -> Any:
    current: Any = value
    for key in dotted.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _artifact(run: Path, name: str) -> Path:
    local = run / name
    return local if local.exists() else run.parent / name


def _load_last_jsonl(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    for line in reversed(path.read_text(encoding="utf-8", errors="replace").splitlines()):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return {}


FIELDS = [
    "run",
    "name",
    "variant",
    "scenario",
    "seed",
    "repetition",
    "cache_state",
    "environment_hash",
    "workload_hash",
    "pair_id",
    "measurement_valid",
    "invalid_reasons",
    "elapsed_s",
    "snapshot_elapsed_s",
    "pre_start_boundary_ms",
    "post_stop_boundary_ms",
    "launch_latency_ms",
    "launch_measurement_source",
    "page_refault_count",
    "evicted_pages",
    "page_refault_ratio",
    "foreground_page_refault_count",
    "foreground_page_refault_ratio",
    "direct_reclaim_allocstall_count",
    "direct_reclaim_tracepoint_count",
    "direct_reclaim_total_duration_ms",
    "direct_reclaim_boundary_spanning_count",
    "direct_reclaim_event_ratio",
    "direct_reclaim_scanned_pages",
    "kswapd_scanned_pages",
    "direct_reclaim_page_ratio",
    "io_read_throughput_mb_s",
    "cpu_one_core_percent",
    "cpu_machine_percent",
    "oom_kill_count",
    "low_memory_killer_count",
    "average_fps",
    "fps_per_second_stddev",
    "jank_ratio",
    "gb_cold_launch_latency_ms",
    "cold_launch_io_throughput_mb_s",
    "cold_restart_count",
    "background_apps_alive",
    "background_app_survival_ratio",
    "hot_launch_latency_ms_mean",
    "maximum_cached_apps_without_loss",
    "gc_working_set_objects",
    "java_heap_ratio",
    "object_reaccess_ratio_mean",
]


def row_for_run(run: Path) -> dict[str, Any]:
    summary = _load(run / "summary.json")
    metadata = _load(run / "metadata.json")
    system_child = _load(run / "system" / "summary.json")
    if system_child:
        summary = system_child
        metadata = _load(run / "system" / "metadata.json")
    for cgroup_directory in ("cgroup", "foreground-cgroup"):
        cgroup_child = _load(run / cgroup_directory / "summary.json")
        if cgroup_child.get("cgroup") is not None:
            summary = {**summary, "cgroup": cgroup_child["cgroup"]}
            break
    launch = _load(_artifact(run, "launch.json"))
    bpf = _load(_artifact(run, "reclaim-events-summary.json"))
    frames = _load(_artifact(run, "frames-summary.json"))
    workload = _load(_artifact(run, "workload-summary.json"))
    cache_eviction = _load(_artifact(run, "cache-eviction.json"))
    cold_launch = _load_last_jsonl(_artifact(run, "cold-launch.jsonl"))
    system_scope = summary.get("system") or {}
    cgroup_scope = summary.get("cgroup") or {}
    manifest = _load(_artifact(run, "manifest.json")) or metadata.get("manifest") or {}
    invalid_reasons: list[str] = []
    if metadata.get("valid") is False:
        invalid_reasons.append(str(metadata.get("invalid_reason", "metadata marked the run invalid")))
    if summary.get("cgroup") and summary["cgroup"].get("valid") is False:
        invalid_reasons.extend(summary["cgroup"].get("invalid_reasons", []))
    if bpf and bpf.get("valid") is False:
        invalid_reasons.extend(bpf.get("invalid_reasons", []))
    if launch and launch.get("timed_out"):
        invalid_reasons.append("launch readiness timed out")
    for error_name in ("cgroup.error", "foreground-cgroup.error"):
        error_path = _artifact(run, error_name)
        if error_path.exists():
            invalid_reasons.append(error_path.read_text(encoding="utf-8", errors="replace").strip() or error_name)
    if metadata.get("scenario") == "appflow":
        if not cache_eviction:
            invalid_reasons.append("AppFlow cold-cache evidence is missing")
        elif cache_eviction.get("valid") is not True:
            invalid_reasons.append(str(cache_eviction.get("reason", "AppFlow cold-cache verification failed")))
    if manifest:
        invalid_reasons.extend(validate_manifest(manifest))
    direct_count = bpf.get("direct_reclaim_count")
    kswapd_wakes = bpf.get("kswapd_wake_count")
    direct_event_ratio = None
    if isinstance(direct_count, (int, float)) and isinstance(kswapd_wakes, (int, float)):
        denominator = direct_count + kswapd_wakes
        direct_event_ratio = direct_count / denominator if denominator > 0 else None
    pair_id = json.dumps(pair_key(manifest), ensure_ascii=False) if manifest else None
    return {
        "run": str(run),
        "name": metadata.get("name", run.name),
        "variant": manifest.get("variant"),
        "scenario": manifest.get("scenario", metadata.get("scenario")),
        "seed": manifest.get("seed"),
        "repetition": manifest.get("repetition"),
        "cache_state": manifest.get("cache_state", metadata.get("cache_state")),
        "environment_hash": manifest.get("environment_hash"),
        "workload_hash": manifest.get("workload_hash"),
        "pair_id": pair_id,
        "measurement_valid": not invalid_reasons,
        "invalid_reasons": json.dumps(invalid_reasons, ensure_ascii=False),
        "elapsed_s": summary.get("elapsed_s"),
        "snapshot_elapsed_s": summary.get("snapshot_elapsed_s"),
        "pre_start_boundary_ms": _get(summary, "measurement_window.pre_start_boundary_ms"),
        "post_stop_boundary_ms": _get(summary, "measurement_window.post_stop_boundary_ms"),
        "launch_latency_ms": launch.get("launch_latency_ms"),
        "launch_measurement_source": launch.get("measurement_source"),
        "page_refault_count": system_scope.get("page_refault_count"),
        "evicted_pages": system_scope.get("evicted_pages"),
        "page_refault_ratio": system_scope.get("page_refault_ratio"),
        "foreground_page_refault_count": cgroup_scope.get("page_refault_count") if cgroup_scope.get("valid", True) else None,
        "foreground_page_refault_ratio": cgroup_scope.get("page_refault_ratio") if cgroup_scope.get("valid", True) else None,
        "direct_reclaim_allocstall_count": system_scope.get("direct_reclaim_allocstall_count"),
        "direct_reclaim_tracepoint_count": bpf.get("direct_reclaim_count"),
        "direct_reclaim_total_duration_ms": bpf.get("direct_reclaim_total_duration_ms"),
        "direct_reclaim_boundary_spanning_count": bpf.get("direct_reclaim_boundary_spanning_count"),
        "direct_reclaim_event_ratio": direct_event_ratio,
        "direct_reclaim_scanned_pages": system_scope.get("direct_reclaim_scanned_pages"),
        "kswapd_scanned_pages": system_scope.get("kswapd_scanned_pages"),
        "direct_reclaim_page_ratio": system_scope.get("direct_reclaim_page_ratio"),
        "io_read_throughput_mb_s": cgroup_scope.get("io_read_throughput_mb_s") if cgroup_scope.get("valid", True) else None,
        "cpu_one_core_percent": cgroup_scope.get("cpu_one_core_percent", system_scope.get("cpu_one_core_percent")) if cgroup_scope.get("valid", True) else system_scope.get("cpu_one_core_percent"),
        "cpu_machine_percent": cgroup_scope.get("cpu_machine_percent", system_scope.get("cpu_machine_percent")) if cgroup_scope.get("valid", True) else system_scope.get("cpu_machine_percent"),
        "oom_kill_count": cgroup_scope.get("oom_kill_count") if cgroup_scope.get("valid", True) else None,
        "low_memory_killer_count": workload.get("low_memory_killer_count"),
        "average_fps": frames.get("average_fps"),
        "fps_per_second_stddev": frames.get("fps_per_second_stddev"),
        "jank_ratio": frames.get("jank_ratio"),
        "gb_cold_launch_latency_ms": cold_launch.get("elapsed_ms"),
        "cold_launch_io_throughput_mb_s": cold_launch.get("throughput_mb_s"),
        "cold_restart_count": workload.get("cold_restart_count"),
        "background_apps_alive": workload.get("background_apps_alive"),
        "background_app_survival_ratio": workload.get("background_app_survival_ratio"),
        "hot_launch_latency_ms_mean": workload.get("hot_launch_latency_ms_mean"),
        "maximum_cached_apps_without_loss": workload.get("maximum_cached_apps_without_loss"),
        "gc_working_set_objects": workload.get("gc_working_set_objects"),
        "java_heap_ratio": workload.get("java_heap_ratio"),
        "object_reaccess_ratio_mean": workload.get("object_reaccess_ratio_mean"),
    }


def discover_runs(root: Path) -> list[Path]:
    logical_runs: set[Path] = set()
    for path in root.rglob("summary.json"):
        directory = path.parent
        if directory.name in {"system", "cgroup", "foreground-cgroup"}:
            parent = directory.parent
            if (parent / "system" / "summary.json").exists() or (parent / "summary.json").exists():
                logical_runs.add(parent)
                continue
        if (directory / "metadata.json").exists():
            logical_runs.add(directory)
    return sorted(logical_runs)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Aggregate experiment runs into an auditable CSV")
    parser.add_argument("--root", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    runs = discover_runs(Path(args.root))
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(row_for_run(run) for run in runs)
    print(f"wrote {len(runs)} runs to {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
