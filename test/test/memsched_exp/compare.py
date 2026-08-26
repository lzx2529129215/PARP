from __future__ import annotations

import argparse
import json
import math
import random
import statistics
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable

from .report import discover_runs, row_for_run


METRIC_DIRECTIONS = {
    "launch_latency_ms": "lower",
    "page_refault_count": "lower",
    "page_refault_ratio": "lower",
    "foreground_page_refault_count": "lower",
    "foreground_page_refault_ratio": "lower",
    "direct_reclaim_allocstall_count": "lower",
    "direct_reclaim_tracepoint_count": "lower",
    "direct_reclaim_total_duration_ms": "lower",
    "direct_reclaim_boundary_spanning_count": "lower",
    "direct_reclaim_event_ratio": "lower",
    "direct_reclaim_scanned_pages": "lower",
    "direct_reclaim_page_ratio": "lower",
    "io_read_throughput_mb_s": "higher",
    "cpu_one_core_percent": "lower",
    "cpu_machine_percent": "lower",
    "oom_kill_count": "lower",
    "average_fps": "higher",
    "fps_per_second_stddev": "lower",
    "jank_ratio": "lower",
    "gb_cold_launch_latency_ms": "lower",
    "cold_launch_io_throughput_mb_s": "higher",
    "cold_restart_count": "lower",
    "background_apps_alive": "higher",
    "background_app_survival_ratio": "higher",
    "hot_launch_latency_ms_mean": "lower",
    "maximum_cached_apps_without_loss": "higher",
    "gc_working_set_objects": "lower",
    "object_reaccess_ratio_mean": "higher",
}


def percentile(values: Iterable[float], quantile: float) -> float | None:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return None
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] * (1 - fraction) + ordered[upper] * fraction


def describe(values: list[float]) -> dict[str, float | int | None]:
    return {
        "n": len(values),
        "mean": statistics.fmean(values) if values else None,
        "median": statistics.median(values) if values else None,
        "sample_stddev": statistics.stdev(values) if len(values) > 1 else (0.0 if values else None),
        "p90": percentile(values, 0.90),
        "p95": percentile(values, 0.95),
        "min": min(values) if values else None,
        "max": max(values) if values else None,
    }


def bootstrap_mean_ci(
    values: list[float],
    iterations: int = 10_000,
    seed: int = 20260814,
) -> list[float] | None:
    if not values:
        return None
    if len(values) == 1:
        return [values[0], values[0]]
    generator = random.Random(seed)
    means = [
        statistics.fmean(generator.choice(values) for _ in values)
        for _ in range(max(iterations, 1))
    ]
    return [float(percentile(means, 0.025)), float(percentile(means, 0.975))]


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    parsed = float(value)
    return parsed if math.isfinite(parsed) else None


def _improvement_percent(baseline: float, candidate: float, direction: str) -> float | None:
    if baseline == 0:
        return None
    if direction == "lower":
        return (baseline - candidate) / abs(baseline) * 100
    if direction == "higher":
        return (candidate - baseline) / abs(baseline) * 100
    return None


def confidence_conclusion(ci: list[float] | None, direction: str) -> dict[str, bool | str | None]:
    """Interpret a candidate-minus-baseline 95% confidence interval."""
    if ci is None:
        return {
            "statistically_significant_95ci": None,
            "directionally_favorable_95ci": None,
            "confidence_conclusion": "not_evaluable",
        }
    significant = ci[0] > 0 or ci[1] < 0
    favorable = (direction == "lower" and ci[1] < 0) or (direction == "higher" and ci[0] > 0)
    if favorable:
        conclusion = "significant_improvement"
    elif significant:
        conclusion = "significant_regression"
    else:
        conclusion = "no_significant_difference"
    return {
        "statistically_significant_95ci": significant,
        "directionally_favorable_95ci": favorable,
        "confidence_conclusion": conclusion,
    }


