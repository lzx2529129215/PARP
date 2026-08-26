from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path
from typing import Any


def _events(path: Path) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            values.append(value)
    return values


def summarize_fleet(run: Path) -> dict[str, Any]:
    capacity = _events(run / "caching-capacity.jsonl")
    launched = max((int(row.get("launched", 0)) for row in capacity), default=0)
    alive = int(capacity[-1].get("alive", 0)) if capacity else 0
    max_cached = 0
    for row in capacity:
        if row.get("phase") == "final":
            continue
        if int(row.get("alive", 0)) != int(row.get("launched", 0)) or int(row.get("new_app_ready", 0)) != 1:
            break
        max_cached = int(row.get("launched", 0))
    hot_events: list[dict[str, Any]] = []
    heap_by_app: dict[int, int] = {}
    for path in sorted(run.glob("app-*.jsonl")):
        app_hot_events = [event for event in _events(path) if event.get("event") == "hot_launch"]
        hot_events.extend(app_hot_events)
        try:
            app_index = int(path.stem.split("-", 1)[1])
        except (IndexError, ValueError):
            continue
        heap_samples = [int(event["java_heap_used_bytes"]) for event in app_hot_events if "java_heap_used_bytes" in event]
        if heap_samples:
            heap_by_app[app_index] = heap_samples[-1]
    latencies = [float(event["latency_ms"]) for event in hot_events if "latency_ms" in event]
    reaccess = [float(event["object_reaccess_ratio"]) for event in hot_events if "object_reaccess_ratio" in event]
    heap = [int(event["java_heap_used_bytes"]) for event in hot_events if "java_heap_used_bytes" in event]
    rss_events = _events(run / "app-rss.jsonl")
    rss_by_app = {
        int(event["app_index"]): int(event["rss_bytes"])
        for event in rss_events
        if "app_index" in event and int(event.get("rss_bytes", 0)) > 0
    }
    matched_apps = sorted(set(heap_by_app) & set(rss_by_app))
    total_heap = sum(heap_by_app[index] for index in matched_apps)
    total_rss = sum(rss_by_app[index] for index in matched_apps)
    per_app_heap_ratios = [heap_by_app[index] / rss_by_app[index] for index in matched_apps]
    return {
        "workload_type": "fleet-managed-object-proxy",
        "background_apps_launched": launched,
        "background_apps_alive": alive,
        "background_app_survival_ratio": alive / launched if launched else None,
        "maximum_cached_apps_without_loss": max_cached,
        "hot_launch_samples": len(latencies),
        "hot_launch_latency_ms_mean": statistics.fmean(latencies) if latencies else None,
        "object_reaccess_ratio_mean": statistics.fmean(reaccess) if reaccess else None,
        "java_heap_used_bytes_mean": statistics.fmean(heap) if heap else None,
        "java_heap_ratio": total_heap / total_rss if total_rss else None,
        "java_heap_ratio_per_app_mean": statistics.fmean(per_app_heap_ratios) if per_app_heap_ratios else None,
        "gc_working_set_objects": None,
        "limitations": [
            "Java heap ratio matches each app's last post-hot heap sample to its post-hot RSS snapshot.",
            "GC working-set size requires a JVM/ART runtime probe and is not inferred from page faults.",
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--type", choices=("fleet",), required=True)
    parser.add_argument("--run", required=True)
    parser.add_argument("--output")
    args = parser.parse_args(argv)
    run = Path(args.run)
    value = summarize_fleet(run)
    output = Path(args.output) if args.output else run / "workload-summary.json"
    output.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
