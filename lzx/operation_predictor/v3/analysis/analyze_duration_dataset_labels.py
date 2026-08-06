#!/usr/bin/env python3
"""Analyze duration-aware labels and switch-label difficulty."""

from __future__ import annotations

import argparse
import csv
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


HORIZONS = [3, 5, 10]
SPLITS = ["train", "val", "test"]


def split_pipe(value: str | None) -> list[str]:
    return [item for item in (value or "").split("|") if item]


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    values = sorted(values)
    idx = min(len(values) - 1, max(0, int(round((len(values) - 1) * p))))
    return values[idx]


def dist_stats(values: list[float]) -> dict[str, float]:
    return {
        "min": min(values) if values else 0.0,
        "p50": percentile(values, 0.50),
        "p90": percentile(values, 0.90),
        "p99": percentile(values, 0.99),
        "max": max(values) if values else 0.0,
    }


def current_app(row: dict[str, str]) -> str:
    if row.get("current_app"):
        return row["current_app"]
    apps = split_pipe(row.get("input_history_apps") or row.get("history_apps"))
    masks = split_pipe(row.get("input_history_mask") or row.get("history_mask"))
    for app, mask in reversed(list(zip(apps, masks))):
        if mask == "1":
            return app
    return ""


def anchor_group(row: dict[str, str]) -> str:
    anchor = row.get("anchor_type") or row.get("trigger_type", "")
    if anchor.startswith("dwell_bucket_cross"):
        return "dwell_bucket_cross"
    if anchor.startswith("foreground_transition"):
        return "foreground_transition"
    if anchor.startswith("periodic_refresh"):
        return "periodic_refresh"
    return anchor or "unknown"


def metric_current_app(cur: str, labels: set[str]) -> dict[str, float]:
    if not cur or not labels:
        return {"hit": 0.0, "recall": 0.0, "precision": 0.0, "mrr": 0.0}
    hit = float(cur in labels)
    return {"hit": hit, "recall": hit / len(labels), "precision": hit, "mrr": hit}


def labels_next(row: dict[str, str], horizon: int, cur: str) -> set[str]:
    field = f"labels_next_{horizon}"
    if field in row:
        return set(split_pipe(row.get(field)))
    labels = set(split_pipe(row.get(f"labels_{horizon}")))
    labels.discard(cur)
    return labels


