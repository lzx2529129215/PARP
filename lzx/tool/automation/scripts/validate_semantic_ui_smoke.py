#!/usr/bin/env python3
"""验证编译后的本地 UI Smoke 场景和 dry-run trace，不执行 UI。"""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from pathlib import Path
from urllib.parse import urlparse


def main() -> int:
    parser = argparse.ArgumentParser(description="语义 UI Smoke 静态验证")
    parser.add_argument("--compiled", required=True, type=Path)
    parser.add_argument("--trace", required=True, type=Path)
    parser.add_argument("--work-dir", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    compiled = json.loads(args.compiled.read_text(encoding="utf-8"))
    actions = compiled.get("actions", [])
    action_ids = [str(item.get("action_id", "")) for item in actions if item.get("action_id")]
    semantic = [item for item in actions if item.get("op_type") == "semantic_operation"]
    starts = Counter(item.get("operation_id", "") for item in semantic if item.get("event_type") == "OP_START")
    terminals = Counter(item.get("operation_id", "") for item in semantic if item.get("event_type") in {"OP_DONE", "OP_FAILED"})
    external = [item.get("operation_id", "") for item in actions if item.get("side_effect_level") in {"EXTERNAL_MESSAGE", "PUBLIC_CONTENT"}]
    urls = [str(item.get("text", "")) for item in actions if item.get("operation_id") == "BROWSER_OPEN_URL" and item.get("type") == "type"]
    loopback_urls = all(urlparse(url).hostname in {"127.0.0.1", "localhost"} for url in urls if url)
    output_paths = [str(item.get("text", "")) for item in actions if item.get("operation_id") == "WPS_SAVE_DOCUMENT" and item.get("type") == "type"]
    work_root = args.work_dir.resolve()
    paths_under_work = bool(output_paths) and all(Path(path).resolve().is_relative_to(work_root) for path in output_paths)
    with args.trace.open(encoding="utf-8", newline="") as handle: trace = list(csv.DictReader(handle))
    trace_events = Counter(row.get("event_type", "") for row in trace)
    statuses = {
        "action_ids_unique": len(action_ids) == len(set(action_ids)),
        "operation_markers_paired": starts == terminals,
        "no_external_side_effect": not external,
        "browser_urls_loopback_only": loopback_urls,
        "wps_paths_under_work_dir": paths_under_work,
        "dry_run_trace_has_scenario_markers": trace_events["SCENARIO_START"] == 1 and trace_events["SCENARIO_DONE"] == 1,
    }
    payload = {"status": "PASS" if all(statuses.values()) else "FAIL", "checks": statuses, "trace_event_counts": dict(trace_events), "external_operations": external, "browser_urls": urls, "wps_output_paths": output_paths}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(payload["status"])
    return 0 if payload["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
