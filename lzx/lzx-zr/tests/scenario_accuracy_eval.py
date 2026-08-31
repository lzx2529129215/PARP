from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from predictor.workload_predictor import WorkloadPredictor
from runtime_monitor.detector.state_machine import StateMachine
from runtime_monitor.features.engine import Observation, extract_features

REPORT_DIR = ROOT / "test-reports"
REPORT_DIR.mkdir(parents=True, exist_ok=True)


SCENARIOS: list[dict[str, Any]] = [
    {
        "name": "stable_hot",
        "expected_dominant": "STABLE_HOT",
        "expected_next": {
            "rule_trend": "STABLE_HOT",
            "second_order_markov": "STABLE_HOT",
        },
        "observations": [
            {
                "scope_type": "region",
                "scope_id": "stable-hot",
                "window_start_ns": 0,
                "window_end_ns": 1000,
                "timestamp_ns": 1000,
                "sampling_interval_ms": 1000,
                "region_ids": ["r1", "r1", "r1", "r1"],
                "region_accesses": [6, 6, 6, 6],
                "region_timestamps_ns": [10, 20, 30, 40],
                "counters": {
                    "wss_pages": 128,
                    "wss_delta_pages": 0,
                    "allocation_delta_pages": 0,
                    "pgfault_delta": 0,
                    "hotspot_jaccard": 0.9,
                    "hotspot_shift_rate": 0.1,
                    "psi": 0.1,
                    "foreground": 1,
                },
            }
        ],
    },
    {
        "name": "burst_expansion",
        "expected_dominant": "BURST_EXPANSION",
        "expected_next": {
            "rule_trend": "STABLE_HOT",
            "second_order_markov": "BURST_EXPANSION",
        },
        "observations": [
            {
                "scope_type": "region",
                "scope_id": "burst-expansion",
                "window_start_ns": 0,
                "window_end_ns": 1000,
                "timestamp_ns": 1000,
                "sampling_interval_ms": 1000,
                "region_ids": ["r1", "r2", "r3", "r4"],
                "region_accesses": [2, 3, 4, 5],
                "region_timestamps_ns": [10, 20, 30, 40],
                "counters": {
                    "wss_pages": 500,
                    "wss_delta_pages": 120,
                    "allocation_delta_pages": 90,
                    "pgfault_delta": 8,
                    "hotspot_jaccard": 0.6,
                    "hotspot_shift_rate": 0.2,
                    "psi": 0.05,
                    "foreground": 1,
                },
            }
        ],
    },
    {
        "name": "streaming",
        "expected_dominant": "STREAMING",
        "expected_next": {
            "rule_trend": "STREAMING",
            "second_order_markov": "STREAMING",
        },
        "observations": [
            {
                "scope_type": "region",
                "scope_id": "streaming",
                "window_start_ns": 0,
                "window_end_ns": 1000,
                "timestamp_ns": 1000,
                "sampling_interval_ms": 1000,
                "region_ids": ["r1", "r2", "r3", "r4"],
                "region_accesses": [1, 1, 1, 1],
                "region_timestamps_ns": [10, 20, 30, 40],
                "counters": {
                    "wss_pages": 40,
                    "wss_delta_pages": 0,
                    "allocation_delta_pages": 0,
                    "pgfault_delta": 0,
                    "hotspot_jaccard": 0.1,
                    "hotspot_shift_rate": 0.1,
                    "psi": 0.0,
                    "foreground": 1,
                },
            }
        ],
    },
    {
        "name": "random_unknown",
        "expected_dominant": "UNKNOWN",
        "expected_next": {
            "rule_trend": "UNKNOWN",
            "second_order_markov": "UNKNOWN",
        },
        "observations": [
            {
                "scope_type": "region",
                "scope_id": "random-unknown",
                "window_start_ns": 0,
                "window_end_ns": 1000,
                "timestamp_ns": 1000,
                "sampling_interval_ms": 1000,
                "region_ids": ["r4", "r1", "r3", "r2"],
                "region_accesses": [1, 1, 1, 1],
                "region_timestamps_ns": [10, 20, 30, 40],
                "counters": {
                    "wss_pages": 200,
                    "wss_delta_pages": 0,
                    "allocation_delta_pages": 0,
                    "pgfault_delta": 0,
                    "hotspot_jaccard": 0.2,
                    "hotspot_shift_rate": 0.2,
                    "psi": 0.0,
                    "foreground": 1,
                },
            }
        ],
    },
    {
        "name": "cold",
        "expected_dominant": "LOW_VALUE_COLD",
        "expected_next": {
            "rule_trend": "LOW_VALUE_COLD",
            "second_order_markov": "LOW_VALUE_COLD",
        },
        "observations": [
            {
                "scope_type": "cgroup",
                "scope_id": "cold",
                "window_start_ns": 0,
                "window_end_ns": 1000,
                "timestamp_ns": 1000,
                "sampling_interval_ms": 1000,
                "region_ids": [],
                "region_accesses": [],
                "region_timestamps_ns": [],
                "counters": {
                    "wss_pages": 0,
                    "wss_delta_pages": 0,
                    "allocation_delta_pages": 0,
                    "pgfault_delta": 0,
                    "psi": 0.0,
                    "foreground": 0,
                },
            }
        ],
    },
    {
        "name": "multi_hotspot",
        "expected_dominant": "MULTI_HOTSPOT",
        "expected_next": {
            "rule_trend": "MULTI_HOTSPOT",
            "second_order_markov": "MULTI_HOTSPOT",
        },
        "observations": [
            {
                "scope_type": "region",
                "scope_id": "multi-hotspot",
                "window_start_ns": 0,
                "window_end_ns": 1000,
                "timestamp_ns": 1000,
                "sampling_interval_ms": 1000,
                "region_ids": ["r1", "r2", "r1", "r3"],
                "region_accesses": [6, 5, 6, 5],
                "region_timestamps_ns": [10, 20, 30, 40],
                "counters": {
                    "wss_pages": 320,
                    "wss_delta_pages": 10,
                    "allocation_delta_pages": 0,
                    "pgfault_delta": 2,
                    "hotspot_jaccard": 0.6,
                    "hotspot_shift_rate": 0.9,
                    "psi": 0.0,
                    "foreground": 1,
                },
            }
        ],
    },
]


