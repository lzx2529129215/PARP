#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def ratio(value: int, total: int) -> float:
    return value / total if total else 0.0


def is_anon(region_type: str) -> bool:
    return "ANON" in region_type or region_type in {"ANON_HEAP", "ANON_STACK"}


def is_file(region_type: str) -> bool:
    return region_type in {"FILE", "SHARED_LIBRARY", "DOCUMENT_FILE"}


def top_regions(heat: dict[str, float], region_by_id: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for region_id, accesses in sorted(heat.items(), key=lambda item: item[1], reverse=True)[:20]:
        region = region_by_id.get(region_id, {})
        metadata = region.get("path_metadata", {})
        result.append(
            {
                "region_id": int(region_id),
                "weighted_accesses": accesses,
                "region_type": region.get("region_type", ""),
                "pathname": metadata.get("pathname", ""),
                "basename": metadata.get("basename", ""),
                "process_role": region.get("process_role", ""),
                "identity_confidence": region.get("identity_confidence", ""),
            }
        )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="分析 Region Monitor 输出。")
    parser.add_argument("region_monitor_dir")
    args = parser.parse_args()
    root = Path(args.region_monitor_dir)
    vocab_path = root / "region_vocab.json"
    vocab = json.loads(vocab_path.read_text(encoding="utf-8")) if vocab_path.exists() else {"regions": []}
    regions = vocab.get("regions", [])
    region_by_id = {str(item.get("region_id")): item for item in regions}
    windows = read_jsonl(root / "region_windows.jsonl")
    events = read_jsonl(root / "region_events.jsonl")

    region_type = Counter(str(item.get("region_type", "")) for item in regions)
    confidence = Counter(str(item.get("identity_confidence", "")) for item in regions)
    role_access: dict[str, float] = defaultdict(float)
    role_observations: Counter[str] = Counter()
    file_heat: dict[str, float] = defaultdict(float)
    anon_heat: dict[str, float] = defaultdict(float)
    cgroup_status = Counter()
    cgroup_availability = Counter()
    active_counts: list[int] = []
    event_counts: list[int] = []
    mapped = unmapped = lowres = 0

    for row in windows:
        vector = row.get("region_sparse_vector", {})
        active_counts.append(len(vector))
        event_counts.append(int(row.get("damon_event_count", 0)))
        mapped += int(row.get("mapped_event_count", 0))
        unmapped += int(row.get("unmapped_event_count", 0))
        lowres += int(row.get("low_resolution_event_count", 0))
        cgroup = row.get("cgroup_features", {})
        cgroup_status[str(cgroup.get("status", "missing"))] += 1
        for field, status in cgroup.get("availability", {}).items():
            cgroup_availability[f"{field}:{status}"] += 1
        for cell in vector.values():
            access = float(cell.get("weighted_accesses", 0.0))
            role = str(cell.get("process_role", ""))
            role_access[role] += access
            role_observations[role] += 1
            region_id = str(cell.get("region_id"))
            if is_anon(str(cell.get("region_type", ""))):
                anon_heat[region_id] += access
            elif is_file(str(cell.get("region_type", ""))):
                file_heat[region_id] += access

    total_events = len(events) or mapped + unmapped
    mapped_ratio = ratio(mapped, total_events)
    unmapped_ratio = ratio(unmapped, total_events)
    lowres_ratio = ratio(lowres, total_events)
    file_region_count = sum(count for kind, count in region_type.items() if is_file(kind))
    anon_region_count = sum(count for kind, count in region_type.items() if is_anon(kind))
    cgroup_ok = bool(windows) and cgroup_status.get("ok", 0) == len(windows)
    known_roles = sum(count for role, count in role_observations.items() if role and role != "UNKNOWN")
    data_quality_issues: list[str] = []
    if not windows:
        data_quality_issues.append("NO_WINDOWS")
    if mapped_ratio < 0.8:
        data_quality_issues.append("LOW_MAPPED_EVENT_RATIO")
    if lowres_ratio > 0.5:
        data_quality_issues.append("HIGH_LOW_RESOLUTION_RATIO")
    if not file_region_count:
        data_quality_issues.append("NO_TRUSTED_FILE_REGION")
    if not cgroup_ok:
        data_quality_issues.append("CGROUP_ALIGNMENT_INCOMPLETE")
    if not known_roles:
        data_quality_issues.append("PROCESS_ROLE_UNRESOLVED")

    operation_ready = bool(
        total_events
        and windows
        and mapped_ratio >= 0.8
        and lowres_ratio <= 0.5
        and file_region_count
        and cgroup_ok
        and known_roles
    )
    summary = {
        "total_damon_events": total_events,
        "mapped_event_count": mapped,
        "unmapped_event_count": unmapped,
        "mapped_event_ratio": mapped_ratio,
        "unmapped_event_ratio": unmapped_ratio,
        "low_resolution_event_count": lowres,
        "low_resolution_ratio": lowres_ratio,
        "region_window_count": len(windows),
        "region_count": len(regions),
        "file_region_count": file_region_count,
        "anonymous_region_count": anon_region_count,
        "region_type_distribution": dict(sorted(region_type.items())),
        "identity_confidence_distribution": dict(sorted(confidence.items())),
        "process_role_weighted_accesses": dict(sorted(role_access.items())),
        "process_role_observations": dict(sorted(role_observations.items())),
        "top_file_regions": top_regions(file_heat, region_by_id),
        "top_anonymous_regions": top_regions(anon_heat, region_by_id),
        "avg_active_regions_per_window": sum(active_counts) / len(active_counts) if active_counts else 0.0,
        "avg_events_per_window": sum(event_counts) / len(event_counts) if event_counts else 0.0,
        "cgroup_status_distribution": dict(sorted(cgroup_status.items())),
        "cgroup_field_availability": dict(sorted(cgroup_availability.items())),
        "cgroup_alignment_ok": cgroup_ok,
        "data_quality_issues": data_quality_issues,
        "meets_operation_collection_requirements": operation_ready,
        "ready_for_operation_recognition": False,
        "ready_for_apply": False,
    }
    (root / "region_monitor_analysis.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    lines = [
        "# Region Monitor 分析报告",
        "",
        f"- DAMON 事件总数：{total_events}",
        f"- mapped/unmapped：{mapped}/{unmapped}（{mapped_ratio:.2%}/{unmapped_ratio:.2%}）",
        f"- 低分辨率事件：{lowres}（{lowres_ratio:.2%}）",
        f"- region 窗口数：{len(windows)}",
        f"- file/anonymous region：{file_region_count}/{anon_region_count}",
        f"- 平均每窗口活跃 region：{summary['avg_active_regions_per_window']:.2f}",
        f"- 平均每窗口事件数：{summary['avg_events_per_window']:.2f}",
        f"- cgroup 对齐：{'PASS' if cgroup_ok else 'FAIL'}",
        f"- 满足操作级采集要求：{str(operation_ready).lower()}",
        "- ready_for_operation_recognition: false",
        "- ready_for_apply: false",
        "",
        "## 数据质量问题",
        "",
    ]
    lines.extend(f"- {issue}" for issue in data_quality_issues) if data_quality_issues else lines.append("- 无")
    lines += ["", "## Identity confidence", ""]
    lines.extend(f"- {key}: {value}" for key, value in sorted(confidence.items()))
    lines += ["", "## Process role 访问", ""]
    lines.extend(
        f"- {key}: weighted_accesses={role_access[key]:.3f}, observations={role_observations[key]}"
        for key in sorted(role_observations)
    )
    lines += ["", "## Top file regions", ""]
    lines.extend(
        f"- region_id={item['region_id']}, accesses={item['weighted_accesses']:.3f}, "
        f"type={item['region_type']}, role={item['process_role']}, path={item['pathname'] or 'N/A'}"
        for item in summary["top_file_regions"]
    )
    lines += ["", "## Top anonymous regions", ""]
    lines.extend(
        f"- region_id={item['region_id']}, accesses={item['weighted_accesses']:.3f}, "
        f"type={item['region_type']}, role={item['process_role']}"
        for item in summary["top_anonymous_regions"]
    )
    lines += ["", "## cgroup availability", ""]
    lines.extend(f"- {key}: {value}" for key, value in sorted(cgroup_availability.items()))
    (root / "region_monitor_analysis.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