def compare_rows(
    rows: list[dict[str, Any]],
    iterations: int = 10_000,
    bootstrap_seed: int = 20260814,
) -> dict[str, Any]:
    by_pair: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    skipped = Counter()
    for row in rows:
        variant = row.get("variant")
        pair_id = row.get("pair_id")
        if variant not in {"baseline", "candidate"} or not pair_id:
            skipped["missing_or_invalid_manifest"] += 1
            continue
        if not row.get("measurement_valid"):
            skipped[f"invalid_{variant}_run"] += 1
            continue
        if variant in by_pair[str(pair_id)]:
            skipped[f"duplicate_{variant}_pair"] += 1
            by_pair[str(pair_id)][variant] = {}
            continue
        by_pair[str(pair_id)][variant] = row

    pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for variants in by_pair.values():
        baseline = variants.get("baseline")
        candidate = variants.get("candidate")
        if not baseline or not candidate:
            skipped["unmatched_pair"] += 1
            continue
        if baseline.get("environment_hash") != candidate.get("environment_hash"):
            skipped["environment_mismatch"] += 1
            continue
        pairs.append((baseline, candidate))

    metrics: list[dict[str, Any]] = []
    scenarios = sorted({str(baseline.get("scenario")) for baseline, _ in pairs})
    for scenario in scenarios:
        scenario_pairs = [pair for pair in pairs if str(pair[0].get("scenario")) == scenario]
        for metric, direction in METRIC_DIRECTIONS.items():
            samples: list[tuple[float, float]] = []
            for baseline, candidate in scenario_pairs:
                baseline_value = _number(baseline.get(metric))
                candidate_value = _number(candidate.get(metric))
                if baseline_value is not None and candidate_value is not None:
                    samples.append((baseline_value, candidate_value))
            if not samples:
                continue
            baseline_values = [sample[0] for sample in samples]
            candidate_values = [sample[1] for sample in samples]
            differences = [candidate - baseline for baseline, candidate in samples]
            baseline_mean = statistics.fmean(baseline_values)
            candidate_mean = statistics.fmean(candidate_values)
            difference_ci = bootstrap_mean_ci(
                differences,
                iterations=iterations,
                seed=bootstrap_seed,
            )
            metrics.append(
                {
                    "scenario": scenario,
                    "metric": metric,
                    "direction": direction,
                    "paired_samples": len(samples),
                    "baseline": describe(baseline_values),
                    "candidate": describe(candidate_values),
                    "paired_difference_candidate_minus_baseline": describe(differences),
                    "paired_difference_mean_bootstrap_95ci": difference_ci,
                    "improvement_percent_from_means": _improvement_percent(
                        baseline_mean,
                        candidate_mean,
                        direction,
                    ),
                    **confidence_conclusion(difference_ci, direction),
                }
            )

    return {
        "schema_version": 1,
        "valid": bool(pairs),
        "paired_runs": len(pairs),
        "scenario_count": len(scenarios),
        "input_rows": len(rows),
        "skipped": dict(sorted(skipped.items())),
        "bootstrap": {"iterations": iterations, "seed": bootstrap_seed},
        "metrics": metrics,
    }


def markdown_report(value: dict[str, Any]) -> str:
    lines = [
        "# Baseline / Candidate 配对实验报告",
        "",
        f"- 有效配对轮次：{value['paired_runs']}",
        f"- 输入轮次：{value['input_rows']}",
        f"- 跳过情况：`{json.dumps(value['skipped'], ensure_ascii=False)}`",
        "",
        "| 场景 | 指标 | 方向 | N | Baseline mean | Candidate mean | 改善率 | 配对差 95% CI | 结论 |",
        "|---|---|---|---:|---:|---:|---:|---|---|",
    ]
    for item in value["metrics"]:
        improvement = item["improvement_percent_from_means"]
        improvement_text = "N/A" if improvement is None else f"{improvement:.3f}%"
        ci = item["paired_difference_mean_bootstrap_95ci"]
        ci_text = "N/A" if ci is None else f"[{ci[0]:.6g}, {ci[1]:.6g}]"
        lines.append(
            f"| {item['scenario']} | `{item['metric']}` | {item['direction']} | {item['paired_samples']} | "
            f"{item['baseline']['mean']:.6g} | {item['candidate']['mean']:.6g} | {improvement_text} | "
            f"{ci_text} | {item['confidence_conclusion']} |"
        )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compare paired baseline/candidate experiment runs")
    parser.add_argument("--root", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--markdown")
    parser.add_argument("--bootstrap-iterations", type=int, default=10_000)
    parser.add_argument("--bootstrap-seed", type=int, default=20260814)
    args = parser.parse_args(argv)
    rows = [row_for_run(run) for run in discover_runs(Path(args.root))]
    value = compare_rows(rows, args.bootstrap_iterations, args.bootstrap_seed)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.markdown:
        markdown = Path(args.markdown)
        markdown.parent.mkdir(parents=True, exist_ok=True)
        markdown.write_text(markdown_report(value), encoding="utf-8")
    print(output)
    return 0 if value["valid"] else 5


if __name__ == "__main__":
    raise SystemExit(main())