def analyze_split(path: Path, split_name: str) -> list[dict[str, Any]]:
    total = 0
    by_anchor: dict[str, Counter[str]] = defaultdict(Counter)
    label_sizes: dict[int, list[float]] = {h: [] for h in HORIZONS}
    label_next_sizes: dict[int, list[float]] = {h: [] for h in HORIZONS}
    duration_values: list[float] = []
    duration_thresholds = {600: 0, 3600: 0, 86400: 0}
    summary: Counter[str] = Counter()
    metric_sums: dict[int, Counter[str]] = {h: Counter() for h in HORIZONS}
    metric_sums_next: dict[int, Counter[str]] = {h: Counter() for h in HORIZONS}

    with path.open(encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            total += 1
            group = anchor_group(row)
            by_anchor[group]["total"] += 1
            cur = current_app(row)

            for value, mask in zip(split_pipe(row.get("history_durations_s")), split_pipe(row.get("history_mask"))):
                if mask != "1":
                    continue
                duration = float(value)
                duration_values.append(duration)
                for threshold in duration_thresholds:
                    if duration > threshold:
                        duration_thresholds[threshold] += 1

            for horizon in HORIZONS:
                labels = set(split_pipe(row.get(f"labels_{horizon}")))
                next_labels = labels_next(row, horizon, cur)
                label_sizes[horizon].append(float(len(labels)))
                label_next_sizes[horizon].append(float(len(next_labels)))

                contains = cur in labels if cur else False
                summary[f"current_app_in_labels_{horizon}"] += int(contains)
                by_anchor[group][f"current_app_in_labels_{horizon}"] += int(contains)
                by_anchor[group][f"non_empty_labels_next_{horizon}"] += int(bool(next_labels))

                if f"has_next_{horizon}" in row:
                    has_next = row.get(f"has_next_{horizon}") == "1"
                else:
                    has_next = bool(next_labels)
                summary[f"has_next_{horizon}"] += int(has_next)
                summary[f"non_empty_labels_next_{horizon}"] += int(bool(next_labels))

                for key, value in metric_current_app(cur, labels).items():
                    metric_sums[horizon][key] += value
                for key, value in metric_current_app(cur, next_labels).items():
                    metric_sums_next[horizon][key] += value

    rows: list[dict[str, Any]] = [{"split": split_name, "metric": "num_samples", "value": total}]
    for horizon in HORIZONS:
        rows.append({"split": split_name, "metric": f"current_app_in_labels_{horizon}_ratio", "value": summary[f"current_app_in_labels_{horizon}"] / total if total else 0.0})
        rows.append({"split": split_name, "metric": f"has_next_{horizon}_ratio", "value": summary[f"has_next_{horizon}"] / total if total else 0.0})
        rows.append({"split": split_name, "metric": f"non_empty_labels_next_{horizon}_ratio", "value": summary[f"non_empty_labels_next_{horizon}"] / total if total else 0.0})
        for stat_name, value in dist_stats(label_sizes[horizon]).items():
            rows.append({"split": split_name, "metric": f"label_size_{horizon}_{stat_name}", "value": value})
        for stat_name, value in dist_stats(label_next_sizes[horizon]).items():
            rows.append({"split": split_name, "metric": f"label_next_size_{horizon}_{stat_name}", "value": value})
        for metric_name in ["hit", "recall", "precision", "mrr"]:
            rows.append({"split": split_name, "metric": f"current_app_naive_labels_{horizon}_{metric_name}", "value": metric_sums[horizon][metric_name] / total if total else 0.0})
            rows.append({"split": split_name, "metric": f"current_app_naive_labels_next_{horizon}_{metric_name}", "value": metric_sums_next[horizon][metric_name] / total if total else 0.0})

    for group, counts in sorted(by_anchor.items()):
        group_total = counts["total"]
        rows.append({"split": split_name, "anchor_type": group, "metric": "anchor_samples", "value": group_total})
        for horizon in HORIZONS:
            rows.append({"split": split_name, "anchor_type": group, "metric": f"current_app_in_labels_{horizon}_ratio", "value": counts[f"current_app_in_labels_{horizon}"] / group_total if group_total else 0.0})
            rows.append({"split": split_name, "anchor_type": group, "metric": f"non_empty_labels_next_{horizon}_ratio", "value": counts[f"non_empty_labels_next_{horizon}"] / group_total if group_total else 0.0})

    for stat_name, value in dist_stats(duration_values).items():
        rows.append({"split": split_name, "metric": f"duration_{stat_name}", "value": value})
    duration_total = len(duration_values)
    rows.append({"split": split_name, "metric": "duration_count", "value": duration_total})
    for threshold, count in duration_thresholds.items():
        rows.append({"split": split_name, "metric": f"duration_gt_{threshold}_count", "value": count})
        rows.append({"split": split_name, "metric": f"duration_gt_{threshold}_ratio", "value": count / duration_total if duration_total else 0.0})
    return rows


def top_dwell_segments(path: Path, limit: int = 20) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    rows.sort(key=lambda row: float(row.get("dwell_s") or 0), reverse=True)
    return rows[:limit]


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = ["split", "anchor_type", "metric", "value"]
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fields})


def lookup(rows: list[dict[str, Any]], split: str, metric: str, anchor_type: str = "") -> float:
    for row in rows:
        if row.get("split") == split and row.get("metric") == metric and row.get("anchor_type", "") == anchor_type:
            return float(row.get("value") or 0.0)
    return 0.0