def make_observation(row: dict[str, Any]) -> Observation:
    return Observation(
        scope_type=str(row.get("scope_type", "cgroup")),
        scope_id=str(row.get("scope_id", "")),
        window_start_ns=int(row.get("window_start_ns", 0)),
        window_end_ns=int(row.get("window_end_ns", 0)),
        timestamp_ns=int(row.get("timestamp_ns", 0)),
        sampling_interval_ms=int(row.get("sampling_interval_ms", 1000)),
        region_ids=tuple(str(value) for value in row.get("region_ids", [])),
        region_accesses=tuple(float(value) for value in row.get("region_accesses", [])),
        region_timestamps_ns=tuple(int(value) for value in row.get("region_timestamps_ns", [])),
        counters={str(key): float(value) for key, value in row.get("counters", {}).items()},
    )


def evaluate_case(case: dict[str, Any]) -> dict[str, Any]:
    machine = StateMachine()
    predictor = WorkloadPredictor()
    history: list[Any] = []
    classification_correct = False
    rule_trend_correct = False
    markov_correct = False

    for index, row in enumerate(case["observations"]):
        observation = make_observation(row)
        features = extract_features(observation)
        state = machine.update(features)

        if index == 0:
            classification_correct = state.dominant == case["expected_dominant"]
            last_state = state
        else:
            last_state = state

        if history:
            predictor.observe_transition(history[-1], state)
        history.append(state)

        if len(history) >= 1:
            rule_prediction = predictor.predict_rule_trend(state)
            markov_prediction = predictor.predict_markov(history, state)
            rule_trend_correct = rule_prediction.next.dominant == case["expected_next"]["rule_trend"]
            markov_correct = markov_prediction.next.dominant == case["expected_next"]["second_order_markov"]

    return {
        "name": case["name"],
        "expected_dominant": case["expected_dominant"],
        "actual_dominant": last_state.dominant,
        "classification_correct": classification_correct,
        "rule_trend_correct": rule_trend_correct,
        "second_order_markov_correct": markov_correct,
        "expected_next": case["expected_next"],
        "actual_next": {
            "rule_trend": predictor.predict_rule_trend(last_state).next.dominant,
            "second_order_markov": predictor.predict_markov(history, last_state).next.dominant,
        },
    }


def main() -> int:
    results = [evaluate_case(case) for case in SCENARIOS]

    classification_total = len(results)
    classification_correct_count = sum(1 for item in results if item["classification_correct"])
    rule_total = classification_total
    rule_correct_count = sum(1 for item in results if item["rule_trend_correct"])
    markov_total = classification_total
    markov_correct_count = sum(1 for item in results if item["second_order_markov_correct"])

    summary = {
        "scenario_count": classification_total,
        "classification_accuracy": round((classification_correct_count / classification_total) * 100.0, 2) if classification_total else 0.0,
        "rule_trend_prediction_accuracy": round((rule_correct_count / rule_total) * 100.0, 2) if rule_total else 0.0,
        "second_order_markov_prediction_accuracy": round((markov_correct_count / markov_total) * 100.0, 2) if markov_total else 0.0,
        "results": results,
    }

    report_json = REPORT_DIR / "scenario_accuracy_report.json"
    report_md = REPORT_DIR / "scenario_accuracy_report.md"
    report_json.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    lines: list[str] = [
        "# Scenario Judgment and Prediction Accuracy Report",
        "",
        f"- Scenario count: {summary['scenario_count']}",
        f"- Judgment accuracy: {summary['classification_accuracy']}%",
        f"- Rule trend prediction accuracy: {summary['rule_trend_prediction_accuracy']}%",
        f"- Second-order Markov prediction accuracy: {summary['second_order_markov_prediction_accuracy']}%",
        "",
        "## Per-scenario result",
        "",
    ]

    for item in results:
        lines.append(f"### {item['name']}")
        lines.append(f"- expected dominant: {item['expected_dominant']}")
        lines.append(f"- actual dominant: {item['actual_dominant']}")
        lines.append(f"- judgment correct: {item['classification_correct']}")
        lines.append(f"- expected next (rule_trend): {item['expected_next']['rule_trend']}")
        lines.append(f"- actual next (rule_trend): {item['actual_next']['rule_trend']}")
        lines.append(f"- rule_trend prediction correct: {item['rule_trend_correct']}")
        lines.append(f"- expected next (second_order_markov): {item['expected_next']['second_order_markov']}")
        lines.append(f"- actual next (second_order_markov): {item['actual_next']['second_order_markov']}")
        lines.append(f"- second_order_markov prediction correct: {item['second_order_markov_correct']}")
        lines.append("")

    report_md.write_text("\n".join(lines), encoding="utf-8")

    print(json.dumps({
        "scenario_count": summary["scenario_count"],
        "classification_accuracy": summary["classification_accuracy"],
        "rule_trend_prediction_accuracy": summary["rule_trend_prediction_accuracy"],
        "second_order_markov_prediction_accuracy": summary["second_order_markov_prediction_accuracy"],
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