def write_markdown(path: Path, rows: list[dict[str, Any]], top_segments: list[dict[str, str]]) -> None:
    lines: list[str] = ["# Duration Dataset Label Analysis", "", "## Conclusion"]
    test_current = [lookup(rows, "test", f"current_app_in_labels_{h}_ratio") for h in HORIZONS]
    test_naive = [lookup(rows, "test", f"current_app_naive_labels_{h}_hit") for h in HORIZONS]
    test_next = [lookup(rows, "test", f"non_empty_labels_next_{h}_ratio") for h in HORIZONS]
    dwell_samples = sum(lookup(rows, split, "anchor_samples", "dwell_bucket_cross") for split in SPLITS)
    max_dwell = max(float(row.get("dwell_s") or 0.0) for row in top_segments) if top_segments else 0.0
    lines.append(f"- Current app appears in persistence labels: h3={test_current[0]:.6f}, h5={test_current[1]:.6f}, h10={test_current[2]:.6f}.")
    lines.append(f"- Current-app naive Hit@1 on persistence labels: h3={test_naive[0]:.6f}, h5={test_naive[1]:.6f}, h10={test_naive[2]:.6f}.")
    lines.append(f"- labels_next non-empty ratio on test: h3={test_next[0]:.6f}, h5={test_next[1]:.6f}, h10={test_next[2]:.6f}.")
    lines.append(f"- dwell_bucket_cross anchor samples={int(dwell_samples)}; if non-zero and current-app ratio is high, it makes persistence evaluation too easy.")
    lines.append("- Switch-aware labels_next_3/5/10 are recommended for transition evaluation.")
    lines.append(f"- Session gap truncation is recommended when segment dwell_s outliers remain; max top segment dwell_s={max_dwell:.0f}.")
    lines.append("- No memory scheduling action was performed.")
    lines.append("")

    lines.append("## Current App In Labels Ratio")
    lines.append("| split | h3 | h5 | h10 |")
    lines.append("|---|---:|---:|---:|")
    for split in SPLITS:
        lines.append(f"| {split} | {lookup(rows, split, 'current_app_in_labels_3_ratio'):.6f} | {lookup(rows, split, 'current_app_in_labels_5_ratio'):.6f} | {lookup(rows, split, 'current_app_in_labels_10_ratio'):.6f} |")
    lines.append("")

    lines.append("## Current App In Labels By Anchor")
    lines.append("| split | anchor | samples | h3 | h5 | h10 | labels_next h3 | labels_next h5 | labels_next h10 |")
    lines.append("|---|---|---:|---:|---:|---:|---:|---:|---:|")
    for split in SPLITS:
        for anchor in ["foreground_transition", "periodic_refresh", "dwell_bucket_cross"]:
            samples = lookup(rows, split, "anchor_samples", anchor)
            lines.append(f"| {split} | {anchor} | {samples:.0f} | {lookup(rows, split, 'current_app_in_labels_3_ratio', anchor):.6f} | {lookup(rows, split, 'current_app_in_labels_5_ratio', anchor):.6f} | {lookup(rows, split, 'current_app_in_labels_10_ratio', anchor):.6f} | {lookup(rows, split, 'non_empty_labels_next_3_ratio', anchor):.6f} | {lookup(rows, split, 'non_empty_labels_next_5_ratio', anchor):.6f} | {lookup(rows, split, 'non_empty_labels_next_10_ratio', anchor):.6f} |")
    lines.append("")

    lines.append("## Current-App Naive Baseline")
    lines.append("| split | label | h3 Hit@1 | h3 Recall@1 | h3 Precision@1 | h3 MRR | h5 Hit@1 | h10 Hit@1 |")
    lines.append("|---|---|---:|---:|---:|---:|---:|---:|")
    for split in SPLITS:
        for label_name, prefix in [("labels", "current_app_naive_labels"), ("labels_next", "current_app_naive_labels_next")]:
            lines.append(f"| {split} | {label_name} | {lookup(rows, split, prefix + '_3_hit'):.6f} | {lookup(rows, split, prefix + '_3_recall'):.6f} | {lookup(rows, split, prefix + '_3_precision'):.6f} | {lookup(rows, split, prefix + '_3_mrr'):.6f} | {lookup(rows, split, prefix + '_5_hit'):.6f} | {lookup(rows, split, prefix + '_10_hit'):.6f} |")
    lines.append("")

    lines.append("## Label Size Distribution")
    lines.append("| split | label | horizon | min | p50 | p90 | p99 | max |")
    lines.append("|---|---|---:|---:|---:|---:|---:|---:|")
    for split in SPLITS:
        for label_name, prefix in [("labels", "label_size"), ("labels_next", "label_next_size")]:
            for horizon in HORIZONS:
                lines.append(f"| {split} | {label_name} | {horizon} | {lookup(rows, split, f'{prefix}_{horizon}_min'):.0f} | {lookup(rows, split, f'{prefix}_{horizon}_p50'):.0f} | {lookup(rows, split, f'{prefix}_{horizon}_p90'):.0f} | {lookup(rows, split, f'{prefix}_{horizon}_p99'):.0f} | {lookup(rows, split, f'{prefix}_{horizon}_max'):.0f} |")
    lines.append("")

    lines.append("## Labels Next Non-Empty Ratio")
    lines.append("| split | h3 | h5 | h10 |")
    lines.append("|---|---:|---:|---:|")
    for split in SPLITS:
        lines.append(f"| {split} | {lookup(rows, split, 'non_empty_labels_next_3_ratio'):.6f} | {lookup(rows, split, 'non_empty_labels_next_5_ratio'):.6f} | {lookup(rows, split, 'non_empty_labels_next_10_ratio'):.6f} |")
    lines.append("")

    lines.append("## Duration Outliers")
    lines.append("| split | min | p50 | p90 | p99 | max | >600 count | >600 ratio | >3600 count | >3600 ratio | >86400 count | >86400 ratio |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for split in SPLITS:
        lines.append(f"| {split} | {lookup(rows, split, 'duration_min'):.0f} | {lookup(rows, split, 'duration_p50'):.0f} | {lookup(rows, split, 'duration_p90'):.0f} | {lookup(rows, split, 'duration_p99'):.0f} | {lookup(rows, split, 'duration_max'):.0f} | {lookup(rows, split, 'duration_gt_600_count'):.0f} | {lookup(rows, split, 'duration_gt_600_ratio'):.6f} | {lookup(rows, split, 'duration_gt_3600_count'):.0f} | {lookup(rows, split, 'duration_gt_3600_ratio'):.6f} | {lookup(rows, split, 'duration_gt_86400_count'):.0f} | {lookup(rows, split, 'duration_gt_86400_ratio'):.6f} |")
    lines.append("")

    lines.append("## Top 20 Dwell Segments")
    lines.append("| rank | user_id | session_id | segment_id | gap_cut_before | start_time | end_time | app | dwell_s | source_note |")
    lines.append("|---:|---|---|---:|---:|---|---|---|---:|---|")
    for idx, row in enumerate(top_segments, start=1):
        lines.append(f"| {idx} | {row.get('user_id','')} | {row.get('session_id','')} | {row.get('segment_id','')} | {row.get('gap_cut_before','')} | {row.get('start_time','')} | {row.get('end_time','')} | {row.get('app','')} | {row.get('dwell_s','')} | {row.get('source_note','')} |")
    lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Analyze duration-aware dataset labels.")
    parser.add_argument("--dataset-dir", default="huawei_mem/lzx/operation_predictor/data/processed/app_lstm_duration_gap3600_periodic180")
    parser.add_argument("--output-md", default="huawei_mem/lzx/operation_predictor/outputs/results/v3/duration_dataset_label_analysis_gap3600_periodic180.md")
    parser.add_argument("--output-csv", default="huawei_mem/lzx/operation_predictor/outputs/results/v3/duration_dataset_label_analysis_gap3600_periodic180.csv")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    dataset_dir = Path(args.dataset_dir)
    rows: list[dict[str, Any]] = []
    for split in SPLITS:
        rows.extend(analyze_split(dataset_dir / f"{split}.csv", split))
    top_segments = top_dwell_segments(dataset_dir / "lsapp_app_state_segments.csv", 20)
    write_csv(Path(args.output_csv), rows)
    write_markdown(Path(args.output_md), rows, top_segments)
    print(f"saved: {args.output_csv}")
    print(f"saved: {args.output_md}")


if __name__ == "__main__":
    main()
